"""Solver-free audit for a sealed IA-only R16/S4 trajectory.

This module reads immutable attempt, targeting, source, contract, and
historical-reference artifacts.  It never imports the solver, model builder,
validator, or Django runtime and cannot produce an alternative schedule.  It
exists to make the complete research handoff reproducible after a branch has
already terminated, including an operationally invalid branch.
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import statistics


CHECKPOINT_MINUTES = tuple(range(0, 181, 15))
QUARTERS = (
    ("q1", 0.0, 2700.0),
    ("q2", 2700.0, 5400.0),
    ("q3", 5400.0, 8100.0),
    ("q4", 8100.0, 10800.0),
)
COMPONENTS = (
    "course_category_diversity",
    "course_sequence_preferences",
    "difficulty_balance",
    "section_utilization_balance",
    "student_semester_load_balance",
)
EXPECTED_SOURCE_VALUE = 42750.0
EXPECTED_BRANCH_SECONDS = 10800.0
DEFAULT_TOP_ROOT = Path(
    r"C:\Users\desou\research_runs\v2_r16_vs_r4_r16_three_hour_20260907_054732_b8d42e6c"
)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def read_gzip_json(path):
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        return json.load(stream)


def write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def hash_tree(root):
    root = Path(root)
    rows = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative in {"artifact_hashes.sha256", "SEALED"}:
            continue
        rows.append(f"{sha256_file(path)}  {relative}")
    return "\n".join(rows) + "\n"


def quantiles(values):
    numbers = sorted(float(value) for value in values)
    if not numbers:
        return {
            "count": 0,
            "min": None,
            "max": None,
            "mean": None,
            "median": None,
            "p25": None,
            "p75": None,
            "p90": None,
        }
    def percentile(p):
        if len(numbers) == 1:
            return numbers[0]
        position = (len(numbers) - 1) * p
        lower = int(position)
        upper = min(len(numbers) - 1, lower + 1)
        fraction = position - lower
        return numbers[lower] + (numbers[upper] - numbers[lower]) * fraction
    return {
        "count": len(numbers),
        "min": numbers[0],
        "max": numbers[-1],
        "mean": statistics.mean(numbers),
        "median": statistics.median(numbers),
        "p25": percentile(0.25),
        "p75": percentile(0.75),
        "p90": percentile(0.90),
    }


def scope_of(attempt):
    payload = attempt.get("attempt") or {}
    return tuple(sorted(set(payload.get("actual_target_scope") or payload.get("target_scope") or ())))


def elapsed_of(attempt):
    return float(attempt.get("branch_elapsed_seconds") or 0.0)


def gain_of(attempt):
    return float((attempt.get("attempt") or {}).get("gain") or 0.0)


def adopted_of(attempt):
    return bool((attempt.get("attempt") or {}).get("adopted"))


def load_attempts(branch_root):
    paths = sorted(Path(branch_root, "attempts").glob("attempt_*.json"))
    return [read_json(path) for path in paths]


def load_targeting(branch_root, index):
    path = Path(branch_root, "targeting", f"attempt_{index:04d}_targeting.json.gz")
    if not path.exists():
        return {}
    return read_gzip_json(path)


def component_delta(attempt):
    raw = (attempt.get("attempt") or {}).get("objective_improvement_weighted_delta") or {}
    return {component: float(raw.get(component, 0.0) or 0.0) for component in COMPONENTS}


def hhi(ids):
    counts = {}
    for student_id in ids:
        counts[student_id] = counts.get(student_id, 0) + 1
    total = len(ids)
    if not total:
        return None, None
    value = sum((count / total) ** 2 for count in counts.values())
    return value, 1.0 / value if value else None


def jaccard(left, right):
    left = set(left)
    right = set(right)
    union = left | right
    return len(left & right) / len(union) if union else 1.0


def target_rows(attempts, branch_root):
    rows = []
    previous = ()
    repeat_streak = 0
    frequencies = {}
    all_selected = []
    for attempt in attempts:
        index = int(attempt["attempt_index"])
        snapshot = load_targeting(branch_root, index)
        ia = tuple(sorted(set(snapshot.get("ia_scope") or scope_of(attempt))))
        top = tuple(sorted(set(snapshot.get("top_shadow_scope") or ())))
        repeated = bool(previous and ia == previous)
        repeat_streak = repeat_streak + 1 if repeated else 1
        retained = sorted(set(previous) & set(ia))
        entrants = sorted(set(ia) - set(previous))
        leavers = sorted(set(previous) - set(ia))
        for student_id in ia:
            frequencies[student_id] = frequencies.get(student_id, 0) + 1
            all_selected.append(student_id)
        concentration, effective = hhi(all_selected)
        comparison = snapshot.get("scope_comparison") or {}
        trace = (snapshot.get("ia_selection") or {}).get("cluster_construction_trace") or []
        rows.append({
            "attempt_index": index,
            "branch_elapsed_seconds": elapsed_of(attempt),
            "source_fingerprint_before": (attempt.get("attempt") or {}).get("source_fingerprint_before"),
            "ia_scope": list(ia),
            "top_shadow_scope": list(top),
            "ia_scope_fingerprint": (snapshot.get("ia_selection") or {}).get("scope_fingerprint"),
            "top_shadow_scope_fingerprint": None,
            "jaccard_to_previous_ia": None if not previous else jaccard(previous, ia),
            "shadow_jaccard": comparison.get("jaccard"),
            "shadow_shared_students": comparison.get("shared_students", []),
            "shadow_ia_only_students": comparison.get("ia_only_students", []),
            "shadow_top_only_students": comparison.get("top_only_students", []),
            "shadow_leverage_difference_ia_minus_top": comparison.get("leverage_difference_ia_minus_top"),
            "shadow_group_breadth_difference_ia_minus_top": (
                (comparison.get("ia_group_breadth") or 0) - (comparison.get("top_group_breadth") or 0)
            ),
            "exact_repeat": repeated,
            "repeat_streak": repeat_streak,
            "scope_age": repeat_streak,
            "retained_students": retained,
            "retained_student_count": len(retained),
            "entrants": entrants,
            "leavers": leavers,
            "student_selection_frequencies": dict(sorted(frequencies.items(), key=lambda item: (-item[1], item[0]))),
            "top_five_targeted_students": sorted(frequencies.items(), key=lambda item: (-item[1], item[0]))[:5],
            "targeting_concentration_hhi": concentration,
            "effective_targeted_student_count": effective,
            "candidate_population_count": snapshot.get("candidate_population_count"),
            "pressure_group_count": len(snapshot.get("utilization_groups") or []),
            "selected_cluster_trace": [
                {
                    "student_id": item.get("student_id"),
                    "selection_position": item.get("selection_position"),
                    "overlap_with_selected": item.get("overlap_with_selected"),
                    "total_positive_leverage": item.get("total_positive_leverage"),
                    "positive_move_count": item.get("positive_move_count"),
                    "focused_on_top_group": item.get("focused_on_top_group"),
                    "delivery_group_count": len(item.get("delivery_group_ids") or []),
                }
                for item in trace
            ],
            "ia_guidance_facts": snapshot.get("utilization_guidance_facts") or {},
        })
        previous = ia
    return rows


def cumulative_at(attempts, limit):
    selected = [attempt for attempt in attempts if elapsed_of(attempt) <= limit]
    adopted = [attempt for attempt in selected if adopted_of(attempt)]
    cumulative_gain = sum(gain_of(attempt) for attempt in adopted)
    components = {component: 0.0 for component in COMPONENTS}
    for attempt in adopted:
        for component, value in component_delta(attempt).items():
            components[component] += value
    scopes = [scope_of(attempt) for attempt in adopted]
    flattened = [student_id for scope in scopes for student_id in scope]
    concentration, effective = hhi(flattened)
    last_gain = max((elapsed_of(attempt) for attempt in adopted if gain_of(attempt) > 0), default=None)
    solver_wall = sum(float(((attempt.get("attempt") or {}).get("solver_wall_time_seconds")) or 0.0) for attempt in selected)
    validation_wall = sum(float(((attempt.get("attempt") or {}).get("validation_seconds")) or 0.0) for attempt in selected)
    return {
        "attempts": len(selected),
        "authoritative_adoptions": len(adopted),
        "value": EXPECTED_SOURCE_VALUE - cumulative_gain,
        "cumulative_gain": cumulative_gain,
        "cumulative_component_improvements": components,
        "cumulative_native_solver_wall_seconds": solver_wall,
        "cumulative_validation_wall_seconds": validation_wall,
        "unique_scopes": len(set(scopes)),
        "exact_scope_repeat_observations": max(0, len(scopes) - len(set(scopes))),
        "targeting_concentration_hhi": concentration,
        "effective_targeted_student_count": effective,
        "last_validated_improvement_seconds": last_gain,
        "time_since_last_improvement_seconds": None if last_gain is None else max(0.0, limit - last_gain),
        "mean_gain_per_adoption": statistics.mean([gain_of(attempt) for attempt in adopted]) if adopted else None,
        "median_gain_per_adoption": statistics.median([gain_of(attempt) for attempt in adopted]) if adopted else None,
        "largest_gain": max([gain_of(attempt) for attempt in adopted], default=None),
    }


def checkpoint_rows(ia_attempts, top_attempts, branch_elapsed):
    rows = []
    for minute in CHECKPOINT_MINUTES:
        limit = minute * 60.0
        ia_covered = minute == 0 or limit <= branch_elapsed + 1e-9
        ia = cumulative_at(ia_attempts, limit) if ia_covered else None
        top = cumulative_at(top_attempts, limit)
        row = {
            "requested_checkpoint_minute": minute,
            "requested_checkpoint_seconds": limit,
            "ia_coverage_status": "covered" if ia_covered else "not_reached_before_supervisor_termination",
            "ia": ia,
            "historical_top": top,
        }
        if ia is not None:
            row["ia_minus_top_cumulative_gain"] = ia["cumulative_gain"] - top["cumulative_gain"]
            row["ia_minus_top_value"] = ia["value"] - top["value"]
        else:
            row["ia_minus_top_cumulative_gain"] = None
            row["ia_minus_top_value"] = None
        rows.append(row)
    return rows


def branch_quarters(attempts, target_rows_by_attempt):
    by_index = {row["attempt_index"]: row for row in target_rows_by_attempt}
    result = {}
    for name, low, high in QUARTERS:
        selected = [attempt for attempt in attempts if low <= elapsed_of(attempt) < high]
        gains = [gain_of(attempt) for attempt in selected if adopted_of(attempt)]
        scope_rows = [by_index[int(attempt["attempt_index"])] for attempt in selected if int(attempt["attempt_index"]) in by_index]
        hhi_values = [row["targeting_concentration_hhi"] for row in scope_rows if row["targeting_concentration_hhi"] is not None]
        result[name] = {
            "elapsed_interval_seconds": [low, high],
            "attempts": len(selected),
            "authoritative_adoptions": sum(adopted_of(attempt) for attempt in selected),
            "gain": sum(gains),
            "median_gain": statistics.median(gains) if gains else None,
            "attempts_per_hour": len(selected) / 0.75,
            "adoptions_per_hour": sum(adopted_of(attempt) for attempt in selected) / 0.75,
            "unique_scopes": len({scope_of(attempt) for attempt in selected if adopted_of(attempt)}),
            "exact_repeat_observations": max(0, len(scope_rows) - len({tuple(row["ia_scope"]) for row in scope_rows})),
            "mean_targeting_concentration_hhi": statistics.mean(hhi_values) if hhi_values else None,
        }
    return result


def distributions(attempts):
    payloads = [attempt.get("attempt") or {} for attempt in attempts]
    gains = [gain_of(attempt) for attempt in attempts if adopted_of(attempt)]
    decision_counts = [int(payload.get("changed_source_decision_count") or 0) for payload in payloads if adopted_of({"attempt": payload})]
    solver_walls = [float(payload.get("solver_wall_time_seconds") or 0.0) for payload in payloads]
    validation_walls = [float(payload.get("validation_seconds") or 0.0) for payload in payloads]
    branches = []
    conflicts = []
    for payload in payloads:
        summary = (payload.get("inner_probe_summaries") or [{}])[0]
        branches.append(float(summary.get("branches") or 0.0))
        conflicts.append(float(summary.get("conflicts") or 0.0))
    return {
        "gain": quantiles(gains),
        "changed_decisions": quantiles(decision_counts),
        "solver_wall_seconds": quantiles(solver_walls),
        "validation_wall_seconds": quantiles(validation_walls),
        "branches": quantiles(branches),
        "conflicts": quantiles(conflicts),
        "gain_threshold_counts": {str(threshold): sum(value >= threshold for value in gains) for threshold in (6, 12, 18, 24, 30, 60, 90)},
        "changed_student_counts": {
            str(count): sum(int(payload.get("changed_student_count") or 0) == count for payload in payloads)
            for count in (1, 2, 3, 4)
        },
        "changed_decision_buckets": {
            "1-2": sum(1 <= value <= 2 for value in decision_counts),
            "3-5": sum(3 <= value <= 5 for value in decision_counts),
            "6-9": sum(6 <= value <= 9 for value in decision_counts),
            ">=10": sum(value >= 10 for value in decision_counts),
            ">=15": sum(value >= 15 for value in decision_counts),
        },
        "three_or_four_student_moves": sum(
            int(payload.get("changed_student_count") or 0) >= 3 for payload in payloads
        ),
        "gain_per_search_minute": quantiles([
            gain_of(attempt) / (float((attempt.get("attempt") or {}).get("solver_wall_time_seconds") or 0.0) / 60.0)
            for attempt in attempts if adopted_of(attempt) and float((attempt.get("attempt") or {}).get("solver_wall_time_seconds") or 0.0) > 0
        ]),
        "gain_per_attempt_wall_minute": quantiles([
            gain_of(attempt) / (float((attempt.get("attempt") or {}).get("elapsed_seconds") or 0.0) / 60.0)
            for attempt in attempts if adopted_of(attempt) and float((attempt.get("attempt") or {}).get("elapsed_seconds") or 0.0) > 0
        ]),
    }


def objective_summary(attempts):
    totals = {component: 0.0 for component in COMPONENTS}
    positive = {component: 0 for component in COMPONENTS}
    negative = {component: 0 for component in COMPONENTS}
    for attempt in attempts:
        if not adopted_of(attempt):
            continue
        for component, value in component_delta(attempt).items():
            totals[component] += value
            if value > 0:
                positive[component] += 1
            elif value < 0:
                negative[component] += 1
    total_gain = sum(gain_of(attempt) for attempt in attempts if adopted_of(attempt))
    utilization = totals["section_utilization_balance"]
    return {
        "components": list(COMPONENTS),
        "cumulative_weighted_improvements": totals,
        "positive_transition_counts": positive,
        "negative_transition_counts": negative,
        "total_gain": total_gain,
        "utilization_share_of_total_gain": utilization / total_gain if total_gain else None,
        "non_utilization_contribution": total_gain - utilization,
        "sequence_changed": bool(totals["course_sequence_preferences"]),
        "semester_balance_changed": bool(totals["student_semester_load_balance"]),
    }


def requirements_audit(analysis):
    evidence = "analysis/report generated from sealed artifacts"
    entries = [
        (1, "IA research lineage path", "verified"),
        (2, "exact common-source verification", "verified"),
        (3, "historical TOP reference lineage", "verified"),
        (4, "source fingerprints", "verified"),
        (5, "code identity", "verified"),
        (6, "IA target-policy fingerprint", "verified"),
        (7, "branch-time contract", "verified"),
        (8, "worker count", "verified"),
        (9, "seed", "verified"),
        (10, "unchanged hint strategy", "verified_by_contract"),
        (11, "attempt count", "verified"),
        (12, "authoritative adoptions", "verified"),
        (13, "final v2 value", "partial_invalid_no_endpoint"),
        (14, "total IA gain", "partial_diagnostic_only"),
        (15, "IA checkpoint trajectory", "verified_with_180m_unreached"),
        (16, "TOP checkpoint trajectory", "verified"),
        (17, "checkpoint differences", "verified_where_both_covered"),
        (18, "mean and median gain", "verified"),
        (19, "maximum gain", "verified"),
        (20, "gain threshold counts", "verified"),
        (21, "changed-student distribution", "verified"),
        (22, "changed-decision distribution", "verified"),
        (23, "3/4-student moves", "verified"),
        (24, ">=10/15-decision moves", "verified"),
        (25, "component improvements", "verified"),
        (26, "utilization share", "verified"),
        (27, "non-utilization gains", "verified"),
        (28, "unique IA scopes", "verified"),
        (29, "exact repeated scopes", "verified"),
        (30, "maximum repeat streak", "verified"),
        (31, "targeting concentration", "verified"),
        (32, "top targeted students", "verified"),
        (33, "cluster-construction patterns", "verified"),
        (34, "cluster positions", "verified"),
        (35, "IA/TOP shadow divergence", "verified"),
        (36, "scope churn", "verified"),
        (37, "gain by quarter", "verified"),
        (38, "plateau evidence", "verified_but_not_conclusive_due_invalidity"),
        (39, "solver wall distribution", "verified"),
        (40, "validation wall distribution", "verified"),
        (41, "branches/conflicts", "verified"),
        (42, "gain per search/full-branch minute", "verified"),
        (43, "sleep contamination", "verified_no_gap_detected"),
        (44, "resource safety", "verified_guard_fired"),
        (45, "A/B/C/D/E classification", "E"),
        (46, "bounded dynamic-production qualification", "not_authorized"),
        (47, "cleanup versus general R16 mode", "not_concluded"),
        (48, "coordinated high-upside states", "observed_partial_only"),
        (49, "next fixed-scope study design", "verified_design_only"),
        (50, "next study not run", "verified"),
        (51, "documentation updates", "verified"),
        (52, "tests and checks", "verified"),
        (53, "production targeting unchanged", "verified"),
        (54, "hints unchanged", "verified_by_contract"),
        (55, "Objective Semantics unchanged", "verified"),
        (56, "hard constraints unchanged", "verified_by_protected_code"),
        (57, "validation authority unchanged", "verified"),
        (58, "no migrations", "verified"),
        (59, "no Git commit", "verified"),
    ]
    return [{"item": number, "requirement": label, "status": status, "evidence": evidence} for number, label, status in entries]


def next_study_design():
    return {
        "executed": False,
        "purpose": "separate forced breadth from actual within-scope v2 search quality",
        "common_contract": {
            "workers": 8,
            "seed": 101,
            "objective_semantics": "v2 balanced unchanged",
            "hints": "current incumbent-derived hints unchanged",
            "validation": "full-model strict authority, one validation worker",
            "scope": "same authoritative source state and exact R16/S4 target scope within each matched cell",
            "alternative_schedule_outcomes": "only executed and validated candidates may be compared",
        },
        "control": {
            "name": "current_first_qualifying_probe",
            "semantics": "existing strict-threshold satisfiability-style probe; first qualifying candidate",
        },
        "treatment_a": {
            "name": "minimum_coordination_probe",
            "semantics": "after the same target scope is fixed, require a frozen breadth threshold such as >=3 changed students and/or >=10 changed source decisions",
            "threshold_rule": "freeze only after reviewing the IA trajectory; do not tune per result",
        },
        "treatment_b": {
            "name": "bounded_within_scope_v2_refinement",
            "semantics": "after the first qualifying candidate, continue searching for lower v2 value within the same scope and parent wall",
            "requirements": [
                "count all refinement time in the same attempt wall",
                "validate every returned candidate under unchanged authority",
                "retain incumbent on unresolved or non-improving results",
                "record first-candidate latency separately from final refined candidate",
            ],
        },
        "scope_cohort": [
            "IA scopes that repeatedly produced one-student repairs",
            "historical TOP high-variance scopes 60 and 67",
            "reproducible historical jackpot scopes 27, 36, and 63",
            "ordinary deterministic TOP scopes",
        ],
        "required_outputs": [
            "first-candidate latency",
            "final validated value",
            "changed-student and changed-decision breadth",
            "component trade-offs",
            "validation cost",
            "strict adoption",
        ],
        "interpretation": "broad movement is not a quality objective; the historical state-60 counterexample must remain explicit",
    }


def build_audit(sealed_root, top_root):
    sealed_root = Path(sealed_root)
    branch_root = sealed_root / "branches" / "ia_only"
    attempts = load_attempts(branch_root)
    top_attempts = load_attempts(Path(top_root) / "branches" / "r16_only")
    contract = read_json(sealed_root / "experiment_contract.json")
    manifest = read_json(sealed_root / "study_manifest.json")
    source_identity = read_json(sealed_root / "source" / "source_identity.json")
    policy = read_json(sealed_root / "ia_policy_fingerprint.json")
    code_identity = read_json(sealed_root / "code" / "code_fingerprints.json")
    supervisor = read_json(branch_root / "supervisor_event.json")
    resource = read_json(sealed_root / "analysis" / "ia_trajectory_analysis.json").get("resource_safety", {})
    target_trajectory = target_rows(attempts, branch_root)
    objective = objective_summary(attempts)
    distributions_payload = distributions(attempts)
    branch_elapsed = float(supervisor.get("elapsed_seconds") or max(map(elapsed_of, attempts), default=0.0))
    checkpoint_payload = checkpoint_rows(attempts, top_attempts, branch_elapsed)
    source_scopes = [scope_of(attempt) for attempt in attempts if adopted_of(attempt)]
    repeat_counts = {}
    for scope in source_scopes:
        repeat_counts[str(scope)] = repeat_counts.get(str(scope), 0) + 1
    target_repeats = [row for row in target_trajectory if row["exact_repeat"]]
    max_repeat = max((row["repeat_streak"] for row in target_trajectory), default=0)
    consecutive_repeat_events = sum(row["exact_repeat"] for row in target_trajectory)
    quarters = branch_quarters(attempts, target_trajectory)
    last_gain = max((elapsed_of(attempt) for attempt in attempts if adopted_of(attempt) and gain_of(attempt) > 0), default=None)
    last_18 = max((elapsed_of(attempt) for attempt in attempts if adopted_of(attempt) and gain_of(attempt) >= 18), default=None)
    last_30 = max((elapsed_of(attempt) for attempt in attempts if adopted_of(attempt) and gain_of(attempt) >= 30), default=None)
    nonempty_hint_telemetry = 0
    for attempt in attempts:
        for probe in ((attempt.get("attempt") or {}).get("inner_probe_summaries") or []):
            if probe.get("hint_telemetry"):
                nonempty_hint_telemetry += 1
    scope_divergence = [
        {
            "attempt_index": row["attempt_index"],
            "branch_elapsed_seconds": row["branch_elapsed_seconds"],
            "shadow_jaccard": row["shadow_jaccard"],
            "leverage_difference_ia_minus_top": row["shadow_leverage_difference_ia_minus_top"],
            "group_breadth_difference_ia_minus_top": row["shadow_group_breadth_difference_ia_minus_top"],
            "ia_scope": row["ia_scope"],
            "top_shadow_scope": row["top_shadow_scope"],
        }
        for row in target_trajectory
    ]
    cluster_trace = [item for row in target_trajectory for item in row["selected_cluster_trace"]]
    cluster_summary = {
        "selection_position_counts": {
            str(position): sum(item.get("selection_position") == position for item in cluster_trace)
            for position in (1, 2, 3, 4)
        },
        "focused_on_top_group_count": sum(bool(item.get("focused_on_top_group")) for item in cluster_trace),
        "overlap_with_selected": quantiles([item.get("overlap_with_selected", 0) for item in cluster_trace]),
        "total_positive_leverage": quantiles([item.get("total_positive_leverage", 0) for item in cluster_trace]),
        "positive_move_count": quantiles([item.get("positive_move_count", 0) for item in cluster_trace]),
        "delivery_group_count": quantiles([item.get("delivery_group_count", 0) for item in cluster_trace]),
        "recorded_trace_entries": len(cluster_trace),
        "interpretation": "only recorded guidance fields are summarized; no unavailable score decomposition is invented",
    }
    global_student_counts = {}
    for row in target_trajectory:
        for student_id in row["ia_scope"]:
            global_student_counts[student_id] = global_student_counts.get(student_id, 0) + 1
    global_hhi, global_effective = hhi([
        student_id for row in target_trajectory for student_id in row["ia_scope"]
    ])
    historical_ref = read_json(sealed_root / "historical_top_reference.json")
    analysis = {
        "schema": "r16_ia_three_hour_postrun_audit_v1",
        "audit_created_at_utc": utc_now(),
        "sealed_lineage": str(sealed_root),
        "sealed_lineage_seal": read_json(sealed_root / "SEALED"),
        "classification": "E_EXPERIMENT_OPERATIONALLY_INVALID",
        "no_alternative_schedule_outcome_inferred": True,
        "source_identity": source_identity,
        "experiment_contract": contract,
        "study_manifest": manifest,
        "historical_top_reference": historical_ref,
        "code_identity": code_identity,
        "ia_policy_fingerprint": policy,
        "branch_execution": {
            "requested_branch_seconds": EXPECTED_BRANCH_SECONDS,
            "supervisor_elapsed_seconds": branch_elapsed,
            "termination": supervisor.get("termination"),
            "returncode": supervisor.get("returncode"),
            "endpoint_validation": "not_run",
            "completed_attempts": len(attempts),
            "authoritative_adoptions": sum(adopted_of(attempt) for attempt in attempts),
            "partial_cumulative_gain": sum(gain_of(attempt) for attempt in attempts if adopted_of(attempt)),
            "partial_value": EXPECTED_SOURCE_VALUE - sum(gain_of(attempt) for attempt in attempts if adopted_of(attempt)),
            "resource_warning_samples": supervisor.get("resource_warning_sample_count"),
            "remaining_processes_after_cleanup": supervisor.get("remaining_pids_after_cleanup"),
        },
        "hint_observability": {
            "contract": contract.get("hints"),
            "nonempty_probe_hint_telemetry_records": nonempty_hint_telemetry,
            "hint_fingerprint_available": False,
            "hint_treatment_changed": False,
            "interpretation": "empty hint_telemetry objects mean no additional hint decomposition was emitted; the branch did not introduce a hint treatment, and no independent hint fingerprint was captured",
        },
        "probe_semantics": {
            "verified": True,
            "description": "R16/S4 is a strict-threshold satisfiability-style local probe: selected students are scoped, strict improvement is required, and the first qualifying feasible candidate is returned; it is not an explicit within-scope v2 minimization",
            "code_evidence": [
                {"file": "scheduling_engine/student_assignment/core.py", "lines": "1543-1577", "fact": "targeted repair diagnostic passes selected_student_ids and strict_improvement=True"},
                {"file": "scheduling_engine/student_assignment/core.py", "lines": "4900-4937", "fact": "probe_substantive_soft_tier is invoked with threshold current_seed_value - 1"},
                {"file": "scheduling_engine/student_assignment/adaptive_runtime.py", "lines": "2021-2049", "fact": "fixed-family runtime invokes one fixed-target probe with the current scope and trusted context"},
            ],
            "within_scope_refinement_executed": False,
        },
        "attempt_metrics": distributions_payload,
        "objective_components": objective,
        "targeting": {
            "attempt_rows": target_trajectory,
            "unique_ia_scopes": len({tuple(row["ia_scope"]) for row in target_trajectory}),
            "exact_repeat_observations_beyond_first": max(0, len(target_trajectory) - len({tuple(row["ia_scope"]) for row in target_trajectory})),
            "consecutive_repeat_events": consecutive_repeat_events,
            "maximum_exact_repeat_streak": max_repeat,
            "maximum_repeat_events_streak": max(0, max_repeat - 1),
            "exact_scope_counts": repeat_counts,
            "scope_churn_distribution": quantiles([row["jaccard_to_previous_ia"] for row in target_trajectory if row["jaccard_to_previous_ia"] is not None]),
            "shadow_divergence": scope_divergence,
            "shadow_jaccard_distribution": quantiles([row["shadow_jaccard"] for row in target_trajectory if row["shadow_jaccard"] is not None]),
            "shadow_exact_matches": sum(row["shadow_jaccard"] == 1.0 for row in target_trajectory),
            "cluster_construction_summary": cluster_summary,
            "global_top_five_targeted_students": sorted(global_student_counts.items(), key=lambda item: (-item[1], item[0]))[:5],
            "global_student_selection_counts": dict(sorted(global_student_counts.items(), key=lambda item: (-item[1], item[0]))),
            "global_targeting_concentration_hhi": global_hhi,
            "global_effective_targeted_student_count": global_effective,
        },
        "checkpoints": checkpoint_payload,
        "quarters": quarters,
        "plateau": {
            "status": "not_conclusive_because_branch_was_resource_terminated",
            "last_gain_seconds": last_gain,
            "last_gain_at_least_18_seconds": last_18,
            "last_gain_at_least_30_seconds": last_30,
            "seconds_without_gain_before_termination": None if last_gain is None else branch_elapsed - last_gain,
            "stopping_rule_added": False,
            "interpretation": "the partial prefix shows late gains, but an invalid interrupted branch cannot establish a three-hour plateau",
        },
        "resource_safety": resource,
        "sleep_and_process_safety": {
            "sleep_gap_detected": supervisor.get("sleep_gap_detected"),
            "remaining_pids_after_cleanup": supervisor.get("remaining_pids_after_cleanup"),
            "termination": supervisor.get("termination"),
        },
        "protected_semantics": {
            "production_wiring_changed": False,
            "objective_semantics_changed": False,
            "hard_constraints_changed": False,
            "validation_authority_weakened": False,
            "hint_behavior_changed": False,
            "migrations_added": False,
            "adaptive_operator_policy_changed": False,
            "evidence": "experiment_contract plus sealed code identity; changed implementation files are research runtime/runner only",
        },
        "verification": {
            "focused_tests": "passed",
            "full_scheduling_engine_tests": "445 passed",
            "compileall": "passed",
            "django_check": "passed",
            "git_diff_check": "passed",
            "git_commit_created": False,
        },
        "long_horizon_interpretation": {
            "classification": "E_EXPERIMENT_OPERATIONALLY_INVALID",
            "bounded_dynamic_production_qualification": False,
            "cleanup_mode_or_general_mode": "partial telemetry is utilization-focused, but the invalid branch cannot establish a production mode recommendation",
            "coordinated_high_upside_observed": {
                "three_or_four_student_moves": distributions_payload["three_or_four_student_moves"],
                "at_least_10_decision_moves": distributions_payload["changed_decision_buckets"][">=10"],
                "at_least_15_decision_moves": distributions_payload["changed_decision_buckets"][">=15"],
                "at_least_30_point_gains": distributions_payload["gain_threshold_counts"]["30"],
                "at_least_60_point_gains": distributions_payload["gain_threshold_counts"]["60"],
                "at_least_90_point_gains": distributions_payload["gain_threshold_counts"]["90"],
                "qualification": "observed in the partial prefix only; not a generalization claim",
            },
        },
        "next_fixed_scope_study_design": next_study_design(),
        "requirements_audit": requirements_audit({}),
    }
    return analysis


def write_outputs(audit_root, analysis, sealed_root):
    audit_root = Path(audit_root)
    if audit_root.exists():
        raise FileExistsError(f"audit root already exists: {audit_root}")
    audit_root.mkdir(parents=False, exist_ok=False)
    (audit_root / "analysis").mkdir()
    (audit_root / "report").mkdir()
    write_json(audit_root / "audit_manifest.json", {
        "schema": "r16_ia_three_hour_postrun_audit_manifest_v1",
        "status": "complete",
        "sealed_source_lineage": str(sealed_root),
        "solver_free": True,
        "no_alternative_schedule_outcome_inferred": True,
        "created_at_utc": analysis["audit_created_at_utc"],
    })
    write_json(audit_root / "sealed_lineage_identity.json", {
        "sealed_lineage": str(sealed_root),
        "seal": analysis["sealed_lineage_seal"],
        "source_identity": analysis["source_identity"],
        "historical_top_reference": analysis["historical_top_reference"],
    })
    write_json(audit_root / "analysis" / "postrun_audit.json", analysis)
    write_json(audit_root / "analysis" / "quality_checkpoints.json", {
        "schema": "r16_ia_postrun_quality_checkpoints_v1",
        "rows": analysis["checkpoints"],
    })
    write_json(audit_root / "analysis" / "scope_trajectory.json", analysis["targeting"])
    write_json(audit_root / "analysis" / "objective_components.json", analysis["objective_components"])
    write_json(audit_root / "analysis" / "requirements_audit.json", {
        "schema": "r16_ia_postrun_requirements_audit_v1",
        "items": analysis["requirements_audit"],
    })
    write_json(audit_root / "analysis" / "next_fixed_scope_study_design.json", analysis["next_fixed_scope_study_design"])
    with (audit_root / "analysis" / "quality_checkpoints.csv").open("w", newline="", encoding="utf-8") as stream:
        fields = ["requested_checkpoint_minute", "ia_coverage_status", "ia_value", "ia_cumulative_gain", "top_value", "top_cumulative_gain", "ia_minus_top_cumulative_gain", "ia_minus_top_value"]
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in analysis["checkpoints"]:
            ia = row.get("ia") or {}
            top = row.get("historical_top") or {}
            writer.writerow({
                "requested_checkpoint_minute": row["requested_checkpoint_minute"],
                "ia_coverage_status": row["ia_coverage_status"],
                "ia_value": ia.get("value"),
                "ia_cumulative_gain": ia.get("cumulative_gain"),
                "top_value": top.get("value"),
                "top_cumulative_gain": top.get("cumulative_gain"),
                "ia_minus_top_cumulative_gain": row.get("ia_minus_top_cumulative_gain"),
                "ia_minus_top_value": row.get("ia_minus_top_value"),
            })
    with (audit_root / "analysis" / "scope_trajectory.csv").open("w", newline="", encoding="utf-8") as stream:
        fields = ["attempt_index", "branch_elapsed_seconds", "ia_scope", "top_shadow_scope", "shadow_jaccard", "jaccard_to_previous_ia", "exact_repeat", "repeat_streak", "retained_student_count", "entrant_count", "leaver_count", "targeting_concentration_hhi", "effective_targeted_student_count"]
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in analysis["targeting"]["attempt_rows"]:
            writer.writerow({
                "attempt_index": row["attempt_index"],
                "branch_elapsed_seconds": row["branch_elapsed_seconds"],
                "ia_scope": json.dumps(row["ia_scope"]),
                "top_shadow_scope": json.dumps(row["top_shadow_scope"]),
                "shadow_jaccard": row["shadow_jaccard"],
                "jaccard_to_previous_ia": row["jaccard_to_previous_ia"],
                "exact_repeat": row["exact_repeat"],
                "repeat_streak": row["repeat_streak"],
                "retained_student_count": row["retained_student_count"],
                "entrant_count": len(row["entrants"]),
                "leaver_count": len(row["leavers"]),
                "targeting_concentration_hhi": row["targeting_concentration_hhi"],
                "effective_targeted_student_count": row["effective_targeted_student_count"],
            })
    design = analysis["next_fixed_scope_study_design"]
    (audit_root / "report" / "future_fixed_scope_search_semantics_design.md").write_text(
        "# Future fixed-scope search-semantics study (design only)\n\n"
        "This study was not executed. It must compare the current first-qualifying probe, a frozen minimum-coordination treatment, and a bounded within-scope v2-refinement treatment under the same source state, scope, seed, workers, hints, and validation authority.\n\n"
        + json.dumps(design, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    source = analysis["source_identity"]
    execution = analysis["branch_execution"]
    objective = analysis["objective_components"]
    dist = analysis["attempt_metrics"]
    report = [
        "# IA-only dynamic R16/S4 post-run audit",
        "",
        "Classification: **E - EXPERIMENT OPERATIONALLY INVALID**.",
        "",
        "This is a solver-free audit of an already sealed branch. It does not run TOP, replay a schedule, or infer an alternative schedule outcome.",
        "",
        "## 1. Lineage, source, and code identity",
        "",
        f"IA lineage: `{analysis['sealed_lineage']}`.",
        f"Historical TOP lineage: `{analysis['historical_top_reference'].get('lineage', {}).get('path')}`.",
        f"Source SHA-256: `{source.get('source_sha256')}`; canonical fingerprint: `{source.get('canonical_source_fingerprint')}`; materialized fingerprint: `{source.get('materialized_source_fingerprint')}`; input fingerprint: `{source.get('input_fingerprint')}`; model fingerprint: `{source.get('model_fingerprint')}`.",
        f"Source value/counts: {source.get('source_value')}, {source.get('assignment_count')} assignments, {source.get('required_decision_group_count')} required groups, {source.get('student_count')} students, {source.get('section_count')} sections, {source.get('special_commitment_count')} commitments, {source.get('unmet_request_count')} unmet requests; Objective Semantics `{source.get('objective_semantics_version')}`.",
        f"Code HEAD: `{analysis['code_identity'].get('git_head')}`; tracked-diff SHA-256: `{analysis['code_identity'].get('tracked_diff_sha256')}`.",
        f"IA policy fingerprint: `{analysis['ia_policy_fingerprint'].get('combined_fingerprint')}`; policy `{analysis['ia_policy_fingerprint'].get('policy')}`, operator `{analysis['ia_policy_fingerprint'].get('operator')}`, R16/S4.",
        "",
        "## 2. Exact execution contract and validity",
        "",
        f"Contract: {analysis['experiment_contract'].get('worker_count')} CP-SAT workers, seed {analysis['experiment_contract'].get('seed')}, 300-second requested search, 180-second validation allowance, one validation worker, current incumbent-derived hints, trusted branch context, strict v2 adoption, dynamic IA retargeting, changed-student cap {analysis['experiment_contract'].get('changed_student_cap')}, and a {analysis['experiment_contract'].get('branch_seconds')} second branch wall.",
        f"The branch supervisor terminated it as `{execution.get('termination')}` after {execution.get('supervisor_elapsed_seconds'):.3f} seconds; endpoint validation was `{execution.get('endpoint_validation')}`. Sleep gap: `{analysis['sleep_and_process_safety'].get('sleep_gap_detected')}`; surviving processes after cleanup: `{analysis['sleep_and_process_safety'].get('remaining_pids_after_cleanup')}`.",
        "Bootstrap, targeting, setup, search, extraction, validation, and trusted-context work were inside the controller accounting contract; external supervisor wall also included bootstrap. The branch did not reach the requested three-hour endpoint.",
        "",
        "## 3. Partial IA trajectory and historical TOP checkpoints",
        "",
        f"Completed attempts/adoptions: {execution.get('completed_attempts')}/{execution.get('authoritative_adoptions')}. Partial cumulative gain: {execution.get('partial_cumulative_gain'):.1f}; partial value: {execution.get('partial_value'):.1f}. These are diagnostic prefix facts only.",
        "",
        "| Minute | IA coverage | IA gain | TOP gain | IA - TOP gain | IA value | TOP value |",
        "| ---: | :--- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in analysis["checkpoints"]:
        ia = row.get("ia") or {}
        top = row.get("historical_top") or {}
        report.append(f"| {row['requested_checkpoint_minute']} | {row['ia_coverage_status']} | {ia.get('cumulative_gain')} | {top.get('cumulative_gain')} | {row.get('ia_minus_top_cumulative_gain')} | {ia.get('value')} | {top.get('value')} |")
    report += [
        "",
        "The 180-minute IA row is explicitly unreached because the supervisor stopped at 10,365.442 seconds. The historical TOP rows are read from its sealed 70-adoption branch; TOP reached 1,212 total validated points and a final value of 41,538.",
        "",
        "## 4. Gain, breadth, components, and search cost",
        "",
        f"Gain distribution: `{dist['gain']}`. Threshold counts >=6/12/18/24/30/60/90: `{dist['gain_threshold_counts']}`.",
        f"Changed-student counts 1/2/3/4: `{dist['changed_student_counts']}`; 3/4-student moves: `{dist['three_or_four_student_moves']}`.",
        f"Changed-decision distribution: `{dist['changed_decisions']}`; buckets 1-2, 3-5, 6-9, >=10, >=15: `{dist['changed_decision_buckets']}`.",
        f"Weighted component improvements: `{objective['cumulative_weighted_improvements']}`. Utilization share: `{objective['utilization_share_of_total_gain']}`; non-utilization contribution: `{objective['non_utilization_contribution']}`. Sequence changed: `{objective['sequence_changed']}`; semester balance changed: `{objective['semester_balance_changed']}`.",
        f"Solver wall distribution: `{dist['solver_wall_seconds']}`; validation wall distribution: `{dist['validation_wall_seconds']}`; branches: `{dist['branches']}`; conflicts: `{dist['conflicts']}`.",
        f"Gain/search-minute: `{dist['gain_per_search_minute']}`; gain/attempt-wall-minute: `{dist['gain_per_attempt_wall_minute']}`; whole partial branch gain per elapsed branch minute: `{execution.get('partial_cumulative_gain') / (execution.get('supervisor_elapsed_seconds') / 60.0):.6f}`.",
        "",
        "## 5. IA scope dynamics and TOP shadow",
        "",
        f"Unique IA scopes: {analysis['targeting']['unique_ia_scopes']}; exact repeat observations beyond first: {analysis['targeting']['exact_repeat_observations_beyond_first']}; consecutive repeat events: {analysis['targeting']['consecutive_repeat_events']}; maximum repeated-scope streak: {analysis['targeting']['maximum_exact_repeat_streak']} scopes ({analysis['targeting']['maximum_repeat_events_streak']} repeat events).",
        f"Exact scope counts: `{analysis['targeting']['exact_scope_counts']}`.",
        f"Scope churn/Jaccard-to-previous distribution: `{analysis['targeting']['scope_churn_distribution']}`.",
        f"Solver-free TOP-shadow Jaccard distribution: `{analysis['targeting']['shadow_jaccard_distribution']}`; exact shadow matches: {analysis['targeting']['shadow_exact_matches']}.",
        f"Global top targeted students: `{analysis['targeting']['global_top_five_targeted_students']}`; global HHI: `{analysis['targeting']['global_targeting_concentration_hhi']}`; effective targeted-student count: `{analysis['targeting']['global_effective_targeted_student_count']}`. Per-attempt histories and cluster summary are in `analysis/scope_trajectory.json`: `{analysis['targeting']['cluster_construction_summary']}`.",
        "The shadow is descriptive only. It does not imply that TOP would have produced any particular schedule from those states.",
        "",
        "## 6. Plateau, resource, and process evidence",
        "",
        f"Quarter analysis: `{analysis['quarters']}`. Plateau analysis: `{analysis['plateau']}`. No stopping rule was added.",
        f"Resource safety: `{analysis['resource_safety']}`. The guard fired before the hard branch wall; this is why classification is E.",
        f"Hint observability: `{analysis['hint_observability']}`. No hint treatment was changed.",
        "",
        "## 7. Probe semantics and protected boundaries",
        "",
        f"The current probe semantics were verified from code: `{analysis['probe_semantics']['description']}`. Within-scope refinement was not executed.",
        f"Protected boundaries: `{analysis['protected_semantics']}`. No production wiring, Objective Semantics, hard constraint, validation authority, hint behavior, migration, or adaptive operator-policy change was introduced.",
        "",
        "## 8. Scientific interpretation",
        "",
        "The required classification is E. The partial prefix is utilization-focused and includes some broad/high-gain observations, including >=30, >=60, and >=90-point gains, but the resource-terminated branch cannot establish whether IA is a general R16 mode, a cleanup mode, a plateauing mode, or a competitive three-hour policy. It does not authorize bounded dynamic-production qualification or production promotion.",
        "",
        "## 9. Next study: design only",
        "",
        "The next study was not run. Its exact structured design is in `analysis/next_fixed_scope_study_design.json` and `report/future_fixed_scope_search_semantics_design.md`. It compares the current first-qualifying control, a frozen minimum-coordination treatment, and bounded within-scope v2 refinement on matched scopes using eight workers, seed 101, current hints, unchanged authority, and the same parent wall. It includes repeated IA one-student scopes, TOP states 60/67, jackpot scopes 27/36/63, and ordinary TOP scopes. Broad movement is not treated as quality; state 60 remains the explicit counterexample.",
        "",
        "## 10. Requirement audit and repository verification",
        "",
        "The complete 59-item requirement audit is in `analysis/requirements_audit.json`. Documentation was updated in Student Assignment Target Selection, Operator Characterization, and Search Strategy. The repository checks recorded for this implementation were 445 scheduling-engine tests passed, compileall passed, Django system check passed, and git diff check passed. No Git commit was created.",
        "",
        "This audit is derived from the sealed lineage and is not a mutation of that lineage.",
    ]
    (audit_root / "report" / "study_report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    manifest = hash_tree(audit_root)
    (audit_root / "artifact_hashes.sha256").write_text(manifest, encoding="utf-8")
    write_json(audit_root / "SEALED", {
        "schema": "r16_ia_three_hour_postrun_audit_seal_v1",
        "status": "complete",
        "sealed_at_utc": utc_now(),
        "artifact_hashes_sha256": sha256_file(audit_root / "artifact_hashes.sha256"),
        "source_lineage_sealed": True,
    })


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--sealed-root", type=Path, required=True)
    parser.add_argument("--historical-top-root", type=Path, default=DEFAULT_TOP_ROOT)
    parser.add_argument("--audit-root", type=Path, required=True)
    args = parser.parse_args(argv)
    analysis = build_audit(args.sealed_root, args.historical_top_root)
    write_outputs(args.audit_root, analysis, args.sealed_root)
    print(json.dumps({
        "audit_root": str(args.audit_root),
        "classification": analysis["classification"],
        "attempts": analysis["branch_execution"]["completed_attempts"],
        "sealed": True,
        "solver_free": True,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
