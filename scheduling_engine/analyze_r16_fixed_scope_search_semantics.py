"""Solver-free post-analysis for the frozen R16/S4 search-semantics grid."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean, median


ANALYSIS_SCHEMA = "r16_fixed_scope_search_semantics_analysis_v1"
COMPONENTS = (
    "course_category_diversity",
    "course_sequence_preferences",
    "difficulty_balance",
    "section_utilization_balance",
    "student_semester_load_balance",
)
CONTROL = "control_first_qualifying"
TREATMENTS = (
    CONTROL,
    "minimum_coordination",
    "iterative_strict_bound_refinement",
    "direct_exact_v2_optimization",
)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, indent=2, sort_keys=True, default=str)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def write_csv(path, rows, fieldnames=None):
    rows = list(rows)
    fieldnames = list(fieldnames or (rows[0].keys() if rows else ()))
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def percentile(values, fraction):
    values = sorted(float(value) for value in values if value is not None)
    if not values:
        return None
    position = (len(values) - 1) * fraction
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return values[lower]
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def distribution(values):
    values = [float(value) for value in values if value is not None]
    if not values:
        return {"count": 0, "min": None, "p25": None, "median": None,
                "mean": None, "p75": None, "max": None}
    return {
        "count": len(values),
        "min": min(values),
        "p25": percentile(values, 0.25),
        "median": median(values),
        "mean": mean(values),
        "p75": percentile(values, 0.75),
        "max": max(values),
    }


def flatten_cell(result, criteria):
    search = result.get("search", {})
    candidate = result.get("candidate", {})
    validation = result.get("validation", {})
    quality = result.get("quality", {})
    components = quality.get("component_improvements", {})
    resource = result.get("resource_summary", {})
    gain = float(quality.get("authoritative_gain", 0) or 0)
    native = float(search.get("cumulative_native_solve_wall_seconds", 0) or 0)
    first_gain = candidate.get("first_gain")
    final_gain = candidate.get("final_discovery_gain")
    changed_students = int(candidate.get("changed_student_count", 0) or 0)
    changed_decisions = int(candidate.get("changed_source_decision_count", 0) or 0)
    non_util = sum(float(components.get(name, 0) or 0) for name in COMPONENTS
                   if name != "section_utilization_balance")
    return {
        "ordinal": result["cell"]["ordinal"],
        "cell_id": result["cell"]["cell_id"],
        "source_cell_id": result["cell"]["source_cell_id"],
        "treatment": result["treatment"],
        "repeat": result["repeat"],
        "source_sha256": result["source"]["sha256"],
        "source_decision_fingerprint": result["source"]["source_decision_fingerprint"],
        "scope_fingerprint": result["scope_fingerprint"],
        "solver_status": search.get("status"),
        "search_termination": search.get("termination"),
        "candidate_found": bool(candidate.get("found")),
        "candidate_fingerprint": candidate.get("fingerprint"),
        "candidate_validated": bool(
            validation.get("full_model_validated", validation.get("authoritative"))
        ),
        "candidate_authoritative": bool(validation.get("authoritative")),
        "validation_classification": validation.get("classification"),
        "validation_wall_seconds": validation.get("wall_seconds"),
        "first_candidate_gain": first_gain,
        "final_candidate_gain": final_gain,
        "refinement_incremental_gain": (
            float(final_gain) - float(first_gain)
            if final_gain is not None and first_gain is not None else None
        ),
        "authoritative_gain": gain,
        "changed_student_count": changed_students,
        "changed_source_decision_count": changed_decisions,
        "broad_candidate": changed_students >= 3 and changed_decisions >= 10,
        "broad_low_failure": bool(
            validation.get("authoritative") and changed_students >= 3
            and changed_decisions >= 10
            and gain < float(criteria["minimum_coordination_extra"][
                "broad_low_gain_below_points"
            ])
        ),
        "solve_round_count": len(search.get("solve_rounds") or ()),
        "native_search_wall_seconds": native,
        "external_search_wall_seconds": search.get("cumulative_external_solve_wall_seconds"),
        "first_qualifying_latency_seconds": search.get("first_qualifying_latency_seconds"),
        "time_after_first_candidate_seconds": (
            max(0.0, native - float(search["first_qualifying_latency_seconds"]))
            if search.get("first_qualifying_latency_seconds") is not None else None
        ),
        "branches": search.get("branches"),
        "conflicts": search.get("conflicts"),
        "best_objective": candidate.get("best_objective"),
        "best_bound": candidate.get("best_bound"),
        "absolute_gap": candidate.get("absolute_gap"),
        "relative_gap": candidate.get("relative_gap"),
        "gain_per_search_minute": gain / (native / 60.0) if native > 0 else None,
        "non_utilization_component_gain": non_util,
        **{f"component_{name}": float(components.get(name, 0) or 0)
           for name in COMPONENTS},
        "peak_tree_rss_bytes": resource.get("peak_tree_rss_bytes"),
        "peak_tree_uss_bytes": resource.get("peak_tree_uss_bytes"),
        "minimum_available_memory_bytes": resource.get("minimum_available_memory_bytes"),
        "result_artifact_bytes": sum(
            int(value) for value in result.get("artifact_sizes", {}).values()
        ),
    }


def scope_treatment_summaries(rows):
    grouped = defaultdict(list)
    for row in rows:
        grouped[(row["source_cell_id"], row["treatment"])].append(row)
    summaries = []
    for (scope, treatment), group in sorted(grouped.items()):
        gains = [row["authoritative_gain"] for row in group]
        summary = {
            "source_cell_id": scope,
            "treatment": treatment,
            **{f"gain_{key}": value for key, value in distribution(gains).items()},
            "validated_repeat_count": sum(row["candidate_validated"] for row in group),
            "candidate_repeat_count": sum(row["candidate_found"] for row in group),
            "gain_ge_30_rate": sum(value >= 30 for value in gains) / len(group),
            "gain_ge_60_rate": sum(value >= 60 for value in gains) / len(group),
            "gain_ge_90_rate": sum(value >= 90 for value in gains) / len(group),
            "median_changed_students": median(row["changed_student_count"] for row in group),
            "median_changed_decisions": median(row["changed_source_decision_count"] for row in group),
            "broad_low_failure_count": sum(row["broad_low_failure"] for row in group),
            "median_non_utilization_gain": median(
                row["non_utilization_component_gain"] for row in group
            ),
            "unique_candidate_fingerprint_count": len({
                row["candidate_fingerprint"] for row in group
                if row["candidate_fingerprint"]
            }),
        }
        summaries.append(summary)
    return summaries


def paired_rows(rows):
    by_key = {(row["source_cell_id"], row["treatment"], row["repeat"]): row
              for row in rows}
    result = []
    scopes = sorted({row["source_cell_id"] for row in rows})
    for scope in scopes:
        control = [by_key[(scope, CONTROL, repeat)] for repeat in (1, 2, 3)]
        for treatment in TREATMENTS[1:]:
            treated = [by_key[(scope, treatment, repeat)] for repeat in (1, 2, 3)]
            differences = [
                right["authoritative_gain"] - left["authoritative_gain"]
                for left, right in zip(control, treated)
            ]
            result.append({
                "source_cell_id": scope,
                "treatment": treatment,
                "paired_gain_differences": differences,
                "paired_median_gain_difference": median(differences),
                "treatment_median_gain": median(
                    row["authoritative_gain"] for row in treated
                ),
                "control_median_gain": median(
                    row["authoritative_gain"] for row in control
                ),
                "validated_repeat_count": sum(row["candidate_validated"] for row in treated),
                "control_validated_repeat_count": sum(row["candidate_validated"] for row in control),
                "median_non_utilization_gain_difference": median(
                    right["non_utilization_component_gain"]
                    - left["non_utilization_component_gain"]
                    for left, right in zip(control, treated)
                ),
                "broad_low_failure_count_difference": (
                    sum(row["broad_low_failure"] for row in treated)
                    - sum(row["broad_low_failure"] for row in control)
                ),
            })
    return result


def classify_treatment(treatment, paired, criteria):
    group = [row for row in paired if row["treatment"] == treatment]
    threshold = float(criteria["practical_gain_points"])
    advance = criteria["advance"]
    wins = sum(row["paired_median_gain_difference"] >= threshold for row in group)
    losses = sum(row["paired_median_gain_difference"] <= -threshold for row in group)
    reliable_scopes = sum(
        row["validated_repeat_count"] >= advance["minimum_validated_repeats_per_scope"]
        for row in group
    )
    classification = "advance" if (
        wins >= advance["minimum_scope_wins"]
        and losses <= advance["maximum_scope_losses"]
        and reliable_scopes >= advance["minimum_reliable_scopes"]
    ) else "do_not_advance"
    extra = {}
    if treatment == "minimum_coordination":
        extra_rules = criteria["minimum_coordination_extra"]
        material_non_util_losses = sum(
            row["median_non_utilization_gain_difference"]
            <= -float(extra_rules["material_non_utilization_loss_points"])
            for row in group
        )
        broad_low_excess_scopes = sum(
            row["broad_low_failure_count_difference"]
            >= int(extra_rules["broad_low_excess_repeats_per_scope"])
            for row in group
        )
        extra = {
            "material_non_utilization_loss_scopes": material_non_util_losses,
            "broad_low_excess_scopes": broad_low_excess_scopes,
            "additional_gate_passed": (
                material_non_util_losses
                <= int(extra_rules["maximum_material_non_utilization_loss_scopes"])
                and broad_low_excess_scopes
                <= int(extra_rules["maximum_broad_low_excess_scopes"])
            ),
        }
        if not extra["additional_gate_passed"]:
            classification = "do_not_advance"
    return {
        "treatment": treatment,
        "classification": classification,
        "scope_wins_at_least_12": wins,
        "scope_losses_at_least_12": losses,
        "scopes_with_at_least_two_validated_repeats": reliable_scopes,
        **extra,
    }


def engineering_comparison(rows, paired, criteria):
    direct_rows = [row for row in rows if row["treatment"] == "direct_exact_v2_optimization"]
    iterative_rows = [row for row in rows if row["treatment"] == "iterative_strict_bound_refinement"]
    by_key = {(row["source_cell_id"], row["treatment"], row["repeat"]): row for row in rows}
    scope_differences = []
    for scope in sorted({row["source_cell_id"] for row in rows}):
        differences = [
            by_key[(scope, "direct_exact_v2_optimization", repeat)]["authoritative_gain"]
            - by_key[(scope, "iterative_strict_bound_refinement", repeat)]["authoritative_gain"]
            for repeat in (1, 2, 3)
        ]
        scope_differences.append({"source_cell_id": scope,
                                  "paired_median_direct_minus_iterative": median(differences)})
    threshold = float(criteria["practical_gain_points"])
    rules = criteria["direct_vs_iterative"]
    wins = sum(row["paired_median_direct_minus_iterative"] >= threshold for row in scope_differences)
    losses = sum(row["paired_median_direct_minus_iterative"] <= -threshold for row in scope_differences)
    direct_valid = sum(row["candidate_validated"] for row in direct_rows)
    iterative_valid = sum(row["candidate_validated"] for row in iterative_rows)
    if (
        wins >= rules["minimum_scope_wins"]
        and losses <= rules["maximum_scope_losses"]
        and direct_valid >= iterative_valid
    ):
        verdict = "direct_wins_engineering_comparison"
    elif (
        losses >= rules["minimum_scope_wins"]
        and wins <= rules["maximum_scope_losses"]
        and iterative_valid >= direct_valid
    ):
        verdict = "iterative_wins_engineering_comparison"
    else:
        verdict = "empirically_tied_or_inconclusive"
    return {
        "classification": verdict,
        "scope_rows": scope_differences,
        "direct_scope_wins_at_least_12": wins,
        "direct_scope_losses_at_least_12": losses,
        "direct_validated_cells": direct_valid,
        "iterative_validated_cells": iterative_valid,
    }


def treatment_summaries(rows):
    summaries = {}
    for treatment in TREATMENTS:
        group = [row for row in rows if row["treatment"] == treatment]
        gains = [row["authoritative_gain"] for row in group]
        summaries[treatment] = {
            "gain": distribution(gains),
            "runtime": distribution(row["native_search_wall_seconds"] for row in group),
            "gain_per_search_minute": distribution(row["gain_per_search_minute"] for row in group),
            "changed_students": dict(Counter(row["changed_student_count"] for row in group)),
            "changed_decisions": distribution(row["changed_source_decision_count"] for row in group),
            "validated_count": sum(row["candidate_validated"] for row in group),
            "validation_rate": sum(row["candidate_validated"] for row in group) / len(group),
            "gain_ge_30_rate": sum(value >= 30 for value in gains) / len(group),
            "gain_ge_60_rate": sum(value >= 60 for value in gains) / len(group),
            "gain_ge_90_rate": sum(value >= 90 for value in gains) / len(group),
            "candidate_fingerprint_diversity": len({row["candidate_fingerprint"] for row in group if row["candidate_fingerprint"]}),
            "first_gain": distribution(row["first_candidate_gain"] for row in group),
            "final_gain": distribution(row["final_candidate_gain"] for row in group),
            "refinement_incremental_gain": distribution(row["refinement_incremental_gain"] for row in group),
            "objective_gap": distribution(row["absolute_gap"] for row in group),
            "peak_tree_rss_bytes": distribution(row["peak_tree_rss_bytes"] for row in group),
            "peak_tree_uss_bytes": distribution(row["peak_tree_uss_bytes"] for row in group),
            "artifact_bytes": distribution(row["result_artifact_bytes"] for row in group),
            "solver_statuses": dict(Counter(row["solver_status"] for row in group)),
            "validation_classifications": dict(Counter(row["validation_classification"] for row in group)),
        }
    return summaries


def analyze(lineage, contract_path):
    lineage = Path(lineage).resolve()
    contract = read_json(contract_path)
    frozen = read_json(lineage / "frozen_contract.json")
    if frozen != contract:
        raise RuntimeError("lineage frozen contract differs from requested contract")
    missing = []
    results = []
    for cell in contract["cell_order"]:
        path = lineage / "cells" / cell["cell_id"] / "valid_result.json"
        if not path.exists():
            missing.append(cell["cell_id"])
            continue
        payload = read_json(path)
        if payload.get("cell") != cell:
            raise RuntimeError(f"cell identity mismatch: {cell['cell_id']}")
        results.append(payload)
    if missing:
        raise RuntimeError(f"analysis requires 72 valid cells; missing {len(missing)}")
    criteria = contract["decision_criteria"]
    rows = sorted(
        (flatten_cell(result, criteria) for result in results),
        key=lambda row: row["ordinal"],
    )
    scopes = scope_treatment_summaries(rows)
    paired = paired_rows(rows)
    summaries = treatment_summaries(rows)
    classifications = {
        treatment: classify_treatment(treatment, paired, criteria)
        for treatment in TREATMENTS[1:]
    }
    engineering = engineering_comparison(rows, paired, criteria)
    component_rows = [
        {"source_cell_id": row["source_cell_id"], "treatment": row["treatment"],
         "repeat": row["repeat"], **{name: row[f"component_{name}"] for name in COMPONENTS},
         "total": row["authoritative_gain"]}
        for row in rows
    ]
    analysis = {
        "schema": ANALYSIS_SCHEMA,
        "experiment_id": contract["experiment_id"],
        "complete_cell_count": len(rows),
        "expected_cell_count": contract["total_cell_count"],
        "contract_status": "complete",
        "treatment_summaries": summaries,
        "scope_treatment_summaries": scopes,
        "paired_scope_comparisons": paired,
        "treatment_classifications": classifications,
        "direct_vs_iterative": engineering,
        "decision_rules_applied_without_change": True,
        "quality_authority": "full_model_validated_candidates_only; unresolved cells have zero authoritative gain",
    }
    analysis_dir = lineage / "analysis"
    report_dir = lineage / "report"
    write_csv(analysis_dir / "treatment_scope_repeat.csv", rows)
    write_json(analysis_dir / "treatment_scope_repeat.json", rows)
    write_csv(analysis_dir / "scope_treatment_summary.csv", scopes)
    write_json(analysis_dir / "scope_treatment_summary.json", scopes)
    paired_csv = [{**row, "paired_gain_differences": canonical_json(row["paired_gain_differences"])} for row in paired]
    write_csv(analysis_dir / "paired_medians.csv", paired_csv)
    write_json(analysis_dir / "paired_medians.json", paired)
    write_csv(analysis_dir / "component_decomposition.csv", component_rows)
    write_json(analysis_dir / "component_decomposition.json", component_rows)
    write_json(analysis_dir / "treatment_summary.json", summaries)
    write_json(analysis_dir / "runtime_and_resource_summary.json", {
        treatment: {key: value for key, value in summary.items()
                    if key in {"runtime", "gain_per_search_minute", "peak_tree_rss_bytes",
                               "peak_tree_uss_bytes", "artifact_bytes"}}
        for treatment, summary in summaries.items()
    })
    write_json(analysis_dir / "classifications.json", {
        "treatments": classifications,
        "direct_vs_iterative": engineering,
    })
    write_json(analysis_dir / "analysis.json", analysis)
    report = [
        "# R16/S4 Fixed-Scope Search-Semantics Study",
        "",
        f"Status: complete ({len(rows)}/{contract['total_cell_count']} valid cells).",
        "",
        "All gains below are authoritative only after unchanged full-model validation. "
        "UNKNOWN is not treated as infeasibility.",
        "",
        "## Preregistered classifications",
        "",
    ]
    for treatment in TREATMENTS[1:]:
        item = classifications[treatment]
        report.append(
            f"- `{treatment}`: **{item['classification']}**; "
            f"wins {item['scope_wins_at_least_12']}, losses "
            f"{item['scope_losses_at_least_12']}, reliable scopes "
            f"{item['scopes_with_at_least_two_validated_repeats']}/6."
        )
    report.extend([
        "",
        "## Direct versus iterative",
        "",
        f"Classification: **{engineering['classification']}**.",
        "",
        "Detailed repeat, scope, component, runtime, validation, diversity, gap, and "
        "resource tables are in `analysis/`. This matched screen does not by itself "
        "authorize production promotion.",
        "",
    ])
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "study_report.md").write_text("\n".join(report), encoding="utf-8")
    write_json(report_dir / "study_report.json", analysis)
    return analysis


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while block := stream.read(8 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def seal(lineage, contract_path):
    lineage = Path(lineage).resolve()
    contract = read_json(contract_path)
    analysis = read_json(lineage / "analysis" / "analysis.json")
    if analysis.get("complete_cell_count") != contract.get("total_cell_count"):
        raise RuntimeError("only a complete 72-cell analyzed lineage can be sealed")
    if (lineage / "SEALED").exists() or (lineage / "artifact_hashes.sha256").exists():
        raise RuntimeError("lineage is already sealed or partially sealed")
    files = sorted(
        path for path in lineage.rglob("*")
        if path.is_file() and path.name not in {"artifact_hashes.sha256", "SEALED"}
    )
    lines = [f"{sha256_file(path)}  {path.relative_to(lineage).as_posix()}" for path in files]
    manifest = lineage / "artifact_hashes.sha256"
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    manifest_hash = sha256_file(manifest)
    write_json(lineage / "SEALED", {
        "schema": "r16_fixed_scope_search_semantics_seal_v1",
        "artifact_hashes_sha256": manifest_hash,
        "file_count": len(files),
    })
    return {"sealed": True, "manifest_sha256": manifest_hash, "file_count": len(files)}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--lineage", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--analyze", action="store_true")
    mode.add_argument("--seal-only", action="store_true")
    args = parser.parse_args(argv)
    result = (
        analyze(args.lineage, args.contract)
        if args.analyze else seal(args.lineage, args.contract)
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
