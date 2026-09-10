"""Solver-free audit and design for fixed-scope R16/S4 search semantics.

This module reads immutable research artifacts only.  It deliberately avoids
imports from OR-Tools, Django, model construction, validation, and scheduling
runtime modules.  It does not execute or replay any schedule.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import math
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median


IA_ROOT = Path(
    r"C:\Users\desou\research_runs\v2_r16_ia_three_hour_20260909_021418_001ceb5d"
)
IA_AUDIT_ROOT = Path(
    r"C:\Users\desou\research_runs\v2_r16_ia_three_hour_postrun_audit_20260909_052301_001ceb5d_v3"
)
TOP_ROOT = Path(
    r"C:\Users\desou\research_runs\v2_r16_vs_r4_r16_three_hour_20260907_054732_b8d42e6c"
)
QUALIFICATION_ROOT = Path(
    r"C:\Users\desou\research_runs\v2_r16_top_vs_ia_eight_worker_20260908_0245_a7c1e9f3"
)
JACKPOT_ROOT = Path(
    r"C:\Users\desou\research_runs\v2_r16_target_selection_jackpot_calibration_20260908T000500Z_b42e7d91"
)
FORENSIC_ROOT = Path(
    r"C:\Users\desou\research_runs\v2_r16_target_selection_forensic_followup_20260908e"
)
OUTPUT_PARENT = Path(r"C:\Users\desou\research_runs")
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

COMPONENTS = (
    "course_category_diversity",
    "course_sequence_preferences",
    "difficulty_balance",
    "section_utilization_balance",
    "student_semester_load_balance",
)
PREFIX_MINUTES = (45, 60, 75, 90, 105, 120, 135, 150, 165)
DEEP_ATTEMPTS = (2, 10, 12, 13, 28, 32, 37, 49, 51, 52, 56, 59, 60, 61, 62, 63)
HIGH_SEQUENCE_CENTERS = (13, 28, 37, 49, 52, 59)


def load_json(path: Path):
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            return json.load(stream)
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def verify_seal(root: Path):
    manifest = root / "artifact_hashes.sha256"
    seal = root / "SEALED"
    if not manifest.exists() or not seal.exists():
        return {"verified": False, "reason": "missing_manifest_or_seal"}
    manifest_hash = sha256(manifest)
    seal_text = seal.read_text(encoding="utf-8").strip()
    try:
        seal_payload = json.loads(seal_text)
        expected_hash = seal_payload.get("artifact_hashes_sha256", seal_text)
    except json.JSONDecodeError:
        expected_hash = seal_text
    missing = []
    mismatched = []
    count = 0
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        expected, relative = line.split("  ", 1)
        target = root / relative
        count += 1
        if not target.exists():
            missing.append(relative)
        elif sha256(target) != expected:
            mismatched.append(relative)
    return {
        "verified": manifest_hash == expected_hash and not missing and not mismatched,
        "manifest_sha256": manifest_hash,
        "hash_manifest_matches_seal": manifest_hash == expected_hash,
        "file_count": count,
        "missing_files": missing,
        "mismatched_files": mismatched,
    }


def write_json(path: Path, value):
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )


def write_csv(path: Path, rows, fieldnames=None):
    rows = list(rows)
    if fieldnames is None:
        fieldnames = list(rows[0]) if rows else ()
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def percentile(values, fraction):
    values = sorted(float(value) for value in values)
    if not values:
        return None
    position = (len(values) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return values[lower]
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def distribution(values):
    values = [float(value) for value in values if value is not None]
    if not values:
        return {"count": 0}
    return {
        "count": len(values),
        "min": min(values),
        "p25": percentile(values, 0.25),
        "median": median(values),
        "mean": mean(values),
        "p75": percentile(values, 0.75),
        "max": max(values),
    }


def average_ranks(values):
    indexed = sorted(enumerate(values), key=lambda item: (item[1], item[0]))
    ranks = [0.0] * len(values)
    start = 0
    while start < len(indexed):
        end = start + 1
        while end < len(indexed) and indexed[end][1] == indexed[start][1]:
            end += 1
        rank = (start + 1 + end) / 2.0
        for index, _ in indexed[start:end]:
            ranks[index] = rank
        start = end
    return ranks


def pearson(left, right):
    if len(left) < 2 or len(right) != len(left):
        return None
    left_mean = mean(left)
    right_mean = mean(right)
    numerator = sum((x - left_mean) * (y - right_mean) for x, y in zip(left, right))
    denominator = math.sqrt(
        sum((x - left_mean) ** 2 for x in left)
        * sum((y - right_mean) ** 2 for y in right)
    )
    return numerator / denominator if denominator else None


def spearman(left, right):
    return pearson(average_ranks(left), average_ranks(right))


def common_language(high, low):
    if not high or not low:
        return None
    wins = sum(x > y for x in high for y in low)
    ties = sum(x == y for x in high for y in low)
    return (wins + 0.5 * ties) / (len(high) * len(low))


def jaccard(left, right):
    left = set(left)
    right = set(right)
    if not left and not right:
        return None
    return len(left & right) / len(left | right)


def compact_json(value):
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def component_improvements(before_quality, after_quality):
    result = {}
    for component in COMPONENTS:
        before = before_quality["components"][component]
        after = after_quality["components"][component]
        result[component] = (
            float(before["weighted_normalized_contribution"])
            - float(after["weighted_normalized_contribution"])
        )
    return result


def scope_key(scope):
    return tuple(sorted(int(student_id) for student_id in scope))


def checkpoint_path(root, branch, attempt_index):
    if attempt_index == 0:
        return root / "branches" / branch / "checkpoints" / "source.json.gz"
    return root / "branches" / branch / "checkpoints" / f"incumbent_{attempt_index:04d}.json.gz"


def extract_inner(attempt):
    rows = attempt.get("inner_probe_summaries") or []
    return rows[0] if rows else {}


def selection_features(snapshot):
    selection = snapshot.get("ia_selection", {})
    guidance = selection.get("guidance_facts", {})
    leverage_rows = guidance.get("selected_leverage", [])
    traces = selection.get("cluster_construction_trace", [])
    groups = {
        int(group)
        for row in leverage_rows
        for group in row.get("delivery_group_ids", [])
    }
    move_facts = [fact for row in leverage_rows for fact in row.get("move_facts", [])]
    total_leverage = sum(float(row.get("total_positive_leverage", 0) or 0) for row in leverage_rows)
    return {
        "selected_total_positive_leverage": total_leverage,
        "selected_mean_positive_leverage": total_leverage / len(leverage_rows) if leverage_rows else 0,
        "selected_positive_move_fact_count": len(move_facts),
        "selected_alternate_destination_count": sum(
            int(row.get("alternate_section_opportunity_count", 0) or 0)
            for row in leverage_rows
        ),
        "selected_delivery_group_count": len(groups),
        "pressured_delivery_group_count": len(selection.get("pressure_groups", []) or []),
        "top_shadow_overlap_count": len(
            set(snapshot.get("ia_scope", [])) & set(snapshot.get("top_shadow_scope", []))
        ),
        "top_shadow_jaccard": snapshot.get("scope_comparison", {}).get("jaccard"),
        "trace_by_student": {
            int(row["student_id"]): {
                "position": int(row["selection_position"]),
                "overlap": int(row.get("overlap_with_selected", 0) or 0),
                "positive_leverage": float(row.get("total_positive_leverage", 0) or 0),
                "positive_move_count": int(row.get("positive_move_count", 0) or 0),
                "delivery_group_ids": list(row.get("delivery_group_ids", [])),
                "focused_on_top_group": bool(row.get("focused_on_top_group")),
            }
            for row in traces
        },
    }


def read_ia_attempts():
    attempts_dir = IA_ROOT / "branches" / "ia_only" / "attempts"
    targeting_dir = IA_ROOT / "branches" / "ia_only" / "targeting"
    rows = []
    previous_scope = None
    cumulative_gain = 0.0
    seen_students = set()
    for attempt_path in sorted(attempts_dir.glob("attempt_*.json")):
        payload = load_json(attempt_path)
        attempt_index = int(payload["attempt_index"])
        attempt = payload["attempt"]
        before_payload = load_json(checkpoint_path(IA_ROOT, "ia_only", attempt_index - 1))
        after_payload = load_json(checkpoint_path(IA_ROOT, "ia_only", attempt_index))
        before_quality = before_payload["quality"]
        after_quality = after_payload["quality"]
        components = component_improvements(before_quality, after_quality)
        recorded = {
            key: float(value)
            for key, value in attempt.get("objective_improvement_weighted_delta", {}).items()
        }
        if any(abs(components[key] - recorded.get(key, 0.0)) > 1e-9 for key in COMPONENTS):
            raise AssertionError(f"component mismatch at IA attempt {attempt_index}")
        gain = float(before_quality["weighted_substantive_value"] - after_quality["weighted_substantive_value"])
        if abs(gain - float(attempt["gain"])) > 1e-9 or abs(gain - sum(components.values())) > 1e-9:
            raise AssertionError(f"gain mismatch at IA attempt {attempt_index}")
        target_path = targeting_dir / f"attempt_{attempt_index:04d}_targeting.json.gz"
        snapshot = load_json(target_path)
        features = selection_features(snapshot)
        scope = scope_key(attempt["actual_target_scope"])
        scope_set = set(scope)
        changed_students = tuple(sorted(int(value) for value in attempt["changed_student_ids_exact"]))
        positions = [
            features["trace_by_student"].get(student_id, {}).get("position")
            for student_id in changed_students
        ]
        positions = sorted(position for position in positions if position is not None)
        inner = extract_inner(attempt)
        cumulative_gain += gain
        seen_students.update(scope_set)
        changed_edges = attempt.get("changed_source_decisions_exact", [])
        changed_requests = [
            {
                "source_key": edge.get("source_key"),
                "student_id": (edge.get("new") or edge.get("old") or [None])[0],
                "old_section_id": (edge.get("old") or [None, None])[1],
                "new_section_id": (edge.get("new") or [None, None])[1],
                "old_semester": (edge.get("old") or [None, None, None, None])[3],
                "new_semester": (edge.get("new") or [None, None, None, None])[3],
                "old_timeslot_id": (edge.get("old") or [None, None, None, None, None])[4],
                "new_timeslot_id": (edge.get("new") or [None, None, None, None, None])[4],
            }
            for edge in changed_edges
        ]
        row = {
            "attempt_index": attempt_index,
            "branch_elapsed_seconds": float(payload["branch_elapsed_seconds"]),
            "source_fingerprint_before": attempt["source_fingerprint_before"],
            "source_fingerprint_after": attempt["source_fingerprint_after"],
            "candidate_fingerprint": attempt.get("candidate_source_decision_fingerprint"),
            "scope": list(scope),
            "scope_fingerprint": snapshot.get("ia_selection", {}).get("scope_fingerprint"),
            "previous_scope": list(previous_scope) if previous_scope is not None else None,
            "scope_jaccard_previous": jaccard(scope, previous_scope) if previous_scope is not None else None,
            "retained_core": sorted(scope_set & set(previous_scope or ())),
            "entrants": sorted(scope_set - set(previous_scope or ())),
            "leavers": sorted(set(previous_scope or ()) - scope_set),
            "exact_scope_repeat_previous": scope == previous_scope,
            "gain": gain,
            "cumulative_gain": cumulative_gain,
            "value_after": float(after_quality["weighted_substantive_value"]),
            "changed_student_count": int(attempt["changed_student_count"]),
            "changed_student_ids": list(changed_students),
            "changed_positions": positions,
            "changed_source_decision_count": int(attempt["changed_source_decision_count"]),
            "changed_requests": changed_requests,
            "search_wall_seconds": float(attempt["solver_wall_time_seconds"]),
            "validation_wall_seconds": float(attempt["validation_seconds"]),
            "attempt_wall_seconds": float(attempt["elapsed_seconds"]),
            "branches": int(inner.get("branches", 0) or 0),
            "conflicts": int(inner.get("conflicts", 0) or 0),
            "solver_status": attempt.get("status"),
            "validation_classification": attempt.get("validation_classification"),
            "candidate_validated": bool(attempt.get("candidate_validated")),
            "adopted": bool(attempt.get("adopted")),
            "before_quality": before_quality,
            "after_quality": after_quality,
            "component_improvements": components,
            "trace_by_student": features.pop("trace_by_student"),
            "targeting_artifact_bytes": target_path.stat().st_size,
            "attempt_artifact_bytes": attempt_path.stat().st_size,
            "unique_targeted_students_to_date": len(seen_students),
            **features,
        }
        rows.append(row)
        previous_scope = scope
    return rows


def read_top_attempts():
    rows = []
    for attempt_path in sorted((TOP_ROOT / "branches" / "r16_only" / "attempts").glob("attempt_*.json")):
        payload = load_json(attempt_path)
        attempt_index = int(payload["attempt_index"])
        attempt = payload["attempt"]
        if not attempt.get("adopted"):
            continue
        before = load_json(checkpoint_path(TOP_ROOT, "r16_only", attempt_index - 1))["quality"]
        after = load_json(checkpoint_path(TOP_ROOT, "r16_only", attempt_index))["quality"]
        components = component_improvements(before, after)
        gain = float(before["weighted_substantive_value"] - after["weighted_substantive_value"])
        if abs(gain - sum(components.values())) > 1e-9:
            raise AssertionError(f"gain mismatch at TOP attempt {attempt_index}")
        inner = extract_inner(attempt)
        rows.append({
            "attempt_index": attempt_index,
            "branch_elapsed_seconds": float(payload["branch_elapsed_seconds"]),
            "scope": list(scope_key(attempt["actual_target_scope"])),
            "gain": gain,
            "component_improvements": components,
            "search_wall_seconds": float(attempt["solver_wall_time_seconds"]),
            "validation_wall_seconds": float(attempt["validation_seconds"]),
            "changed_student_count": int(attempt["changed_student_count"]),
            "changed_source_decision_count": int(attempt["changed_source_decision_count"]),
            "branches": int(inner.get("branches", 0) or 0),
            "conflicts": int(inner.get("conflicts", 0) or 0),
            "candidate_fingerprint": attempt.get("candidate_source_decision_fingerprint"),
        })
    return rows


def summarize_group(rows):
    result = {
        "n": len(rows),
        "total_gain": sum(row["gain"] for row in rows),
        "gain": distribution(row["gain"] for row in rows),
        "probability_gain_at_least_30": (
            sum(row["gain"] >= 30 for row in rows) / len(rows) if rows else None
        ),
        "probability_gain_at_least_60": (
            sum(row["gain"] >= 60 for row in rows) / len(rows) if rows else None
        ),
        "component_totals": {
            component: sum(row["component_improvements"][component] for row in rows)
            for component in COMPONENTS
        },
    }
    result["component_means"] = {
        key: value / len(rows) if rows else None
        for key, value in result["component_totals"].items()
    }
    return result


def mechanism_master_rows(rows):
    result = []
    for row in rows:
        result.append({
            "attempt_index": row["attempt_index"],
            "branch_elapsed_seconds": row["branch_elapsed_seconds"],
            "scope": compact_json(row["scope"]),
            "scope_jaccard_previous": row["scope_jaccard_previous"],
            "retained_core_count": len(row["retained_core"]),
            "exact_scope_repeat_previous": row["exact_scope_repeat_previous"],
            "gain": row["gain"],
            "changed_student_count": row["changed_student_count"],
            "changed_student_ids": compact_json(row["changed_student_ids"]),
            "changed_positions": compact_json(row["changed_positions"]),
            "changed_source_decision_count": row["changed_source_decision_count"],
            "category_improvement": row["component_improvements"]["course_category_diversity"],
            "sequence_improvement": row["component_improvements"]["course_sequence_preferences"],
            "difficulty_improvement": row["component_improvements"]["difficulty_balance"],
            "utilization_improvement": row["component_improvements"]["section_utilization_balance"],
            "semester_improvement": row["component_improvements"]["student_semester_load_balance"],
            "selected_total_positive_leverage": row["selected_total_positive_leverage"],
            "selected_mean_positive_leverage": row["selected_mean_positive_leverage"],
            "selected_positive_move_fact_count": row["selected_positive_move_fact_count"],
            "selected_alternate_destination_count": row["selected_alternate_destination_count"],
            "selected_delivery_group_count": row["selected_delivery_group_count"],
            "pressured_delivery_group_count": row["pressured_delivery_group_count"],
            "top_shadow_overlap_count": row["top_shadow_overlap_count"],
            "top_shadow_jaccard": row["top_shadow_jaccard"],
            "search_wall_seconds": row["search_wall_seconds"],
            "validation_wall_seconds": row["validation_wall_seconds"],
            "branches": row["branches"],
            "conflicts": row["conflicts"],
            "candidate_fingerprint": row["candidate_fingerprint"],
            "source_fingerprint_before": row["source_fingerprint_before"],
            "source_fingerprint_after": row["source_fingerprint_after"],
        })
    return result


def feature_associations(rows):
    features = (
        "selected_total_positive_leverage",
        "selected_mean_positive_leverage",
        "selected_positive_move_fact_count",
        "selected_alternate_destination_count",
        "selected_delivery_group_count",
        "pressured_delivery_group_count",
        "scope_jaccard_previous",
        "top_shadow_overlap_count",
        "top_shadow_jaccard",
    )
    broad = [row for row in rows if row["changed_student_count"] >= 3]
    high = [row for row in broad if row["gain"] >= 30]
    low = [row for row in broad if row["gain"] < 30]
    output = {}
    for feature in features:
        all_pairs = [(row[feature], row["gain"]) for row in rows if row[feature] is not None]
        broad_pairs = [(row[feature], row["gain"]) for row in broad if row[feature] is not None]
        high_values = [float(row[feature]) for row in high if row[feature] is not None]
        low_values = [float(row[feature]) for row in low if row[feature] is not None]
        output[feature] = {
            "all_attempt_spearman_gain": spearman(
                [float(item[0]) for item in all_pairs],
                [float(item[1]) for item in all_pairs],
            ),
            "broad_only_spearman_gain": spearman(
                [float(item[0]) for item in broad_pairs],
                [float(item[1]) for item in broad_pairs],
            ),
            "broad_high_median": median(high_values) if high_values else None,
            "broad_low_median": median(low_values) if low_values else None,
            "median_difference_high_minus_low": (
                median(high_values) - median(low_values)
                if high_values and low_values else None
            ),
            "common_language_probability_high_greater_than_low": common_language(
                high_values, low_values
            ),
        }
    return output


def write_breadth_analysis(root, rows):
    groups = {
        "all": rows,
        "one_student": [row for row in rows if row["changed_student_count"] == 1],
        "three_or_four_students": [row for row in rows if row["changed_student_count"] >= 3],
        "at_least_10_decisions": [row for row in rows if row["changed_source_decision_count"] >= 10],
        "fewer_than_10_decisions": [row for row in rows if row["changed_source_decision_count"] < 10],
        "gain_at_least_30": [row for row in rows if row["gain"] >= 30],
        "gain_at_least_60": [row for row in rows if row["gain"] >= 60],
        "broad_high": [row for row in rows if row["changed_student_count"] >= 3 and row["gain"] >= 30],
        "broad_low": [row for row in rows if row["changed_student_count"] >= 3 and row["gain"] < 30],
    }
    one_student_positions = Counter(
        row["changed_positions"][0]
        for row in groups["one_student"]
        if len(row["changed_positions"]) == 1
    )
    threshold_eligible = [
        row for row in rows
        if row["changed_student_count"] >= 3 and row["changed_source_decision_count"] >= 10
    ]
    payload = {
        "schema": "r16_ia_broad_high_low_mechanism_v1",
        "branch_classification": "E_EXPERIMENT_OPERATIONALLY_INVALID",
        "attempt_level_authority": "63 completed transitions remain fully validated strict authoritative mechanism evidence",
        "groups": {name: summarize_group(group) for name, group in groups.items()},
        "changed_student_distribution": dict(Counter(row["changed_student_count"] for row in rows)),
        "changed_decision_buckets": {
            "1-2": sum(row["changed_source_decision_count"] <= 2 for row in rows),
            "3-5": sum(3 <= row["changed_source_decision_count"] <= 5 for row in rows),
            "6-9": sum(6 <= row["changed_source_decision_count"] <= 9 for row in rows),
            ">=10": sum(row["changed_source_decision_count"] >= 10 for row in rows),
            ">=15": sum(row["changed_source_decision_count"] >= 15 for row in rows),
        },
        "one_student_changed_position_distribution": dict(sorted(one_student_positions.items())),
        "minimum_coordination_threshold_audit": {
            "rule": "changed_student_count >= 3 AND changed_source_decision_count >= 10",
            "eligible_attempts": [row["attempt_index"] for row in threshold_eligible],
            "all_gain_at_least_60_attempts_satisfy": all(
                row in threshold_eligible for row in groups["gain_at_least_60"]
            ),
            "gain_at_least_60_attempts": [row["attempt_index"] for row in groups["gain_at_least_60"]],
            "observational_only": True,
        },
        "pre_solve_feature_associations": feature_associations(rows),
        "pre_solve_discrimination_conclusion": (
            "No feature is qualified as a reliable discriminator: broad-high has only six observations and broad-low three; "
            "reported medians, rank correlations, and common-language effects are exploratory and unvalidated."
        ),
    }
    write_json(root / "ia_broad_high_vs_broad_low.json", payload)
    broad = payload["groups"]["three_or_four_students"]
    decisions = payload["groups"]["at_least_10_decisions"]
    low_decisions = payload["groups"]["fewer_than_10_decisions"]
    high = payload["groups"]["broad_high"]
    low = payload["groups"]["broad_low"]
    broad_high_rows = groups["broad_high"]
    broad_low_rows = groups["broad_low"]
    ordinary_rows = [row for row in rows if row["changed_student_count"] < 3 and row["gain"] < 30]
    lines = [
        "# IA broad/high versus broad/low",
        "",
        "The long branch remains **E — experiment operationally invalid** for a three-hour endpoint comparison. Its 63 completed transitions are nevertheless full-model-validated strict adoptions and are valid attempt-level mechanism evidence.",
        "",
        f"The audit recomputes 63 attempts, 1,098 points of partial gain, mean {mean(row['gain'] for row in rows):.6f}, median {median(row['gain'] for row in rows):.0f}, and maximum {max(row['gain'] for row in rows):.0f}. Counts at ≥30/≥60/≥90 are {sum(row['gain'] >= 30 for row in rows)}/{sum(row['gain'] >= 60 for row in rows)}/{sum(row['gain'] >= 90 for row in rows)}.",
        "",
        f"The nine 3–4-student moves produced {broad['total_gain']:.0f} points ({100*broad['total_gain']/1098:.1f}% of the prefix), mean {broad['gain']['mean']:.2f}, median {broad['gain']['median']:.0f}; {100*broad['probability_gain_at_least_30']:.1f}% reached 30 and {100*broad['probability_gain_at_least_60']:.1f}% reached 60. The eight ≥10-decision moves produced {decisions['total_gain']:.0f} points, mean {decisions['gain']['mean']:.2f}, median {decisions['gain']['median']:.0f}; the 55 smaller moves averaged {low_decisions['gain']['mean']:.2f} and none reached 60.",
        "",
        f"Broad/high (≥30) contributed category {high['component_totals']['course_category_diversity']:+.0f}, difficulty {high['component_totals']['difficulty_balance']:+.0f}, utilization {high['component_totals']['section_utilization_balance']:+.0f}, semester {high['component_totals']['student_semester_load_balance']:+.0f}, and sequence {high['component_totals']['course_sequence_preferences']:+.0f}. Broad/low contributed category {low['component_totals']['course_category_diversity']:+.0f}, difficulty {low['component_totals']['difficulty_balance']:+.0f}, utilization {low['component_totals']['section_utilization_balance']:+.0f}, semester {low['component_totals']['student_semester_load_balance']:+.0f}, and sequence {low['component_totals']['course_sequence_preferences']:+.0f}.",
        "",
        "Breadth is strongly associated with realized gain here, but it is not sufficient: attempts 32, 56, and 63 are broad/low counterexamples. The frozen ≥3-student and ≥10-decision rule includes every observed ≥60 result, but that is an in-sample observational fact—not evidence that forcing those counts causes higher full-v2 value.",
        "",
        f"Solver branching is consistent with a richer broad-move landscape but does not distinguish quality: median branches/conflicts are {median(row['branches'] for row in broad_high_rows):.1f}/{median(row['conflicts'] for row in broad_high_rows):.1f} for broad/high, {median(row['branches'] for row in broad_low_rows):.1f}/{median(row['conflicts'] for row in broad_low_rows):.1f} for broad/low, and {median(row['branches'] for row in ordinary_rows):.1f}/{median(row['conflicts'] for row in ordinary_rows):.1f} for ordinary narrow outcomes. Branches and conflicts are post-solve observations; at most they motivate testing additional refinement effort after a candidate is found, not pre-solve targeting coefficients.",
        "",
        f"For one-student repairs, changed construction-position counts are {dict(sorted(one_student_positions.items()))}. This tests the position-4 observation directly; later-selected students can be the moved repair while earlier cluster members remain structural context, but the trace cannot establish that context causally enabled the move.",
        "",
        "No pre-solve feature is promoted as a discriminator. The JSON reports medians, Spearman correlations, and common-language effect sizes transparently; the broad/high and broad/low samples are only six and three observations and have no held-out validation.",
    ]
    (root / "ia_broad_high_vs_broad_low.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return payload


def write_component_outputs(root, rows, breadth_payload):
    component_rows = []
    for row in rows:
        record = {
            "attempt_index": row["attempt_index"],
            "branch_elapsed_seconds": row["branch_elapsed_seconds"],
            "gain": row["gain"],
            "changed_student_count": row["changed_student_count"],
            "changed_source_decision_count": row["changed_source_decision_count"],
            "broad": row["changed_student_count"] >= 3,
            "at_least_10_decisions": row["changed_source_decision_count"] >= 10,
        }
        for component in COMPONENTS:
            record[f"{component}_improvement"] = row["component_improvements"][component]
            record[f"{component}_before"] = row["before_quality"]["components"][component]["weighted_normalized_contribution"]
            record[f"{component}_after"] = row["after_quality"]["components"][component]["weighted_normalized_contribution"]
        component_rows.append(record)
    write_csv(root / "ia_component_by_attempt.csv", component_rows)
    groups = breadth_payload["groups"]
    notable = {row["attempt_index"]: row for row in rows if row["attempt_index"] in DEEP_ATTEMPTS}
    lines = [
        "# IA component trade-off analysis",
        "",
        "All component values below are reconstructed from the authoritative before/after checkpoint quality records. Positive means the weighted v2 penalty improved; negative means that component worsened. The five components sum exactly to each adopted gain.",
        "",
        "| Attempt | Gain | Students | Decisions | Category | Difficulty | Utilization | Semester | Sequence |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for index in DEEP_ATTEMPTS:
        row = notable[index]
        c = row["component_improvements"]
        lines.append(
            f"| {index} | {row['gain']:+.0f} | {row['changed_student_count']} | {row['changed_source_decision_count']} | "
            f"{c['course_category_diversity']:+.0f} | {c['difficulty_balance']:+.0f} | "
            f"{c['section_utilization_balance']:+.0f} | {c['student_semester_load_balance']:+.0f} | "
            f"{c['course_sequence_preferences']:+.0f} |"
        )
    broad = groups["three_or_four_students"]
    one = groups["one_student"]
    lines += [
        "",
        f"Across all nine broad moves, utilization contributed {broad['component_totals']['section_utilization_balance']:+.0f} while category plus difficulty contributed {broad['component_totals']['course_category_diversity'] + broad['component_totals']['difficulty_balance']:+.0f}. Across 53 one-student moves, utilization contributed {one['component_totals']['section_utilization_balance']:+.0f} and category plus difficulty {one['component_totals']['course_category_diversity'] + one['component_totals']['difficulty_balance']:+.0f}.",
        "",
        "The largest broad candidates are not a separate multi-component species: their gains are still predominantly utilization improvements. Some also avoid or reverse category/difficulty costs, which is why full-v2 optimization—not breadth alone—is the relevant next causal question. The broad/low examples demonstrate that moving many students and decisions can still yield only +6 or +12 after cross-component accounting.",
        "",
        "Sequence and semester movement remain zero throughout this IA prefix. That is a factual lack of differentiation, not a reason to remove those components from the frozen objective.",
    ]
    (root / "ia_component_tradeoff_analysis.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_scope_outputs(root, rows):
    scope_rows = []
    for row in rows:
        scope_rows.append({
            "attempt_index": row["attempt_index"],
            "branch_elapsed_seconds": row["branch_elapsed_seconds"],
            "gain": row["gain"],
            "scope": compact_json(row["scope"]),
            "previous_scope": compact_json(row["previous_scope"]) if row["previous_scope"] else "",
            "scope_jaccard_previous": row["scope_jaccard_previous"],
            "retained_core": compact_json(row["retained_core"]),
            "entrants": compact_json(row["entrants"]),
            "leavers": compact_json(row["leavers"]),
            "exact_scope_repeat_previous": row["exact_scope_repeat_previous"],
            "changed_students": compact_json(row["changed_student_ids"]),
            "changed_positions": compact_json(row["changed_positions"]),
            "unique_targeted_students_to_date": row["unique_targeted_students_to_date"],
        })
    write_csv(root / "ia_scope_evolution_analysis.csv", scope_rows)

    by_index = {row["attempt_index"]: row for row in rows}
    lines = [
        "# IA high-gain scope sequences",
        "",
        "These are observed transitions on changing authoritative incumbents. Scope repetition after an adoption is not repetition of the same exact search state. No sequence below proves an unlock effect.",
    ]
    for center in HIGH_SEQUENCE_CENTERS:
        lines += ["", f"## Around attempt {center}", ""]
        for index in range(max(1, center - 2), min(len(rows), center + 2) + 1):
            row = by_index[index]
            lines.append(
                f"- Attempt {index}: scope {row['scope']}; gain {row['gain']:+.0f}; previous-scope Jaccard "
                f"{row['scope_jaccard_previous'] if row['scope_jaccard_previous'] is not None else 'n/a'}; "
                f"retained {row['retained_core']}; entrants {row['entrants']}; leavers {row['leavers']}; "
                f"changed students {row['changed_student_ids']} at construction positions {row['changed_positions']}."
            )
        center_row = by_index[center]
        lines += ["", "Changed requests and destinations:", ""]
        for edge in center_row["changed_requests"]:
            lines.append(
                f"- `{edge['source_key']}` student {edge['student_id']}: section {edge['old_section_id']} → "
                f"{edge['new_section_id']}, semester {edge['old_semester']} → {edge['new_semester']}, "
                f"timeslot {edge['old_timeslot_id']} → {edge['new_timeslot_id']}."
            )
        lines += ["", "Cluster-construction facts for changed students:", ""]
        for student_id in center_row["changed_student_ids"]:
            trace = center_row["trace_by_student"].get(student_id, {})
            lines.append(
                f"- Student {student_id}: position {trace.get('position')}, selected leverage "
                f"{trace.get('positive_leverage')}, positive moves {trace.get('positive_move_count')}, "
                f"prior-overlap count {trace.get('overlap')}, delivery groups {trace.get('delivery_group_ids')}."
            )
    row51, row52 = by_index[51], by_index[52]
    lines += [
        "",
        "## Same-scope changed-incumbent example",
        "",
        f"Attempts 51 and 52 use the same canonical scope ({row51['scope'] == row52['scope']}): attempt 51 returned +{row51['gain']:.0f} with {row51['changed_student_count']} student/{row51['changed_source_decision_count']} decision, then attempt 52 returned +{row52['gain']:.0f} with {row52['changed_student_count']} students/{row52['changed_source_decision_count']} decisions. The source fingerprint changed between them, so this supports testing changed-incumbent scope reuse; it does not prove the +12 move caused the later +66 move.",
        "",
        "## Attempts 60–63",
        "",
    ]
    for index in range(60, 64):
        row = by_index[index]
        lines.append(
            f"- Attempt {index}: scope {row['scope']}, gain {row['gain']:+.0f}, "
            f"{row['changed_student_count']} students/{row['changed_source_decision_count']} decisions, "
            f"exact previous-scope repeat {row['exact_scope_repeat_previous']}."
        )
    lines += [
        "",
        "The four attempts use the same student set on successive incumbents. They continue to produce strict gains, but the final +6 broad/16-decision result is direct evidence that changed-incumbent repetition and breadth are not sufficient indicators of high full-v2 value.",
    ]
    (root / "ia_high_gain_scope_sequences.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_branch_conflicts(root, rows):
    output = []
    for row in rows:
        if row["changed_student_count"] >= 3 and row["gain"] >= 30:
            category = "broad_high"
        elif row["changed_student_count"] >= 3:
            category = "broad_low"
        elif row["changed_student_count"] == 1 and row["gain"] >= 30:
            category = "high_gain_one_student"
        else:
            category = "ordinary_one_student_or_two_student"
        output.append({
            "attempt_index": row["attempt_index"],
            "category": category,
            "gain": row["gain"],
            "changed_student_count": row["changed_student_count"],
            "changed_source_decision_count": row["changed_source_decision_count"],
            "branches": row["branches"],
            "conflicts": row["conflicts"],
            "search_wall_seconds": row["search_wall_seconds"],
            "validation_wall_seconds": row["validation_wall_seconds"],
            "candidate_fingerprint": row["candidate_fingerprint"],
        })
    write_csv(root / "ia_branch_conflict_analysis.csv", output)
    return {
        category: {
            "branches": distribution(row["branches"] for row in output if row["category"] == category),
            "conflicts": distribution(row["conflicts"] for row in output if row["category"] == category),
            "search_wall": distribution(row["search_wall_seconds"] for row in output if row["category"] == category),
        }
        for category in sorted({row["category"] for row in output})
    }


def prefix_snapshot(rows, minute):
    selected = [row for row in rows if row["branch_elapsed_seconds"] <= minute * 60]
    gains = [row["gain"] for row in selected]
    search_seconds = sum(row["search_wall_seconds"] for row in selected)
    validation_seconds = sum(row["validation_wall_seconds"] for row in selected)
    scopes = [scope_key(row["scope"]) for row in selected]
    frequencies = Counter(student for scope in scopes for student in scope)
    total_targets = sum(frequencies.values())
    hhi = (
        sum((count / total_targets) ** 2 for count in frequencies.values())
        if total_targets else None
    )
    return {
        "attempts": len(selected),
        "gain": sum(gains),
        "gain_per_adoption": mean(gains) if gains else None,
        "attempts_per_hour": len(selected) / (minute / 60),
        "search_minutes": search_seconds / 60,
        "validation_minutes": validation_seconds / 60,
        "gain_per_search_minute": sum(gains) / (search_seconds / 60) if search_seconds else None,
        "gain_per_validation_minute": sum(gains) / (validation_seconds / 60) if validation_seconds else None,
        "components": {
            component: sum(row["component_improvements"][component] for row in selected)
            for component in COMPONENTS
        },
        "component_gain_per_search_minute": {
            component: (
                sum(row["component_improvements"][component] for row in selected) / (search_seconds / 60)
                if search_seconds else None
            )
            for component in COMPONENTS
        },
        "unique_scopes": len(set(scopes)),
        "duplicate_scope_observations_beyond_first": len(scopes) - len(set(scopes)),
        "targeting_hhi": hhi,
        "effective_targeted_student_count": 1 / hhi if hhi else None,
    }


def write_prefix_analysis(root, ia_rows, top_rows):
    snapshots = []
    for minute in PREFIX_MINUTES:
        ia = prefix_snapshot(ia_rows, minute)
        top = prefix_snapshot(top_rows, minute)
        snapshots.append({"minute": minute, "ia": ia, "top": top})
    lines = [
        "# IA versus TOP matched-prefix decomposition",
        "",
        "The IA branch remains **E — experiment operationally invalid** for the requested three-hour endpoint. Every row through minute 165 uses completed, validated authoritative adoptions from each branch; no IA endpoint is supplied and no policy winner is claimed.",
        "",
        "| Minute | IA attempts | TOP attempts | IA gain | TOP gain | IA gain/adoption | TOP gain/adoption | IA util | TOP util | IA cat+diff | TOP cat+diff |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in snapshots:
        ia, top = item["ia"], item["top"]
        ia_cd = ia["components"]["course_category_diversity"] + ia["components"]["difficulty_balance"]
        top_cd = top["components"]["course_category_diversity"] + top["components"]["difficulty_balance"]
        lines.append(
            f"| {item['minute']} | {ia['attempts']} | {top['attempts']} | {ia['gain']:.0f} | {top['gain']:.0f} | "
            f"{ia['gain_per_adoption']:.2f} | {top['gain_per_adoption']:.2f} | "
            f"{ia['components']['section_utilization_balance']:+.0f} | {top['components']['section_utilization_balance']:+.0f} | "
            f"{ia_cd:+.0f} | {top_cd:+.0f} |"
        )
    at165 = snapshots[-1]
    ia165, top165 = at165["ia"], at165["top"]
    lines += [
        "",
        f"At 165 minutes, IA has {ia165['gain']:.0f} gain from {ia165['attempts']} adoptions ({ia165['gain_per_adoption']:.2f} each) and TOP {top165['gain']:.0f} from {top165['attempts']} ({top165['gain_per_adoption']:.2f} each). IA improves utilization by {ia165['components']['section_utilization_balance']:.0f}, {ia165['components']['section_utilization_balance'] - top165['components']['section_utilization_balance']:+.0f} more than TOP. TOP improves category+difficulty by {(top165['components']['course_category_diversity'] + top165['components']['difficulty_balance']):.0f}, {(top165['components']['course_category_diversity'] + top165['components']['difficulty_balance']) - (ia165['components']['course_category_diversity'] + ia165['components']['difficulty_balance']):+.0f} more than IA. That cross-component difference explains the observed 78-point TOP lead at that prefix.",
        "",
        "The trajectory is not a uniform IA trade: IA leads total gain through 90 minutes, trails at 105/120, leads again at 135/150, and trails at 165. IA does generate more cumulative utilization improvement at every listed prefix; TOP's late jackpots add category and difficulty value that IA does not capture in the same prefix.",
        "",
        f"At 165 minutes IA has {ia165['unique_scopes']}/{ia165['attempts']} unique scopes, HHI {ia165['targeting_hhi']:.5f}, and effective targeted-student count {ia165['effective_targeted_student_count']:.2f}; TOP has {top165['unique_scopes']}/{top165['attempts']}, HHI {top165['targeting_hhi']:.5f}, and effective count {top165['effective_targeted_student_count']:.2f}. Across IA's full valid prefix, the corresponding values are {len(set(scope_key(row['scope']) for row in ia_rows))}/{len(ia_rows)}, HHI {prefix_snapshot(ia_rows, 180)['targeting_hhi']:.5f}, and effective count {prefix_snapshot(ia_rows, 180)['effective_targeted_student_count']:.2f}.",
        "",
        "The earlier small-cohort description of IA as intrinsically concentrated is contradicted as a universal claim by this self-trajectory: IA rotated through 53 distinct scopes and broadened its effective targeted population substantially. Concentration is state- and horizon-dependent, not an intrinsic fixed property established by current evidence.",
        "",
        "Search and validation productivity are recorded in the structured snapshots used to build this report. Component-specific rates retain negative values where a component worsened. Comparable gain/adoption does not imply comparable schedules: the branches have different scope sequences, component composition, and jackpot timing, and eight-worker CP-SAT is nondeterministic.",
    ]
    (root / "ia_vs_top_prefix_decomposition.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return snapshots


def linear_slope(xs, ys):
    if len(xs) < 2:
        return None
    x_mean, y_mean = mean(xs), mean(ys)
    denominator = sum((x - x_mean) ** 2 for x in xs)
    return sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys)) / denominator if denominator else None


def resource_audit(root, ia_rows):
    resource_path = IA_ROOT / "branches" / "ia_only" / "resource_samples.jsonl"
    samples = [json.loads(line) for line in resource_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    baselines = []
    for row in ia_rows:
        later = [sample for sample in samples if float(sample["elapsed_seconds"]) >= row["branch_elapsed_seconds"]]
        if later:
            sample = later[0]
            baselines.append({
                "attempt": row["attempt_index"],
                "rss": int(sample["tree_rss_bytes"]),
                "uss": int(sample["tree_uss_bytes"]),
                "available": int(sample["system_available_memory_bytes"]),
            })
    uss_transitions = [baselines[index]["uss"] - baselines[index - 1]["uss"] for index in range(1, len(baselines))]
    returns = sum(delta < -(16 * 1024 * 1024) for delta in uss_transitions)
    nondecreasing = sum(delta >= -(16 * 1024 * 1024) for delta in uss_transitions)
    targeting_sizes = [row["targeting_artifact_bytes"] for row in ia_rows]
    branch_dir = IA_ROOT / "branches" / "ia_only"
    all_targeting_paths = list((branch_dir / "targeting").glob("*.json.gz"))
    attempt_sizes = [row["attempt_artifact_bytes"] for row in ia_rows]
    phase_size = (branch_dir / "phase_events.jsonl").stat().st_size
    checkpoint_paths = list((branch_dir / "checkpoints").glob("*"))
    checkpoint_size = sum(path.stat().st_size for path in checkpoint_paths if path.is_file())
    target_size = sum(path.stat().st_size for path in all_targeting_paths)
    attempt_size = sum(attempt_sizes)
    last = baselines[-1]
    first = baselines[0]
    slope = linear_slope([row["attempt"] for row in baselines], [row["uss"] for row in baselines])
    lines = [
        "# IA resource and observability audit",
        "",
        "The supervisor stopped the branch at approximately 10,365.442 seconds after the resource guard fired. This preserves classification E for the three-hour experiment. It does not invalidate the 63 already completed and validated transitions.",
        "",
        f"The five-second stream contains {len(samples):,} samples. Tree RSS rose from {samples[0]['tree_rss_bytes']/2**30:.2f} GiB to a maximum of {max(s['tree_rss_bytes'] for s in samples)/2**30:.2f} GiB; tree USS rose from {samples[0]['tree_uss_bytes']/2**30:.2f} GiB to {max(s['tree_uss_bytes'] for s in samples)/2**30:.2f} GiB; available system memory fell from {samples[0]['system_available_memory_bytes']/2**30:.2f} GiB to a minimum of {min(s['system_available_memory_bytes'] for s in samples)/2**30:.2f} GiB.",
        "",
        f"At first samples after completed attempts, USS increased by {(last['uss']-first['uss'])/2**30:.2f} GiB across the prefix, with a fitted slope of {(slope or 0)/2**20:.2f} MiB per attempt. {nondecreasing} of {len(uss_transitions)} transitions were nondecreasing within a 16 MiB tolerance; only {returns} showed a drop larger than 16 MiB. The persisted monitor cannot attribute this retention to one Python/native subsystem, and it does not expose enough phase identity to prove memory returned immediately after CP-SAT.",
        "",
        "## Persisted volume",
        "",
        f"- 63 attempt JSON files: {attempt_size/2**30:.2f} GiB (mean {mean(attempt_sizes)/2**20:.2f} MiB).",
        f"- `phase_events.jsonl`: {phase_size/2**30:.2f} GiB.",
        f"- {len(all_targeting_paths)} compressed targeting snapshots (63 completed plus one in-flight): {target_size/2**20:.2f} MiB (completed-attempt mean {mean(targeting_sizes)/2**20:.2f} MiB).",
        f"- Checkpoints: {checkpoint_size/2**20:.2f} MiB.",
        f"- Targeting-snapshot size versus attempt-index Spearman: {spearman(list(range(1, len(targeting_sizes)+1)), targeting_sizes):.3f}.",
        "",
        "The dominant avoidable observability cost is duplication: complete targeting/selector payloads, including roughly 1,400 candidate-population records, are embedded in every large attempt JSON and again emitted into the uncompressed phase ledger. The compressed standalone targeting records are comparatively small on disk, although constructing and serializing their complete populations can still create transient Python buffers.",
        "",
        "## Attribution boundary",
        "",
        "The evidence is consistent with cumulative retention or allocator high-water behavior across repeated model cloning/native solves, trusted context, large targeting objects, and JSON serialization. Process-tree totals cannot separate CP-SAT/native model memory, trusted-context retention, callback payloads, serialization buffers, Python allocator behavior, or supervisor overhead. It would be incorrect to attribute the growth to IA targeting alone.",
        "",
        "## Required research telemetry change",
        "",
        "Before any heavy fixed-scope study, persist one compact targeting summary per cell: selected four records, aggregate population counts/distributions, fingerprints, and the exact scope. Do not embed the full candidate population in attempt JSON or phase events. Keep standalone full-population snapshots only for pre-registered sampled cells and anomaly cases. Stream five-second process-tree resources externally, keep attempt/checkpoint writes atomic, and record phase names in the resource stream so post-solve return can be measured. Add an artifact-size preflight and a small-process memory smoke test. These are research-runner/telemetry changes only; production targeting behavior must remain unchanged.",
    ]
    (root / "ia_resource_overhead_audit.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {
        "sample_count": len(samples),
        "maximum_tree_rss_bytes": max(s["tree_rss_bytes"] for s in samples),
        "maximum_tree_uss_bytes": max(s["tree_uss_bytes"] for s in samples),
        "minimum_available_memory_bytes": min(s["system_available_memory_bytes"] for s in samples),
        "uss_slope_bytes_per_attempt": slope,
        "baseline_nondecreasing_within_16mib": nondecreasing,
        "baseline_drops_over_16mib": returns,
        "attempt_json_total_bytes": attempt_size,
        "phase_events_bytes": phase_size,
        "targeting_total_bytes": target_size,
        "checkpoint_total_bytes": checkpoint_size,
    }


def source_for_cell(lineage, attempt_index):
    root = IA_ROOT if lineage == "ia" else TOP_ROOT
    branch = "ia_only" if lineage == "ia" else "r16_only"
    path = checkpoint_path(root, branch, attempt_index - 1)
    payload = load_json(path)
    return {
        "path": str(path),
        "sha256": sha256(path),
        "source_decision_fingerprint": payload["source_decision_fingerprint"],
        "substantive_value": payload["quality"]["weighted_substantive_value"],
    }


def design_payload(ia_rows, top_rows, branch_stats):
    ia = {row["attempt_index"]: row for row in ia_rows}
    top = {row["attempt_index"]: row for row in top_rows}
    cell_specs = [
        ("ia", 13, "IA +90 coordinated positive case"),
        ("ia", 32, "IA +6 broad/16-decision negative control"),
        ("ia", 51, "IA +12 one-student predecessor on the scope reused at attempt 52"),
        ("ia", 52, "IA +66 coordinated changed-incumbent repeat of attempt-51 scope"),
        ("top", 60, "TOP high-variance state"),
        ("top", 27, "TOP reproducible jackpot scope"),
    ]
    cells = []
    for lineage, attempt_index, rationale in cell_specs:
        row = (ia if lineage == "ia" else top)[attempt_index]
        cells.append({
            "cell_id": f"{lineage}_attempt_{attempt_index:04d}",
            "lineage": lineage,
            "historical_attempt_index": attempt_index,
            "authoritative_source": source_for_cell(lineage, attempt_index),
            "fixed_scope": row["scope"],
            "observed_control_gain": row["gain"],
            "observed_changed_students": row["changed_student_count"],
            "observed_changed_decisions": row["changed_source_decision_count"],
            "rationale": rationale,
            "historical_result_is_context_not_a_cell_outcome": True,
        })
    treatments = [
        {
            "id": "control_first_qualifying",
            "semantics": "Existing strict seed_value-1 satisfiability probe; return the first complete qualifying candidate.",
        },
        {
            "id": "minimum_coordination",
            "semantics": "The same satisfiability probe plus changed_student_count >= 3 AND changed_source_decision_count >= 10; retain existing <=4 student and <=16 decision bounds.",
            "infeasible_interpretation": "resolved no-candidate result for that treatment/cell, not evidence that breadth is quality",
        },
        {
            "id": "iterative_strict_bound_refinement",
            "semantics": "Build one fixed-scope clone. Find the first qualifying candidate, then repeatedly add target_expression <= best_value-1 and solve with a fresh CpSolver/hints while cumulative native Solve wall remains below 300 seconds. Retain the best complete candidate; validate only the final reportable candidate.",
            "budget_accounting": "Every CpSolver.Solve wall counts against one cumulative 300-second CP-SAT budget; preparation/extraction telemetry is separate total wall; final validation has a separate 180-second allowance.",
        },
        {
            "id": "direct_exact_v2_optimization",
            "semantics": "On the same fixed-scope clone, preserve higher objective tiers, keep target_expression <= seed_value-1, and Minimize the exact recorded v2 target_expression for up to 300 seconds. Return the best complete feasible candidate at timeout or earlier only on proven optimality.",
            "eligibility": "eligible_after_implementation_parity_gate",
        },
    ]
    return {
        "schema": "r16_fixed_scope_search_semantics_design_v1",
        "executed": False,
        "recommendation": "A_FIXED_SCOPE_SEARCH_SEMANTICS_SCREEN",
        "scientific_question": "What should CP-SAT do inside an already fixed R16/S4 neighborhood?",
        "direct_v2_semantics_audit": {
            "classification": "EXACTLY_REPRESENTABLE_AND_ELIGIBLE_AFTER_PARITY_TESTS",
            "evidence": [
                "objective_semantics.py defines deterministic integer floor normalization and weighted normalized penalties",
                "core.py constructs those same normalized IntVars with AddDivisionEquality and stores the complete weighted v2 soft-tier term_specs",
                "substantive_probe.py reconstructs the exact target_expression from term_specs, fixes all preceding lexicographic tiers, preserves the full cloned model, and already replaces the objective when minimizing one component",
            ],
            "required_implementation_gate": [
                "add an explicit mutually exclusive minimize_substantive_tier mode that calls Minimize(target_expression)",
                "prove seed and candidate target-expression values equal evaluate_student_assignment_quality weighted_substantive_value on v2 fixtures and selected target states",
                "prove all completion, higher-tier, fixed-scope, R16/S4, hint, and validation-authority contracts are unchanged",
                "report BestObjectiveBound/gap only for the direct optimization treatment",
                "do not approximate normalization or optimize raw utilization alone",
            ],
        },
        "common_contract": {
            "objective_semantics": "v2 balanced unchanged",
            "hard_constraints": "unchanged full model",
            "scope": "exact frozen four-student R16/S4 scope within matched cell",
            "seed": 101,
            "search_workers": 8,
            "validation_workers": 1,
            "hints": "current incumbent-derived hints unchanged",
            "cp_sat_search_budget_seconds": 300,
            "independent_validation_allowance_seconds": 180,
            "candidate_authority": "complete full-model validation and strict lower-v2 gain only",
            "process_isolation": "clean process per treatment/repeat cell",
            "resource_telemetry": "compact summaries plus external five-second process-tree stream; no full population embedded in attempt or phase events",
        },
        "treatments": treatments,
        "scope_cells": cells,
        "repeat_count_per_treatment_cell": 3,
        "repeat_rationale": "Three clean repeats are the minimum used to expose observed eight-worker candidate variance without treating same-seed parallel CP-SAT as deterministic.",
        "total_cell_count": len(cells) * len(treatments) * 3,
        "staging": "Run this 72-cell six-scope screen only. Do not add combination treatments. A later holdout stage may add ordinary deterministic TOP and another IA >=60 scope only if a treatment passes the frozen screen gate.",
        "record_per_cell": [
            "first qualifying latency where meaningful", "final CP-SAT wall", "solver status",
            "best substantive candidate value", "full-model validation status", "validated gain",
            "changed students", "changed decisions", "five weighted component improvements",
            "candidate fingerprint", "branches", "conflicts", "total wall", "gain/search-minute",
        ],
        "refinement_fields": [
            "successful threshold tightenings", "initial candidate gain", "final candidate gain",
            "incremental refinement gain", "time after first candidate",
        ],
        "direct_optimization_fields": ["best objective", "best bound", "relative/absolute gap", "optimal/feasible/unknown status"],
        "classification_criteria": {
            "unit": "within-source/scope median across three clean repeats; unresolved or invalid candidates have no authoritative gain",
            "practical_gain_threshold_points": 12,
            "advance_search_semantics": "A treatment advances only if its paired median gain exceeds control by >=12 on at least two of six scopes, is worse by >=12 on no more than one scope, and has at least 5/6 cells with >=2 validated repeats.",
            "minimum_coordination_success": "In addition to the advance rule, must improve total v2 rather than utilization alone: pooled median non-utilization component delta versus control may not be negative by more than 6 points, and broad-low validated outcomes may not exceed control by two or more scopes.",
            "direct_vs_iterative": "Direct wins the engineering choice if it exceeds iterative by >=12 median points on at least two scopes, loses by >=12 on no more than one, and has no worse validation reliability. Reverse criteria select iterative. Otherwise they remain empirically tied/inconclusive.",
            "ordinary_scope_harm_gate": "The attempt-51 easy scope may not lose a validated qualifying result in two or more repeats; total wall is reported separately from gain because refinement intentionally uses the full search budget.",
            "operational_invalid": "Any source/scope mismatch, semantic parity failure, authority failure, uncontrolled budget, resource stop, or incomplete telemetry invalidates the affected cell; two or more invalid cells in one treatment make that treatment inconclusive.",
            "no_general_policy_claim": True,
        },
        "observed_branch_complexity_context": branch_stats,
        "excluded_initially": [
            "minimum coordination plus refinement", "minimum coordination plus direct optimization",
            "hint treatment", "target release", "different target policy", "different search budget",
        ],
        "canonical_document": {
            "recommended": True,
            "path": "docs/STUDENT_ASSIGNMENT_WITHIN_SCOPE_SEARCH.md",
            "owns": [
                "first-qualifying semantics", "minimum-coordination research contracts",
                "iterative within-scope refinement", "direct exact-v2 within-scope optimization",
                "equal search-budget accounting", "within-scope telemetry and promotion gates",
            ],
            "links_without_duplication": {
                "objective_semantics": "STUDENT_ASSIGNMENT_OBJECTIVE_SEMANTICS.md remains authoritative",
                "validation": "STUDENT_ASSIGNMENT_VALIDATION.md remains authoritative",
                "target_selection": "STUDENT_ASSIGNMENT_TARGET_SELECTION.md owns how the four students are chosen",
                "operator_selection": "STUDENT_ASSIGNMENT_ADAPTIVE_SEARCH.md owns which operator runs",
                "hints": "existing hint documentation owns hint construction and identity",
            },
            "created_in_this_audit": False,
        },
        "policy_viability_statement": "Both TOP and IA are viable research-only long-horizon scope generators and appear complementary. IA's exact three-hour branch is operationally invalid, so this is not an endpoint superiority or production-promotion claim.",
    }


def write_design(root, payload):
    write_json(root / "fixed_scope_search_semantics_design.json", payload)
    lines = [
        "# Fixed-scope search-semantics screen",
        "",
        "Recommendation: **A — FIXED-SCOPE SEARCH-SEMANTICS SCREEN**. This document designs the experiment only; no CP-SAT cell was run.",
        "",
        "## Why this is next",
        "",
        "TOP and IA have both produced useful R16/S4 scopes. The remaining uncertainty is candidate selection inside a fixed scope. IA's breadth association is substantial but includes broad/low counterexamples, so the study isolates breadth from refinement and exact-objective search instead of creating a combined treatment.",
        "",
        "Direct v2 optimization is eligible after an implementation parity gate. The model already contains the exact integer-normalized, counselor-weighted v2 expression, and the probe already reconstructs that expression while fixing higher tiers and preserving all hard and scope constraints. The future implementation should minimize that exact expression; no approximate objective is permitted.",
        "",
        "## Frozen screen",
        "",
        f"Six exact source/scope cells × four treatments × three clean repeats = **{payload['total_cell_count']} cells**. Every cell uses seed 101, eight search workers, current hints, a 300-second total CP-SAT search budget, and a separate 180-second one-worker validation allowance.",
        "",
        "Treatments:",
        "",
    ]
    for treatment in payload["treatments"]:
        lines.append(f"- `{treatment['id']}`: {treatment['semantics']}")
    lines += ["", "Scopes:", ""]
    for cell in payload["scope_cells"]:
        lines.append(
            f"- `{cell['cell_id']}` scope {cell['fixed_scope']}, historical +{cell['observed_control_gain']:.0f}: {cell['rationale']}. Source `{cell['authoritative_source']['path']}`."
        )
    lines += [
        "",
        "The six cells deliberately include coordinated upside, a broad low-gain counterexample, an easy one-student predecessor, a changed-incumbent same-scope coordinated result, a high-variance TOP state, and a reproducible TOP jackpot. Combination treatments are excluded. A later holdout stage—not this screen—can add an ordinary deterministic TOP scope and another IA high-gain scope.",
        "",
        "## Decision rules",
        "",
        "A treatment advances only if its paired three-repeat median beats control by at least 12 v2 points on at least two scopes, loses by that amount on no more than one, and has at least two validated repeats in at least five scopes. Minimum coordination must also avoid converting utilization gains into material non-utilization losses. Direct and iterative refinement use the same ±12 paired rule against each other, with validation reliability as a guard. Operational or semantic-contract failures are invalid cells, never zero-quality observations.",
        "",
        "## Research telemetry gate",
        "",
        "Before launch, remove full candidate-population duplication from attempt JSON and phase events, retain compact selected/aggregate/fingerprint records, stream five-second process resources externally, and pass an artifact-size/memory smoke test. This is a preflight requirement for A, not a reason to run more targeting or hint research first.",
        "",
        "## Documentation boundary",
        "",
        "A dedicated `STUDENT_ASSIGNMENT_WITHIN_SCOPE_SEARCH.md` is justified once implementation begins. It should own search behavior after a scope is fixed, equal-budget semantics, telemetry, and promotion gates. It should link to—never redefine—Objective Semantics, validation authority, target selection, operator selection, or hint identity.",
        "",
        "## Viability statement",
        "",
        "Current evidence supports **both TOP and IA as complementary research-only long-horizon scope generators**. It does not support a valid three-hour IA endpoint, a universal target-policy ranking, or production promotion.",
    ]
    (root / "fixed_scope_search_semantics_design.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def make_output_root(explicit=None):
    if explicit is not None:
        root = Path(explicit).resolve()
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        suffix = hashlib.sha256(f"{stamp}-{os.getpid()}".encode()).hexdigest()[:8]
        root = OUTPUT_PARENT / f"v2_r16_within_scope_semantics_audit_{stamp}_{suffix}"
    root.mkdir(parents=True, exist_ok=False)
    return root


def seal(root):
    entries = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name not in {"artifact_hashes.sha256", "SEALED"}:
            entries.append(f"{sha256(path)}  {path.relative_to(root).as_posix()}")
    manifest = "\n".join(entries) + "\n"
    manifest_path = root / "artifact_hashes.sha256"
    manifest_path.write_text(manifest, encoding="utf-8")
    manifest_hash = sha256(manifest_path)
    (root / "SEALED").write_text(manifest_hash + "\n", encoding="utf-8")
    if sha256(manifest_path) != manifest_hash:
        raise AssertionError("artifact manifest verification failed")
    return manifest_hash, len(entries)


def analyze(output_root=None, verify_all_seals=True):
    root = make_output_root(output_root)
    source_roots = {
        "ia_long_branch": IA_ROOT,
        "ia_postrun_audit": IA_AUDIT_ROOT,
        "historical_top_branch": TOP_ROOT,
        "top_ia_qualification": QUALIFICATION_ROOT,
        "jackpot_calibration": JACKPOT_ROOT,
        "forensic_followup": FORENSIC_ROOT,
    }
    seals = {
        name: verify_seal(path) if verify_all_seals else {"verified": None, "skipped": True}
        for name, path in source_roots.items()
    }
    if verify_all_seals and not all(value["verified"] for value in seals.values()):
        raise RuntimeError(f"source seal verification failed: {seals}")

    ia_rows = read_ia_attempts()
    top_rows = read_top_attempts()
    if len(ia_rows) != 63 or not all(row["candidate_validated"] and row["adopted"] for row in ia_rows):
        raise AssertionError("IA authoritative attempt contract mismatch")
    if sum(row["gain"] for row in ia_rows) != 1098:
        raise AssertionError("IA cumulative gain mismatch")

    write_csv(root / "ia_attempt_mechanism_master.csv", mechanism_master_rows(ia_rows))
    breadth_payload = write_breadth_analysis(root, ia_rows)
    write_component_outputs(root, ia_rows, breadth_payload)
    write_scope_outputs(root, ia_rows)
    branch_stats = write_branch_conflicts(root, ia_rows)
    prefix_snapshots = write_prefix_analysis(root, ia_rows, top_rows)
    resources = resource_audit(root, ia_rows)
    design = design_payload(ia_rows, top_rows, branch_stats)
    write_design(root, design)

    code_sources = {
        "objective_semantics": REPOSITORY_ROOT / "scheduling_engine" / "student_assignment" / "objective_semantics.py",
        "model_objective_construction": REPOSITORY_ROOT / "scheduling_engine" / "student_assignment" / "core.py",
        "fixed_scope_probe": REPOSITORY_ROOT / "scheduling_engine" / "student_assignment" / "substantive_probe.py",
    }
    manifest = {
        "schema": "r16_within_scope_semantics_solver_free_audit_manifest_v1",
        "lineage_id": root.name,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "sealed",
        "solver_execution": False,
        "validation_execution": False,
        "schedule_mutation": False,
        "recommendation": design["recommendation"],
        "ia_branch_classification": "E_EXPERIMENT_OPERATIONALLY_INVALID",
        "ia_attempt_level_evidence": {
            "completed_attempts": len(ia_rows),
            "validated_strict_adoptions": sum(row["candidate_validated"] and row["adopted"] for row in ia_rows),
            "partial_gain": sum(row["gain"] for row in ia_rows),
            "partial_value": ia_rows[-1]["value_after"],
        },
        "source_roots": {name: str(path) for name, path in source_roots.items()},
        "source_seal_verification": seals,
        "resource_summary": resources,
        "prefix_snapshots": prefix_snapshots,
        "generated_files": [
            "ia_attempt_mechanism_master.csv",
            "ia_broad_high_vs_broad_low.json",
            "ia_broad_high_vs_broad_low.md",
            "ia_component_by_attempt.csv",
            "ia_component_tradeoff_analysis.md",
            "ia_scope_evolution_analysis.csv",
            "ia_high_gain_scope_sequences.md",
            "ia_branch_conflict_analysis.csv",
            "ia_resource_overhead_audit.md",
            "ia_vs_top_prefix_decomposition.md",
            "fixed_scope_search_semantics_design.json",
            "fixed_scope_search_semantics_design.md",
            "study_manifest.json",
        ],
        "code_semantics_sources": {
            name: {
                "path": str(path.relative_to(REPOSITORY_ROOT)).replace("\\", "/"),
                "sha256": sha256(path),
            }
            for name, path in code_sources.items()
        },
        "no_alternative_schedule_outcome_inferred": True,
    }
    write_json(root / "study_manifest.json", manifest)
    manifest_hash, file_count = seal(root)
    return {
        "root": str(root),
        "artifact_hashes_sha256": manifest_hash,
        "hashed_file_count": file_count,
        "recommendation": design["recommendation"],
        "cells": design["total_cell_count"],
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--skip-full-seal-verification", action="store_true")
    args = parser.parse_args(argv)
    result = analyze(args.output_root, verify_all_seals=not args.skip_full_seal_verification)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
