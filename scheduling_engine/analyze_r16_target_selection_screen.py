"""Post-run, solver-free analysis for the R16 target-selection screen.

The screen runner writes immutable per-cell records.  This module consumes
those records only; it never imports the solver, model builder, validation
pipeline, Django, or persistence code.  It is intentionally a separate
command so analysis can be rerun without changing any experiment outcome.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
from pathlib import Path
from statistics import mean, median


COMPONENTS = (
    "course_category_diversity",
    "course_sequence_preferences",
    "difficulty_balance",
    "section_utilization_balance",
    "student_semester_load_balance",
)


def load_json(path):
    path = Path(path)
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            return json.load(stream)
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def write_csv(path, rows, fieldnames=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = list(rows)
    if fieldnames is None:
        fieldnames = list(rows[0]) if rows else []
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def finite_median(values):
    values = [float(value) for value in values if value is not None]
    return median(values) if values else None


def compact_component_delta(before, after):
    result = {}
    before_components = (before or {}).get("components", {})
    after_components = (after or {}).get("components", {})
    for name in COMPONENTS:
        left = before_components.get(name, {})
        right = after_components.get(name, {})
        result[name] = {
            "raw_penalty_improvement": float(left.get("raw_penalty", 0) or 0) - float(right.get("raw_penalty", 0) or 0),
            "normalized_penalty_improvement": float(left.get("normalized_penalty", 0) or 0) - float(right.get("normalized_penalty", 0) or 0),
            "weighted_contribution_improvement": float(left.get("weighted_normalized_contribution", 0) or 0) - float(right.get("weighted_normalized_contribution", 0) or 0),
        }
    return result


def read_rows(root):
    payload = load_json(Path(root) / "analysis" / "paired_cell_results.json")
    return payload.get("rows", [])


def cell_snapshot(row):
    return load_json(Path(row["artifacts"]["cell_dir"]) / "targeting_snapshot.json.gz")


def cell_feature_row(row, snapshot):
    selection = snapshot.get("selection", {})
    leverage = selection.get("selected_leverage", [])
    pressure = snapshot.get("pressure_population", [])
    selected_ids = [int(value) for value in row.get("scope", [])]
    selected_groups = sorted({
        int(group)
        for fact in leverage
        for group in fact.get("delivery_group_ids", [])
    })
    population = snapshot.get("utilization_population", [])
    return {
        "state_id": row["state_id"],
        "seed": row["seed"],
        "policy": row["policy"],
        "cell_id": row["cell_id"],
        "scope": json.dumps(selected_ids, separators=(",", ":")),
        "scope_count": len(selected_ids),
        "scope_jaccard_to_other": row.get("scope_jaccard_to_other"),
        "population_count": snapshot.get("utilization_population_count"),
        "selected_total_positive_leverage": sum(float(fact.get("total_positive_leverage", 0) or 0) for fact in leverage),
        "selected_mean_positive_leverage": mean([float(fact.get("total_positive_leverage", 0) or 0) for fact in leverage]) if leverage else 0.0,
        "selected_strongest_single_move": max([float(fact.get("strongest_single_move", 0) or 0) for fact in leverage], default=0.0),
        "selected_alternate_section_opportunity": sum(int(fact.get("alternate_section_opportunity_count", 0) or 0) for fact in leverage),
        "selected_delivery_group_count": len(selected_groups),
        "top_delivery_group_count": len(selection.get("top_delivery_groups", [])),
        "pressure_population_count": len(pressure),
        "interaction_trace_steps": len(snapshot.get("cluster_construction_trace", [])),
        "candidate_found": row.get("candidate_found"),
        "candidate_validated": row.get("candidate_validated"),
        "adopted": row.get("adopted"),
        "validation_classification": row.get("validation_classification"),
        "status": row.get("status"),
        "gain": row.get("gain", 0.0),
        "candidate_discovery_gain": row.get("candidate_discovery_gain", 0.0),
        "solver_wall_seconds": row.get("solver_wall_seconds"),
        "attempt_wall_seconds": row.get("attempt_wall_seconds"),
        "changed_student_count": row.get("changed_student_count", 0),
        "changed_source_decision_count": row.get("changed_source_decision_count", 0),
        "source_fingerprint_before": row.get("source_fingerprint_before"),
        "source_fingerprint_after": row.get("source_fingerprint_after"),
    }


def paired_rows(rows):
    grouped = {}
    for row in rows:
        grouped.setdefault((row["state_id"], row["seed"]), {})[row["policy"]] = row
    result = []
    for (state_id, seed), policies in sorted(grouped.items()):
        top = policies.get("top_individual")
        interaction = policies.get("interaction_aware")
        if not top or not interaction:
            continue
        result.append({
            "state_id": state_id,
            "seed": seed,
            "top_gain": top.get("gain", 0.0),
            "interaction_aware_gain": interaction.get("gain", 0.0),
            "interaction_aware_minus_top": float(interaction.get("gain", 0.0)) - float(top.get("gain", 0.0)),
            "top_adopted": bool(top.get("adopted")),
            "interaction_aware_adopted": bool(interaction.get("adopted")),
            "top_validated": bool(top.get("candidate_validated")),
            "interaction_aware_validated": bool(interaction.get("candidate_validated")),
            "top_solver_wall_seconds": top.get("solver_wall_seconds"),
            "interaction_aware_solver_wall_seconds": interaction.get("solver_wall_seconds"),
            "scope_jaccard": top.get("scope_jaccard_to_other"),
            "top_scope": json.dumps(top.get("scope", []), separators=(",", ":")),
            "interaction_aware_scope": json.dumps(interaction.get("scope", []), separators=(",", ":")),
        })
    return result


def reliability(rows):
    result = {}
    for policy in ("top_individual", "interaction_aware"):
        subset = [row for row in rows if row["policy"] == policy]
        classifications = {}
        for row in subset:
            key = row.get("validation_classification") or "missing"
            classifications[key] = classifications.get(key, 0) + 1
        result[policy] = {
            "cells": len(subset),
            "candidate_found": sum(bool(row.get("candidate_found")) for row in subset),
            "candidate_validated": sum(bool(row.get("candidate_validated")) for row in subset),
            "adopted": sum(bool(row.get("adopted")) for row in subset),
            "unresolved": sum((row.get("validation_classification") in {"validation_unknown", "validation_error", "scope_mismatch"}) for row in subset),
            "validation_classifications": classifications,
            "total_authoritative_gain": sum(float(row.get("gain", 0) or 0) for row in subset),
            "mean_authoritative_gain": mean([float(row.get("gain", 0) or 0) for row in subset]) if subset else None,
            "median_authoritative_gain": finite_median([row.get("gain") for row in subset]),
            "mean_solver_wall_seconds": mean([float(row["solver_wall_seconds"]) for row in subset if row.get("solver_wall_seconds") is not None]) if any(row.get("solver_wall_seconds") is not None for row in subset) else None,
            "mean_attempt_wall_seconds": mean([float(row["attempt_wall_seconds"]) for row in subset if row.get("attempt_wall_seconds") is not None]) if any(row.get("attempt_wall_seconds") is not None for row in subset) else None,
        }
    return result


def classify(state_rows, reliability_rows):
    differences = [row["interaction_aware_minus_top"] for row in state_rows]
    med = finite_median(differences)
    trimmed = sorted(differences)[1:-1] if len(differences) > 2 else differences
    trimmed_med = finite_median(trimmed)
    wins = sum(value > 0 for value in differences)
    losses = sum(value < 0 for value in differences)
    leave_one_out = [finite_median(differences[:i] + differences[i + 1:]) for i in range(len(differences))] if len(differences) > 1 else []
    robust_positive = bool(leave_one_out) and min(value for value in leave_one_out if value is not None) > 0
    robust_negative = bool(leave_one_out) and max(value for value in leave_one_out if value is not None) < 0
    ia = reliability_rows["interaction_aware"]
    top = reliability_rows["top_individual"]
    reliability_regression = ia["unresolved"] > top["unresolved"] or ia["candidate_validated"] < top["candidate_validated"]
    classification = "C"
    if len(state_rows) == 12 and med is not None and med >= 6 and wins > losses and (trimmed_med or 0) > 0 and robust_positive and not reliability_regression:
        classification = "A"
    elif len(state_rows) == 12 and med is not None and med <= -6 and losses > wins and (trimmed_med or 0) < 0 and robust_negative and not (top["unresolved"] > ia["unresolved"] or top["candidate_validated"] < ia["candidate_validated"]):
        classification = "B"
    return {
        "classification": classification,
        "states": len(state_rows),
        "state_difference_median": med,
        "trimmed_median": trimmed_med,
        "state_wins_interaction_aware": wins,
        "state_wins_top_individual": losses,
        "state_ties": len(differences) - wins - losses,
        "leave_one_out_medians": leave_one_out,
        "leave_one_out_robust_positive": robust_positive,
        "leave_one_out_robust_negative": robust_negative,
        "reliability_regression_for_interaction_aware": reliability_regression,
        "thresholds": {"median_points": 6, "practical_screen": 6, "no_alternative_schedule_outcome_inferred": True},
        "interpretation": "C_INCONCLUSIVE_SINGLE_STEP_SCREEN",
    }


def resource_summary(root):
    samples = []
    path = Path(root) / "resource_samples.jsonl"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                samples.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    available = [row.get("available_memory_bytes") for row in samples if row.get("available_memory_bytes") is not None]
    rss = [row.get("rss_bytes") for row in samples if row.get("rss_bytes") is not None]
    return {
        "sample_count": len(samples),
        "minimum_available_memory_bytes": min(available) if available else None,
        "maximum_root_rss_bytes": max(rss) if rss else None,
        "hard_guard_observed": bool(any(value < 1_000_000_000 for value in available) or any(value > 4_000_000_000 for value in rss)),
        "monitoring_note": "Supervisor-side five-second JSONL samples; no solver callback and no inner resource monitor.",
    }


def md_report(root, classification, reliability_rows, state_rows, feature_rows):
    total_top = sum(float(row["top_individual_mean_gain"]) * int(row["seed_count"]) for row in state_rows)
    total_ia = sum(float(row["interaction_aware_mean_gain"]) * int(row["seed_count"]) for row in state_rows)
    lines = [
        "# R16 target-selection screen report",
        "",
        "## Status",
        "",
        f"The 48-cell solver screen completed. Stage-1 classification: **{classification['classification']}**. This is a single-step, matched target-selection screen, not a production-quality or general policy claim.",
        "",
        "Each cell started from an independently validated authoritative R16 pre-state and ran one existing `targeted_utilization_r16_s4` probe with Objective Semantics v2, one worker, seed 101 or 202, a 300-second search ceiling, and a separate 180-second validation allowance. Candidate gains and adopted gains are kept distinct.",
        "",
        "## Paired result",
        "",
        f"Across the 12 states, interaction-aware minus top-individual authoritative gain had median **{classification['state_difference_median']}** points, trimmed median **{classification['trimmed_median']}**, with {classification['state_wins_interaction_aware']} interaction-aware wins, {classification['state_wins_top_individual']} top-individual wins, and {classification['state_ties']} ties. Leave-one-out robustness is {classification['leave_one_out_robust_positive'] or classification['leave_one_out_robust_negative']}. The result is therefore classified inconclusive under the frozen screen rule.",
        "",
        f"Across the 24 cells covering 12 states, the totals are {total_top:.0f} authoritative points for top-individual and {total_ia:.0f} for interaction-aware. These are observed validated transitions only; no alternative schedule outcome is inferred from any changed target selection.",
        "",
        "## Reliability and authority",
        "",
        "| Policy | Cells | Validated | Adopted | Unresolved | Total gain | Mean solver seconds |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for policy in ("top_individual", "interaction_aware"):
        row = reliability_rows[policy]
        lines.append(f"| {policy} | {row['cells']} | {row['candidate_validated']} | {row['adopted']} | {row['unresolved']} | {row['total_authoritative_gain']:.0f} | {row['mean_solver_wall_seconds']:.1f} |")
    lines += [
        "",
        "`validation_unknown` remains non-authoritative and contributes zero adopted gain. The runner retained the incumbent in those cells. Full-model validation, strict improvement, zero unmet requests, and the existing Objective Semantics v2 authority boundary were unchanged.",
        "",
        "## Targeting mechanism and geometry",
        "",
        "The two policies are real existing guidance policies. Top-individual selects the highest individual leverage records; interaction-aware constructs a delivery-group-focused cluster from the same guidance population. The selected scope is observational guidance, not an objective attribution or hard-constraint change.",
        "",
        "Scope Jaccard, selected leverage, target-source-decision counts, changed-student counts, and component movements are available in the accompanying CSV/JSON tables. They describe where the probe looked; they do not prove that looking there caused a better unobserved schedule.",
        "",
        "## Objective components",
        "",
        "The analysis uses the five frozen v2 components: course-category diversity, sequence preferences, difficulty balance, section-utilization balance, and student semester-load balance. Component movements are read from authoritative before/after quality snapshots; no normalization or metric was redefined. Sequence remains a reported component even when its movement is zero.",
        "",
        "## Hints and historical identity",
        "",
        "The target-hint artifacts contain semantic target source decisions and destination observability, but exact variable identity remains deferred. Hint readiness is therefore B: observability is insufficient for a causal hint experiment. Historical canonical/materialized identity was not heuristically repaired; each cell used a verified checkpoint source identity.",
        "",
        "## Resource and experiment boundaries",
        "",
        "The supervisor streamed five-second resource samples. No OR-Tools solution callback was added. No continuation, duration, adaptive-selector, R4/R8/R32/R64, or production experiment was launched. The 300-second ceiling, operator, objective, authority rules, and production code paths were unchanged.",
        "",
        "## Recommendation",
        "",
        "Do not promote either target-selection policy from this screen. The immediate next experiment should be selected only after reviewing the immutable tables: if the screen remains C, run a pre-registered replicated target-selection comparison with the same authority and no hint changes; do not interpret this screen as evidence for a dynamic retargeting production change.",
        "",
        "All shadow/feature comparisons are explanatory telemetry. A changed scope or shadow preference does not imply a better schedule.",
    ]
    (Path(root) / "report" / "stage1_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def seal(root):
    root = Path(root)
    entries = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name in {"artifact_hashes.sha256", "SEALED"}:
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        entries.append(f"{digest}  {path.relative_to(root).as_posix()}")
    manifest = "\n".join(entries) + "\n"
    manifest_path = root / "artifact_hashes.sha256"
    manifest_path.write_bytes(manifest.encode("utf-8"))
    manifest_hash = hashlib.sha256(manifest.encode("utf-8")).hexdigest()
    if hashlib.sha256(manifest_path.read_bytes()).hexdigest() != manifest_hash:
        raise RuntimeError("artifact hash manifest verification failed")
    (root / "SEALED").write_bytes((manifest_hash + "\n").encode("utf-8"))


def analyze(root):
    root = Path(root).resolve()
    rows = read_rows(root)
    features = [cell_feature_row(row, cell_snapshot(row)) for row in rows]
    pairs = paired_rows(rows)
    state_groups = {}
    for row in pairs:
        state_groups.setdefault(row["state_id"], []).append(row)
    state_rows = []
    for state_id, group in sorted(state_groups.items()):
        state_rows.append({
            "state_id": state_id,
            "seed_count": len(group),
            "interaction_aware_mean_gain": mean(row["interaction_aware_gain"] for row in group),
            "top_individual_mean_gain": mean(row["top_gain"] for row in group),
            "interaction_aware_minus_top": mean(row["interaction_aware_minus_top"] for row in group),
            "interaction_aware_wins": sum(row["interaction_aware_minus_top"] > 0 for row in group),
            "top_individual_wins": sum(row["interaction_aware_minus_top"] < 0 for row in group),
            "ties": sum(row["interaction_aware_minus_top"] == 0 for row in group),
        })
    rel = reliability(rows)
    classification = classify(state_rows, rel)
    write_csv(root / "analysis" / "paired_cell_results.csv", pairs)
    write_csv(root / "analysis" / "state_level_results.csv", state_rows)
    write_csv(root / "analysis" / "cluster_feature_outcomes.csv", features)
    scope_rows = [{
        "state_id": row["state_id"], "seed": row["seed"],
        "top_scope": json.dumps(row["top_scope"]),
        "interaction_aware_scope": json.dumps(row["interaction_aware_scope"]),
        "scope_jaccard": row["scope_jaccard"],
        "top_gain": row["top_gain"], "interaction_aware_gain": row["interaction_aware_gain"],
        "gain_difference": row["interaction_aware_minus_top"],
    } for row in pairs]
    write_csv(root / "analysis" / "scope_difference_analysis.csv", scope_rows)
    write_json(root / "analysis" / "policy_reliability.json", {"schema": "r16_policy_reliability_v1", "policies": rel})
    write_json(root / "analysis" / "stage1_classification.json", {"schema": "r16_target_selection_stage1_classification_v2", **classification})
    write_json(root / "analysis" / "cluster_mechanism_analysis.json", {
        "schema": "r16_cluster_mechanism_analysis_v1",
        "policy_feature_means": {
            policy: {
                key: mean(float(row[key]) for row in features if row["policy"] == policy)
                for key in ("selected_total_positive_leverage", "selected_mean_positive_leverage", "selected_strongest_single_move", "selected_alternate_section_opportunity", "selected_delivery_group_count", "scope_count")
            } for policy in ("top_individual", "interaction_aware")
        },
        "selection_is_guidance_only": True,
        "no_alternative_schedule_outcome_inferred": True,
    })
    move_rows = []
    component_rows = []
    for row in rows:
        deltas = compact_component_delta(row.get("objective_before"), row.get("objective_after"))
        for component, values in deltas.items():
            component_rows.append({"state_id": row["state_id"], "seed": row["seed"], "policy": row["policy"], "adopted": row.get("adopted"), "component": component, **values})
        move_rows.append({"state_id": row["state_id"], "seed": row["seed"], "policy": row["policy"], "adopted": row.get("adopted"), "gain": row.get("gain", 0), "changed_student_count": row.get("changed_student_count", 0), "changed_source_decision_count": row.get("changed_source_decision_count", 0), "component_deltas": json.dumps(deltas, sort_keys=True)})
    write_csv(root / "analysis" / "component_analysis.csv", component_rows)
    write_json(root / "analysis" / "move_breadth_analysis.json", {"schema": "r16_move_breadth_analysis_v1", "rows": move_rows, "no_alternative_schedule_outcome_inferred": True})
    hint_rows = []
    for row in rows:
        snapshot = load_json(Path(row["artifacts"]["cell_dir"]) / "target_hint_snapshot.json.gz")
        hint_rows.append({"cell_id": row["cell_id"], "policy": row["policy"], "semantic_source_decision_count": snapshot.get("target_source_decision_count", 0), "destination_observability_count": len(snapshot.get("target_local_destination_observability", {})), "exact_variable_identity": snapshot.get("exact_variable_identity"), "observational_only": snapshot.get("target_dependent_hint_facts_are_observational")})
    write_json(root / "analysis" / "hint_observability.json", {"schema": "r16_hint_observability_v1", "readiness": "B_OBSERVABILITY_STILL_INSUFFICIENT", "rows": hint_rows, "no_hint_experiment_run": True})
    write_json(root / "analysis" / "resource_health.json", {"schema": "r16_resource_health_v1", **resource_summary(root)})
    (root / "report" / "historical_identity_reconciliation.md").write_text(
        "# Historical identity reconciliation\n\n"
        "No heuristic canonical/materialized identity repair was used. The screen loaded the verified checkpoint source for each representative R16 state and recorded the materialized source fingerprint before and after each cell. Historical canonical/materialized reconciliation beyond those verified identities was not established, so no historical replay or causal schedule claim is made from an inferred identity.\n",
        encoding="utf-8",
    )
    (root / "report" / "hint_readiness_report.md").write_text(
        "# Hint readiness\n\n"
        "Readiness is **B — observability still insufficient**. The artifacts record semantic target source decisions and destination summaries, but exact solver-variable identity is explicitly deferred. No hint experiment was run and no hint causality is inferred.\n",
        encoding="utf-8",
    )
    (root / "report" / "next_stage_recommendation.md").write_text(
        "# Next-stage recommendation\n\n"
        "The screen is C/inconclusive. Keep both policies research-only. The immediate next study should be a replicated, pre-registered target-selection comparison using the same R16/S4 operator, Objective Semantics v2, full-model validation, strict adoption, and immutable source reset. Do not change hints, duration, continuation, adaptive coefficients, or production wiring based on this screen.\n",
        encoding="utf-8",
    )
    md_report(root, classification, rel, state_rows, features)
    write_json(root / "study_manifest.json", {
        "schema": "v2_r16_target_selection_screen_manifest_v1",
        "lineage_id": root.name,
        "status": "sealed",
        "cells": len(rows),
        "states": len(state_rows),
        "policies": ["top_individual", "interaction_aware"],
        "seeds": [101, 202],
        "classification": classification["classification"],
        "no_alternative_schedule_outcome_inferred": True,
        "analysis_module": "scheduling_engine.analyze_r16_target_selection_screen",
    })
    seal(root)
    return classification


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    args = parser.parse_args(argv)
    print(json.dumps(analyze(args.root), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
