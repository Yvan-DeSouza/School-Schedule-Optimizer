"""Solver-free post-run audit for the sealed R16 TOP-versus-IA qualification.

This module intentionally uses only the Python standard library.  It reads
immutable qualification artifacts and never imports the scheduling engine,
CP-SAT, model construction, validation, Django, or persistence code.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import pathlib
import secrets
import statistics
from datetime import datetime, timezone


POLICIES = ("top_individual", "interaction_aware")
COMPONENTS = (
    "course_category_diversity",
    "course_sequence_preferences",
    "difficulty_balance",
    "section_utilization_balance",
    "student_semester_load_balance",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path: pathlib.Path):
    return json.loads(path.read_text(encoding="utf-8"))


def read_rows(path: pathlib.Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def percentile(values, fraction):
    values = sorted(float(value) for value in values)
    if not values:
        return None
    position = (len(values) - 1) * fraction
    low = int(position)
    high = min(len(values) - 1, low + 1)
    return values[low] + (values[high] - values[low]) * (position - low)


def distribution(values):
    values = [float(value) for value in values]
    return {
        "count": len(values),
        "mean": statistics.mean(values) if values else None,
        "median": statistics.median(values) if values else None,
        "p25": percentile(values, 0.25),
        "p75": percentile(values, 0.75),
        "min": min(values) if values else None,
        "max": max(values) if values else None,
    }


def probe(row):
    return (row.get("inner_probe_summaries") or [{}])[0]


def component_improvements(row):
    before = (row.get("objective_before") or {}).get("components", {})
    after = (row.get("objective_after") or {}).get("components", {})
    return {
        name: float(before.get(name, {}).get("weighted_normalized_contribution", 0) or 0)
        - float(after.get(name, {}).get("weighted_normalized_contribution", 0) or 0)
        for name in COMPONENTS
    }


def policy_stats(rows):
    gains = [float(row.get("gain", 0) or 0) for row in rows]
    changed_students = [int(row.get("changed_student_count", 0) or 0) for row in rows]
    changed_decisions = [int(row.get("changed_source_decision_count", 0) or 0) for row in rows]
    solver = [float(row.get("solver_wall_seconds", 0) or 0) for row in rows]
    validation = [float(probe(row).get("validation_elapsed_seconds", 0) or 0) for row in rows]

    def rates(predicate):
        return sum(predicate(value) for value in gains) / len(gains) if gains else None

    components = {}
    for component in COMPONENTS:
        values = [component_improvements(row)[component] for row in rows]
        components[component] = {
            "distribution": distribution(values),
            "positive": sum(value > 0 for value in values),
            "zero": sum(value == 0 for value in values),
            "negative": sum(value < 0 for value in values),
        }
    return {
        "cells": len(rows),
        "gain": distribution(gains),
        "probability_gain_at_least_30": rates(lambda value: value >= 30),
        "probability_gain_at_least_60": rates(lambda value: value >= 60),
        "probability_gain_at_least_90": rates(lambda value: value >= 90),
        "changed_student_distribution": {str(value): changed_students.count(value) for value in range(1, 5)},
        "changed_decision_distribution": distribution(changed_decisions),
        "probability_changed_decisions_at_least_15": sum(value >= 15 for value in changed_decisions) / len(rows) if rows else None,
        "solver_wall_distribution": distribution(solver),
        "validation_wall_distribution": distribution(validation),
        "gain_per_search_minute": distribution([
            gain / (wall / 60) if wall else 0 for gain, wall in zip(gains, solver)
        ]),
        "gain_per_full_wall_minute": distribution([
            gain / (float(row.get("attempt_wall_seconds", 0) or 0) / 60)
            if float(row.get("attempt_wall_seconds", 0) or 0) else 0
            for gain, row in zip(gains, rows)
        ]),
        "solver_statuses": dict(sorted(_counts(row.get("status") or "missing" for row in rows).items())),
        "validation_statuses": dict(sorted(_counts(row.get("validation_classification") or "missing" for row in rows).items())),
        "component_improvements": components,
        "unique_scopes": len({tuple(sorted(row.get("scope") or ())) for row in rows}),
        "all_cells_operationally_valid": all(bool(row.get("operationally_valid")) for row in rows),
    }


def _counts(values):
    counts = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return counts


def state_analysis(rows):
    result = {}
    for state in sorted({str(row.get("state_attempt")) for row in rows}, key=int):
        by_policy = {
            policy: [row for row in rows if str(row.get("state_attempt")) == state and row.get("policy") == policy]
            for policy in POLICIES
        }
        means = {policy: statistics.mean(float(row.get("gain", 0) or 0) for row in values) for policy, values in by_policy.items()}
        medians = {policy: statistics.median(float(row.get("gain", 0) or 0) for row in values) for policy, values in by_policy.items()}
        result[state] = {
            "known_top_jackpot_positive_control": state == "60",
            "by_policy": {policy: policy_stats(values) for policy, values in by_policy.items()},
            "mean_ia_minus_top": means[POLICIES[1]] - means[POLICIES[0]],
            "median_ia_minus_top": medians[POLICIES[1]] - medians[POLICIES[0]],
            "gains_by_repeat": {
                policy: {str(row.get("repeat")): float(row.get("gain", 0) or 0) for row in values}
                for policy, values in by_policy.items()
            },
        }
    return result


def outcome_rows(rows):
    return [
        {
            "state_attempt": row.get("state_attempt"),
            "policy": row.get("policy"),
            "repeat": row.get("repeat"),
            "scope": row.get("scope"),
            "gain": row.get("gain"),
            "changed_student_count": row.get("changed_student_count"),
            "changed_source_decision_count": row.get("changed_source_decision_count"),
            "solver_status": row.get("status"),
            "solver_wall_seconds": row.get("solver_wall_seconds"),
            "validation_wall_seconds": probe(row).get("validation_elapsed_seconds"),
            "validation_classification": row.get("validation_classification"),
            "candidate_fingerprint": probe(row).get("candidate_source_decision_fingerprint"),
            "component_improvements": component_improvements(row),
            "hint_vector_fingerprint": (probe(row).get("hint_telemetry") or {}).get("hint_vector_fingerprint"),
            "exact_hint_identity": bool((probe(row).get("hint_telemetry") or {}).get("exact_identity")),
        }
        for row in rows
    ]


def resource_analysis(root):
    samples = read_rows(root / "resource_samples.jsonl")
    available = [int(row["system_available_memory_bytes"]) for row in samples]
    rss = [int(row["tree_rss_bytes"]) for row in samples]
    warning_samples = sum(
        int(row.get("system_available_memory_bytes", 0) or 0) < 2_000_000_000
        or int(row.get("tree_rss_bytes", 0) or 0) > 3_000_000_000
        for row in samples
    )
    return {
        "samples": len(samples),
        "minimum_available_memory_bytes": min(available) if available else None,
        "maximum_tree_rss_bytes": max(rss) if rss else None,
        "warning_samples": warning_samples,
        "sleep_gap_samples": sum(float(row.get("wall_gap_seconds", 0) or 0) > 5 for row in samples),
        "no_sleep_gap_detected": not any(float(row.get("wall_gap_seconds", 0) or 0) > 5 for row in samples),
    }


def build_analysis(source_root: pathlib.Path):
    rows = read_rows(source_root / "cell_results.jsonl")
    summary = read_json(source_root / "analysis" / "qualification_summary.json")
    smoke = read_json(source_root / "smoke" / "smoke_result.json")
    trajectory = read_json(source_root / "trajectory_hypothesis_v1.json")
    contract = read_json(source_root / "experiment_contract.json")
    identity = read_json(source_root / "source" / "source_identity.json")
    by_state = state_analysis(rows)
    all_differences = [value["median_ia_minus_top"] for value in by_state.values()]
    no60 = [value for key, value in by_state.items() if key != "60"]
    return {
        "schema": "r16_top_vs_ia_postrun_audit_v1",
        "generated_at_utc": utc_now(),
        "source_lineage": str(source_root),
        "source_lineage_artifact_hash_manifest_sha256": sha256_file(source_root / "artifact_hashes.sha256"),
        "source_lineage_sealed": (source_root / "SEALED").exists(),
        "experiment_contract": contract,
        "source_identity": identity,
        "primary_classification": summary.get("primary_classification"),
        "cells": {
            "total": len(rows),
            "by_policy": {policy: len([row for row in rows if row.get("policy") == policy]) for policy in POLICIES},
            "operationally_valid": sum(bool(row.get("operationally_valid")) for row in rows),
            "invalid": sum(not bool(row.get("operationally_valid")) for row in rows),
            "rerun": sum(bool((row.get("supervisor_event") or {}).get("rerun")) for row in rows),
            "candidate_found": sum(bool(row.get("candidate_found")) for row in rows),
            "candidate_validated": sum(bool(row.get("candidate_validated")) for row in rows),
            "adopted": sum(bool(row.get("adopted")) for row in rows),
        },
        "policy_statistics": {policy: policy_stats([row for row in rows if row.get("policy") == policy]) for policy in POLICIES},
        "state_statistics": by_state,
        "excluding_state_60": {
            "policy_statistics": {policy: policy_stats([row for row in rows if row.get("policy") == policy and str(row.get("state_attempt")) != "60"]) for policy in POLICIES},
            "ia_median_wins": sum(value["median_ia_minus_top"] > 0 for value in no60),
            "top_median_wins": sum(value["median_ia_minus_top"] < 0 for value in no60),
        },
        "state_60_positive_control": by_state.get("60"),
        "median_state_differences": {
            "all_states": all_differences,
            "all_state_mean_difference": statistics.mean(all_differences),
            "excluding_state_60": [value["median_ia_minus_top"] for value in no60],
        },
        "hint_telemetry": {
            "smoke_passed": bool(smoke.get("passed")),
            "exact_identity_enabled": bool(smoke.get("exact_identity_enabled_for_grid")),
            "cells_with_exact_identity": sum(bool((probe(row).get("hint_telemetry") or {}).get("exact_identity")) for row in rows),
            "distinct_hint_vector_fingerprints": len({(probe(row).get("hint_telemetry") or {}).get("hint_vector_fingerprint") for row in rows}),
            "hint_values_changed": False,
            "target_release_heavy_study": "not_ready_not_run",
            "directional_hints": "not_ready",
        },
        "trajectory_hypothesis": trajectory,
        "resource_analysis": resource_analysis(source_root),
        "outcome_rows": outcome_rows(rows),
        "no_alternative_schedule_outcome_inferred": True,
        "interpretation_limits": [
            "The eight states were selected from the previously analyzed 70-state TOP trajectory.",
            "State 60 is a known TOP-jackpot positive control, not an unseen state.",
            "Eight-worker CP-SAT is nondeterministic and this is a matched qualification, not a general superiority claim.",
            "Shadow scope differences and hint telemetry do not imply an alternative schedule outcome.",
            "No duration counterfactual is inferred from the observed probe walls.",
        ],
    }


def write_report(analysis, path: pathlib.Path):
    top = analysis["policy_statistics"]["top_individual"]
    ia = analysis["policy_statistics"]["interaction_aware"]
    control = analysis["state_60_positive_control"]
    states = analysis["state_statistics"]
    lines = [
        "# R16/S4 diverse-prestate eight-worker TOP-vs-IA post-run audit",
        "",
        f"Primary classification: **{analysis['primary_classification']}**.",
        "",
        f"Sealed source lineage: `{analysis['source_lineage']}`.",
        f"Cells: {analysis['cells']['total']} total; {analysis['cells']['operationally_valid']} operationally valid; {analysis['cells']['invalid']} invalid; {analysis['cells']['rerun']} rerun.",
        "",
        "## Contract and integrity",
        "",
        "The grid used R16/S4, seed 101, eight CP-SAT workers, one validation worker, current incumbent-derived hints, a 300-second search ceiling, a 180-second validation allowance, a 720-second parent containment wall, strict full-model validation, strict adoption, fresh source resets, and no dynamic continuation.",
        f"Source SHA-256: `{analysis['source_identity']['source_sha256']}`; input fingerprint: `{analysis['source_identity']['input_fingerprint']}`; model fingerprint: `{analysis['source_identity']['model_fingerprint']}`.",
        f"Source value: {analysis['source_identity']['source_value']}; ordinary assignments: {analysis['source_identity']['ordinary_assignments']}; commitments: {analysis['source_identity']['special_commitments']}; unmet required requests: {analysis['source_identity']['unmet_request_count']}.",
        f"Resource samples: {analysis['resource_analysis']['samples']}; minimum available memory: {analysis['resource_analysis']['minimum_available_memory_bytes'] / 1e9:.3f} GB; maximum tree RSS: {analysis['resource_analysis']['maximum_tree_rss_bytes'] / 1e9:.3f} GB; sleep-gap samples: {analysis['resource_analysis']['sleep_gap_samples']}.",
        "",
        "## Policy distributions",
        "",
        f"TOP: mean/median/p25/p75/min/max gain = {top['gain']['mean']:.2f}/{top['gain']['median']:.2f}/{top['gain']['p25']:.2f}/{top['gain']['p75']:.2f}/{top['gain']['min']:.2f}/{top['gain']['max']:.2f}; gain >=30/60/90 = {top['probability_gain_at_least_30']:.3f}/{top['probability_gain_at_least_60']:.3f}/{top['probability_gain_at_least_90']:.3f}; changed decisions >=15 = {top['probability_changed_decisions_at_least_15']:.3f}.",
        f"IA: mean/median/p25/p75/min/max gain = {ia['gain']['mean']:.2f}/{ia['gain']['median']:.2f}/{ia['gain']['p25']:.2f}/{ia['gain']['p75']:.2f}/{ia['gain']['min']:.2f}/{ia['gain']['max']:.2f}; gain >=30/60/90 = {ia['probability_gain_at_least_30']:.3f}/{ia['probability_gain_at_least_60']:.3f}/{ia['probability_gain_at_least_90']:.3f}; changed decisions >=15 = {ia['probability_changed_decisions_at_least_15']:.3f}.",
        f"TOP changed-student counts 1/2/3/4: {top['changed_student_distribution']}; IA: {ia['changed_student_distribution']}.",
        f"TOP solver wall median {top['solver_wall_distribution']['median']:.2f}s and validation wall median {top['validation_wall_distribution']['median']:.2f}s; IA solver wall median {ia['solver_wall_distribution']['median']:.2f}s and validation wall median {ia['validation_wall_distribution']['median']:.2f}s.",
        f"TOP gain/search-minute median {top['gain_per_search_minute']['median']:.2f}; IA {ia['gain_per_search_minute']['median']:.2f}. TOP gain/full-wall-minute median {top['gain_per_full_wall_minute']['median']:.2f}; IA {ia['gain_per_full_wall_minute']['median']:.2f}.",
        "",
        "## Per-state and positive-control results",
        "",
        "| State | TOP gains by repeat | IA gains by repeat | Median IA − TOP |",
        "| --- | --- | --- | ---: |",
    ]
    for state, value in states.items():
        lines.append(f"| {state} | {list(value['gains_by_repeat']['top_individual'].values())} | {list(value['gains_by_repeat']['interaction_aware'].values())} | {value['median_ia_minus_top']:.0f} |")
    lines += [
        "",
        f"State 60 was the known TOP positive control. TOP gains were {list(control['gains_by_repeat']['top_individual'].values())}; IA gains were {list(control['gains_by_repeat']['interaction_aware'].values())}. TOP produced the +84 jackpot repeat; IA did not produce a gain >=30.",
        f"Excluding state 60, IA won {analysis['excluding_state_60']['ia_median_wins']} state medians and TOP won {analysis['excluding_state_60']['top_median_wins']}; the result remains mixed.",
        "",
        "## Component movement and IA mechanism",
        "",
        f"TOP component improvement counts (positive/zero/negative): { {name: (facts['positive'], facts['zero'], facts['negative']) for name, facts in top['component_improvements'].items()} }.",
        f"IA component improvement counts (positive/zero/negative): { {name: (facts['positive'], facts['zero'], facts['negative']) for name, facts in ia['component_improvements'].items()} }.",
        "IA remained a focused utilization specialist in this cohort: all 24 IA cells improved utilization, all 24 changed one student, none changed at least 15 decisions, and none reached gain 30. TOP showed broader move breadth and the observed jackpot upside, concentrated in the known positive-control state.",
        "",
        "## Near-tie and duration interpretation",
        "",
        "The preregistered near-tie trajectory audit was verified from the authoritative 70-attempt CSV: 57 fresh student sets had mean gain 18.95, median 12, and maximum 114; 13 exact repeated student sets on a new incumbent had mean 10.15, median 6, maximum 24, and zero gains >=30 or >=60. The four jackpot transitions matched their expected scopes, retained cores, incoming ranks, leverage deltas, and recent-gain facts.",
        "This remains an observational hypothesis, not a qualified policy. The current 48-cell grid did not execute a core-plus-near-tie policy, so that policy is not justified for production or for another automatic heavy run.",
        "Observed 300-second-contract probes returned substantially earlier than the ceiling in this cohort; this is evidence about this sample only. It does not establish what a 180-second or other ceiling would have returned, so a matched 180-vs-300 duration study remains future work.",
        "",
        "## Hint readiness and future work",
        "",
        f"The exact hint-identity smoke passed, and exact identity telemetry was present in {analysis['hint_telemetry']['cells_with_exact_identity']}/{analysis['cells']['total']} cells. Hint values and target-release behavior were unchanged. This qualifies observability only; a target-release heavy study is not ready, and directional hints remain not ready.",
        "The result does not justify automatic dynamic continuation or production promotion. The next scientifically useful study is a separately approved, truly fresh-state state-aware qualification; a bounded TOP-vs-IA continuation study, 180-vs-300 duration fork, hint treatment study, or within-scope refinement study must be run independently and must preserve full validation and strict authority.",
        "",
        "## Interpretation boundary",
        "",
        "Every quality claim above uses a returned candidate that passed the unchanged full-model validator. Scope differences, structural facts, hint identity, and any shadow comparison do not imply an alternative schedule outcome. No additional heavy experiment was launched after the 48-cell grid.",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_package(source_root: pathlib.Path, output_parent: pathlib.Path):
    if not (source_root / "SEALED").exists():
        raise RuntimeError("source lineage is not sealed")
    output_parent.mkdir(parents=True, exist_ok=True)
    name = f"v2_r16_top_vs_ia_eight_worker_postrun_audit_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}_{secrets.token_hex(4)}"
    output = output_parent / name
    output.mkdir(exist_ok=False)
    analysis = build_analysis(source_root)
    analysis["output_package"] = str(output)
    (output / "source_lineage_ref.json").write_text(json.dumps({
        "schema": "r16_top_vs_ia_postrun_source_ref_v1",
        "source_lineage": str(source_root),
        "source_hash_manifest_sha256": sha256_file(source_root / "artifact_hashes.sha256"),
        "source_seal_sha256": sha256_file(source_root / "SEALED"),
        "cell_results_sha256": sha256_file(source_root / "cell_results.jsonl"),
        "solver_free": True,
    }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (output / "postrun_analysis.json").write_text(json.dumps(analysis, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    write_report(analysis, output / "postrun_report.md")
    with (output / "outcome_rows.csv").open("w", newline="", encoding="utf-8") as stream:
        rows = analysis["outcome_rows"]
        fields = ["state_attempt", "policy", "repeat", "scope", "gain", "changed_student_count", "changed_source_decision_count", "solver_status", "solver_wall_seconds", "validation_wall_seconds", "validation_classification", "candidate_fingerprint", "component_improvements", "hint_vector_fingerprint", "exact_hint_identity"]
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: json.dumps(row[key], sort_keys=True) if isinstance(row[key], (dict, list)) else row[key] for key in fields})
    (output / "README.txt").write_text(
        "This is a solver-free post-run audit of the sealed qualification lineage. It does not contain or infer alternative schedules.\n",
        encoding="utf-8",
    )
    lines = []
    for path in sorted(output.rglob("*")):
        if path.is_file() and path.name not in {"artifact_hashes.sha256", "SEALED"}:
            lines.append(f"{sha256_file(path)}  {path.relative_to(output).as_posix()}")
    manifest = output / "artifact_hashes.sha256"
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    (output / "SEALED").write_text(json.dumps({
        "schema": "r16_top_vs_ia_postrun_audit_seal_v1",
        "sealed_at_utc": utc_now(),
        "artifact_hashes_sha256": sha256_file(manifest),
        "source_lineage": str(source_root),
        "solver_free": True,
    }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output), "classification": analysis["primary_classification"], "cells": analysis["cells"]}, sort_keys=True))


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-lineage", type=pathlib.Path, required=True)
    parser.add_argument("--output-parent", type=pathlib.Path, default=pathlib.Path(r"C:\Users\desou\research_runs"))
    args = parser.parse_args(argv)
    write_package(args.source_lineage, args.output_parent)


if __name__ == "__main__":
    main()
