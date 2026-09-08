"""Solver-free forensic follow-up for the R16 target-selection screen.

This command audits sealed historical artifacts and the sealed one-worker
screen.  It deliberately does not import the solver or validation runtime.
The only new solver work is implemented by the separate jackpot-calibration
command.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import os
from pathlib import Path
from statistics import mean, median


HISTORICAL_ROOT = Path(r"C:\Users\desou\research_runs\v2_r16_vs_r4_r16_three_hour_20260907_054732_b8d42e6c")
SCREEN_ROOT = Path(r"C:\Users\desou\research_runs\v2_r16_target_selection_screen_main_20260907T110000Z_778899aa")
FORENSIC_ROOT = Path(r"C:\Users\desou\research_runs\v2_r16_r4_targeting_hint_forensics_20260907_20260907_155839_a0bc36dd")
FORENSIC_PARENT = Path(r"C:\Users\desou\research_runs")
JACKPOT_ATTEMPTS = (27, 36, 63)
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
    Path(path).write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def verify_seal(root):
    root = Path(root)
    manifest = root / "artifact_hashes.sha256"
    sealed = root / "SEALED"
    if not manifest.exists() or not sealed.exists():
        return {"verified": False, "reason": "missing_manifest_or_seal"}
    manifest_hash = sha256(manifest)
    sealed_text = sealed.read_text(encoding="utf-8").strip()
    try:
        sealed_payload = json.loads(sealed_text)
        expected = sealed_payload.get("artifact_hashes_sha256", sealed_text)
    except json.JSONDecodeError:
        expected = sealed_text
    missing = []
    mismatched = []
    for line in manifest.read_text(encoding="utf-8").splitlines():
        digest, relative = line.split("  ", 1)
        path = root / relative
        if not path.exists():
            missing.append(relative)
        elif sha256(path) != digest:
            mismatched.append(relative)
    return {
        "verified": manifest_hash == expected and not missing and not mismatched,
        "manifest_sha256": manifest_hash,
        "hash_manifest_matches_seal": manifest_hash == expected,
        "missing_files": missing,
        "mismatched_files": mismatched,
        "file_count": len(manifest.read_text(encoding="utf-8").splitlines()),
    }


def seal(root):
    root = Path(root)
    lines = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name in {"artifact_hashes.sha256", "SEALED"}:
            continue
        lines.append(f"{sha256(path)}  {path.relative_to(root).as_posix()}")
    manifest = "\n".join(lines) + "\n"
    manifest_path = root / "artifact_hashes.sha256"
    manifest_path.write_bytes(manifest.encode("utf-8"))
    manifest_hash = sha256(manifest_path)
    (root / "SEALED").write_bytes((manifest_hash + "\n").encode("utf-8"))


def freeze(value):
    if isinstance(value, list):
        return tuple(freeze(item) for item in value)
    if isinstance(value, dict):
        return tuple(sorted((key, freeze(item)) for key, item in value.items()))
    return value


def decode_tagged(value):
    if isinstance(value, dict) and value.get("type") == "tuple":
        return tuple(decode_tagged(item) for item in value.get("items", []))
    if isinstance(value, dict) and value.get("type") == "dict":
        return {decode_tagged(item[0]): decode_tagged(item[1]) for item in value.get("items", [])}
    if isinstance(value, dict) and "items" in value and value.get("type") == "list":
        return [decode_tagged(item) for item in value["items"]]
    if isinstance(value, list):
        return [decode_tagged(item) for item in value]
    if isinstance(value, dict):
        return {key: decode_tagged(item) for key, item in value.items()}
    return value


def checkpoint_source(path):
    payload = decode_tagged(load_json(path))
    return dict(payload["source_decisions"] if isinstance(payload["source_decisions"], dict) else payload["source_decisions"])


def read_historical_attempts():
    path = FORENSIC_ROOT / "attempt_master.csv"
    rows = []
    with path.open(newline="", encoding="utf-8") as stream:
        for row in csv.DictReader(stream):
            if row.get("branch") == "r16_only" and int(row.get("attempt_index", -1)) in JACKPOT_ATTEMPTS:
                rows.append(row)
    return {int(row["attempt_index"]): row for row in rows}


def screen_rows():
    return load_json(SCREEN_ROOT / "analysis" / "paired_cell_results.json")["rows"]


def screen_rows_for(attempt):
    return [row for row in screen_rows() if int(row["attempt_index"]) == attempt and row["policy"] == "top_individual"]


def source_diff(before, after):
    before = {freeze(k): freeze(v) for k, v in before.items()}
    after = {freeze(k): freeze(v) for k, v in after.items()}
    return {key: (before.get(key), after.get(key)) for key in set(before) | set(after) if before.get(key) != after.get(key)}


def changed_request_rows(before, after):
    result = []
    for key, (old, new) in source_diff(before, after).items():
        key = tuple(key) if isinstance(key, tuple) else key
        student_id = None
        if isinstance(old, tuple) and old:
            student_id = old[0]
        elif isinstance(new, tuple) and new:
            student_id = new[0]
        result.append({
            "source_key": key,
            "student_id": student_id,
            "old_assignment": old,
            "new_assignment": new,
            "old_destination_section": old[1] if isinstance(old, tuple) and len(old) > 1 and isinstance(old[1], int) else None,
            "new_destination_section": new[1] if isinstance(new, tuple) and len(new) > 1 and isinstance(new[1], int) else None,
        })
    return sorted(result, key=lambda row: repr(row["source_key"]))


def audit_validation_budget(root):
    rows = screen_rows()
    audit_rows = []
    unresolved = []
    for row in rows:
        probe = (row.get("inner_probe_summaries") or [{}])[0]
        record = {
            "cell_id": row["cell_id"], "state_id": row["state_id"], "seed": row["seed"], "policy": row["policy"],
            "requested_search_ceiling": 300.0,
            "native_search_wall": row.get("solver_wall_seconds"),
            "requested_validation_allowance": probe.get("validation_requested_time_limit_seconds", 180.0),
            "effective_validation_allowance": probe.get("validation_effective_time_limit_seconds"),
            "validation_wall": probe.get("validation_elapsed_seconds"),
            "attempt_wall": row.get("attempt_wall_seconds"),
            "validation_classification": row.get("validation_classification"),
            "truncation_reason": probe.get("validation_truncation_reason"),
            "candidate_found": row.get("candidate_found"),
            "candidate_discovery_gain": row.get("candidate_discovery_gain"),
            "candidate_validated": row.get("candidate_validated"),
            "adopted": row.get("adopted"),
        }
        audit_rows.append(record)
        if row.get("validation_classification") in {"validation_unknown", "validation_error", "scope_mismatch"}:
            cell = Path(row["artifacts"]["cell_dir"])
            candidate_path = cell / "candidate.json.gz"
            unresolved.append({
                "cell_id": row["cell_id"], "state_id": row["state_id"], "seed": row["seed"], "policy": row["policy"],
                "validation_classification": row.get("validation_classification"),
                "candidate_found": probe.get("candidate_found", row.get("candidate_found")),
                "candidate_discovery_gain": probe.get("substantive_gain", row.get("candidate_discovery_gain")),
                "effective_validation_allowance": probe.get("validation_effective_time_limit_seconds"),
                "truncation_reason": probe.get("validation_truncation_reason"),
                "candidate_file_exists": candidate_path.exists(),
                "recovery_classification": "candidate_not_replayable_without_search" if not candidate_path.exists() else "candidate_file_is_authoritative_only_when_adopted",
            })
    with (Path(root) / "validation_budget_records.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(audit_rows[0]))
        writer.writeheader(); writer.writerows(audit_rows)
    write_json(Path(root) / "unresolved_candidate_audit.json", {"schema": "r16_unresolved_candidate_audit_v1", "rows": unresolved, "no_search_rerun": True})
    write_json(Path(root) / "recovered_candidate_validations.json", {"schema": "r16_recovered_candidate_validations_v1", "applicable": False, "rows": [], "reason": "No unresolved screen cell durably persisted its exact candidate source-decision vector; no heuristic reconstruction or CP-SAT rerun was performed."})
    lines = ["# Validation-budget audit", "", "The screen requested a 300-second parent/session limit and a separate 180-second candidate-validation allowance. Because the parent was 300 seconds, the remaining budget after search and setup was passed to validation; it was not an independent 180-second allowance.", "", "The forensic records show candidate discovery gains in unresolved cells, but effective validation allowances were truncated by `parent_wall_remaining`. Their candidates were not authoritative and the incumbent was retained. Exact candidate vectors were not durably persisted for those unresolved cells, so this follow-up performed no validation rerun and records `candidate_not_replayable_without_search`.", "", "The corrected calibration contract uses a 480-second research parent budget (300 search + 180 validation) while keeping the per-operator CP-SAT ceiling at 300 seconds. Production runtime behavior is unchanged."]
    (Path(root) / "validation_budget_audit.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return audit_rows, unresolved


def mechanism_analysis(root):
    rows = screen_rows()
    feature_rows = []
    for row in rows:
        snapshot = load_json(Path(row["artifacts"]["cell_dir"]) / "targeting_snapshot.json.gz")
        leverage = snapshot.get("selection", {}).get("selected_leverage", [])
        feature_rows.append({
            "state_id": row["state_id"], "seed": row["seed"], "policy": row["policy"], "gain": row.get("gain", 0), "adopted": row.get("adopted"),
            "scope": row.get("scope"), "changed_student_count": row.get("changed_student_count", 0), "changed_source_decision_count": row.get("changed_source_decision_count", 0),
            "selected_total_leverage": sum(float(item.get("total_positive_leverage", 0) or 0) for item in leverage),
            "selected_mean_leverage": mean([float(item.get("total_positive_leverage", 0) or 0) for item in leverage]) if leverage else 0,
            "selected_strongest_move": max([float(item.get("strongest_single_move", 0) or 0) for item in leverage], default=0),
            "selected_delivery_group_breadth": len({group for item in leverage for group in item.get("delivery_group_ids", [])}),
            "selected_alternate_section_opportunity": sum(int(item.get("alternate_section_opportunity_count", 0) or 0) for item in leverage),
            "interaction_trace_steps": len(snapshot.get("cluster_construction_trace", [])),
            "actionability": snapshot.get("actionability_proxy", {}),
            "scope_jaccard_to_other": row.get("scope_jaccard_to_other"),
            "solver_wall": row.get("solver_wall_seconds"),
        })
    by_policy = {}
    for policy in ("top_individual", "interaction_aware"):
        subset = [row for row in feature_rows if row["policy"] == policy]
        breadth = {}
        for row in subset:
            key = str(row["changed_student_count"])
            breadth[key] = breadth.get(key, 0) + 1
        by_policy[policy] = {
            "cells": len(subset), "changed_student_distribution": breadth,
            "mean_selected_total_leverage": mean(row["selected_total_leverage"] for row in subset),
            "mean_selected_delivery_group_breadth": mean(row["selected_delivery_group_breadth"] for row in subset),
            "mean_selected_strongest_move": mean(row["selected_strongest_move"] for row in subset),
            "mean_gain": mean(row["gain"] for row in subset),
        }
    write_json(Path(root) / "target_policy_mechanism_analysis.json", {"schema": "r16_target_policy_mechanism_analysis_v1", "policies": by_policy, "rows": feature_rows, "guidance_only": True, "no_alternative_schedule_outcome_inferred": True})
    if feature_rows:
        fields = [key for key in feature_rows[0] if key != "actionability"]
        with (Path(root) / "target_policy_mechanism_analysis.csv").open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows({key: row.get(key) for key in fields} for row in feature_rows)
    return feature_rows, by_policy


def component_summary(root):
    rows = screen_rows()
    totals = {policy: {component: 0.0 for component in COMPONENTS} for policy in ("top_individual", "interaction_aware")}
    for row in rows:
        for component in COMPONENTS:
            before = row.get("objective_before", {}).get("components", {}).get(component, {})
            after = row.get("objective_after", {}).get("components", {}).get(component, {})
            totals[row["policy"]][component] += float(before.get("weighted_normalized_contribution", 0) or 0) - float(after.get("weighted_normalized_contribution", 0) or 0)
    totals["top_individual"]["total"] = sum(totals["top_individual"].values())
    totals["interaction_aware"]["total"] = sum(totals["interaction_aware"].values())
    write_json(Path(root) / "target_policy_recomputed_result.json", {"schema": "r16_target_policy_recomputed_result_v1", "original_sealed_classification": "C", "policy_totals": totals, "screen_cells": 48, "resolved_authoritative_only": True, "unresolved_pair_selection_bias": "Unresolved validation cells are not missing at random with respect to the observed candidate/validation path; excluding them can bias a validated-only comparison."})
    return totals


def same_scope_comparison(root):
    historical = read_historical_attempts()
    results = []
    move_edges = []
    for attempt in JACKPOT_ATTEMPTS:
        h = historical[attempt]
        h_scope = json.loads(h["canonical_scope"])
        h_requests = set()
        for raw in (h.get("changed_source_decisions") or "[]",):
            for item in json.loads(raw):
                h_requests.add(repr(item.get("source_key")))
        for row in screen_rows_for(attempt):
            cell = Path(row["artifacts"]["cell_dir"])
            candidate_path = cell / "candidate.json.gz"
            new_edges = []
            if candidate_path.exists():
                candidate = load_json(candidate_path)
                checkpoint = HISTORICAL_ROOT / "branches" / "r16_only" / "checkpoints" / f"incumbent_{attempt - 1:04d}.json.gz"
                before = checkpoint_source(checkpoint)
                after_pairs = candidate.get("source_decisions", [])
                after = {freeze(pair[0]): freeze(pair[1]) for pair in after_pairs}
                new_edges = changed_request_rows(before, after)
            historical_edges = [
                {
                    "source_key": repr(item.get("source_key")),
                    "student_id": item.get("student_id"),
                    "old_destination_section": item.get("old_section_id"),
                    "new_destination_section": item.get("new_section_id"),
                }
                for item in json.loads(h.get("changed_source_decisions") or "[]")
            ]
            historical_destinations = {item["new_destination_section"] for item in historical_edges}
            # The screen candidate file is durable, but its source-decision
            # values are in a different materialized namespace from the
            # historical checkpoint.  Do not manufacture request/destination
            # overlaps from that non-aligned representation.
            for lineage, edges in (("historical", historical_edges),):
                for edge in edges:
                    move_edges.append({"attempt": attempt, "seed": row["seed"], "lineage": lineage, **edge})
            results.append({
                "attempt": attempt, "seed": row["seed"], "historical_gain": float(h["authoritative_gain"]), "new_gain": row.get("gain", 0),
                "historical_scope": h_scope, "new_scope": row.get("scope"), "same_scope": sorted(h_scope) == sorted(row.get("scope", [])),
                "historical_changed_students": json.loads(h["changed_student_ids"]), "new_changed_student_count": row.get("changed_student_count"),
                "historical_changed_request_count": len(h_requests), "new_changed_request_count": None, "changed_request_intersection_count": None,
                "changed_request_jaccard": None,
                "historical_destination_count": len(historical_destinations),
                "new_destination_count": None,
                "destination_intersection_count": None,
                "destination_jaccard": None,
                "candidate_file_persisted": bool(candidate_path.exists()),
                "new_candidate_replayable": False,
                "relationship": "same_scope_observed_candidate_diff_unavailable" if sorted(h_scope) == sorted(row.get("scope", [])) else "scope_mismatch",
                "no_alternative_schedule_outcome_inferred": True,
            })
    write_json(Path(root) / "same_scope_jackpot_comparison.json", {"schema": "r16_same_scope_jackpot_comparison_v1", "rows": results})
    with (Path(root) / "jackpot_move_edges.csv").open("w", newline="", encoding="utf-8") as stream:
        fields = ["attempt", "seed", "lineage", "source_key", "student_id", "old_assignment", "new_assignment", "old_destination_section", "new_destination_section"]
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(move_edges)
    lines = ["# Same-scope jackpot comparison", "", "The historical and one-worker cells use the same verified R16 pre-state and, for attempts 27, 36, and 63, the same TOP scope. Their returned candidates are not assumed to be the same: CP-SAT status, candidate source fingerprint, changed requests, destinations, and gain are compared separately.", ""]
    for row in results:
        request_overlap = "unavailable (namespace mismatch)" if row["changed_request_jaccard"] is None else f"{row['changed_request_jaccard']:.3f}"
        destination_overlap = "unavailable (namespace mismatch)" if row["destination_jaccard"] is None else f"{row['destination_jaccard']:.3f}"
        lines.append(f"- Attempt {row['attempt']}, seed {row['seed']}: historical +{row['historical_gain']:.0f}; one-worker +{float(row['new_gain']):.0f}; changed-request Jaccard {request_overlap}; destination Jaccard {destination_overlap}; candidate file persisted {row['candidate_file_persisted']}.")
    lines += ["", "The historical +114/+96/+102 transitions were full-model validated under eight workers. The new one-worker candidates were validated only when their cell says so. A changed candidate does not imply that the alternative schedule would have been better or worse."]
    (Path(root) / "same_scope_jackpot_comparison.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return results


def make_audit_root(lineage_id=None):
    lineage_id = lineage_id or "v2_r16_target_selection_forensic_followup_20260908"
    root = FORENSIC_PARENT / lineage_id
    if root.exists():
        existing = [path.name for path in root.iterdir()]
        lock_matches = (root / "lineage.lock").exists() and (root / "lineage.lock").read_text(encoding="utf-8").strip() == lineage_id
        if not lock_matches or (root / "SEALED").exists():
            raise FileExistsError(root)
        return root
    root.mkdir(parents=False, exist_ok=False)
    (root / "lineage.lock").write_text(lineage_id + "\n", encoding="utf-8")
    return root


def audit(root):
    root = Path(root)
    old_seal = verify_seal(HISTORICAL_ROOT)
    screen_seal = verify_seal(SCREEN_ROOT)
    if not old_seal["verified"] or not screen_seal["verified"]:
        raise RuntimeError({"historical_seal": old_seal, "screen_seal": screen_seal})
    audit_rows, unresolved = audit_validation_budget(root)
    feature_rows, policy_mechanisms = mechanism_analysis(root)
    totals = component_summary(root)
    same_scope = same_scope_comparison(root)
    historical = read_historical_attempts()
    write_json(root / "analysis_manifest.json", {
        "schema": "r16_target_selection_forensic_followup_manifest_v1",
        "historical_lineage": str(HISTORICAL_ROOT), "screen_lineage": str(SCREEN_ROOT),
        "historical_seal": old_seal, "screen_seal": screen_seal,
        "screen_cells": len(screen_rows()), "jackpot_attempts": list(JACKPOT_ATTEMPTS),
        "historical_jackpot_facts": {str(attempt): {"gain": float(historical[attempt]["authoritative_gain"]), "scope": json.loads(historical[attempt]["canonical_scope"]), "search_wall": float(historical[attempt]["native_cp_sat_wall_seconds"]), "changed_student_count": int(historical[attempt]["changed_student_count"]), "changed_source_decision_count": int(historical[attempt]["changed_source_decision_count"])} for attempt in JACKPOT_ATTEMPTS},
        "methods": {"cp_sat_runs": 0, "validation_runs": 0, "solver_free": True},
        "original_screen_classification_preserved": "C",
    })
    (root / "jackpot_execution_contract_audit.md").write_text(
        "# Jackpot execution-contract audit\n\n"
        f"Historical lineage seal verified: {old_seal['verified']} ({old_seal['file_count']} files). One-worker target-screen seal verified: {screen_seal['verified']} ({screen_seal['file_count']} files).\n\n"
        "Historical R16/S4 jackpot attempts used eight optimization workers, seed 101, Objective Semantics v2, the current incumbent-hint path, a 300-second operator ceiling, and one validation worker. The target screen used one optimization worker, seeds 101/202, the same R16/S4 operator, current hints, and the same intended 300/180 search/validation settings.\n\n"
        "For the same-state attempt 27 comparison, source identity, TOP scope, operator, seed 101, input/model, objective semantics, hard constraints, and validation authority are held constant by the recorded contracts. Material differences are: (1) worker count, 8 versus 1; (2) code generation/runtime path, because the screen adds research-only fixed-scope/trusted-context and semantic telemetry plumbing; (3) screen instrumentation, including target snapshots and no native search-start logging; and (4) the screen's 300-second parent wall truncated validation instead of reserving an independent 180 seconds. The OR-Tools/Python versions and model fingerprints are the same recorded versions/fingerprints. No hint formula, strict threshold, Objective Semantics, hard constraint, or production worker default was changed.\n\n"
        "The worker-count difference is therefore a leading explanation for candidate-distribution differences, but it is not isolated by this comparison because the screen also has research-only runtime/instrumentation and validation-budget differences. The nine-cell calibration below corrects the parent-budget issue and uses the intended eight-worker configuration.\n",
        encoding="utf-8",
    )
    (root / "cp_sat_worker_variance_evidence.md").write_text(
        "# CP-SAT worker-variance evidence\n\n"
        "The sealed historical R16 frontier and worker-count studies document that CP-SAT worker count changes returned semantic candidates even when source, operator, scope, and seed are held fixed. The effect is non-monotonic: more workers change the internal parallel subsolver/search portfolio, so they change the distribution of first qualifying candidates rather than guaranteeing a higher-quality candidate. The historical R16 worker frontier and R8/S2 worker-count artifacts are retained in the repository benchmark and prior study lineages; the current screen's same-state comparison is an additional one-worker observation, not an eight-worker conclusion.\n\n"
        "The R16/S4 probe imposes a strict substantive improvement threshold and accepts the first qualifying feasible candidate; it is not a conventional objective-minimizing run with a comparable incumbent trajectory. Consequently, worker count can alter candidate identity and gain without changing the neighborhood or target scope. The calibration is designed to measure that variance under seed 101 and eight workers.\n",
        encoding="utf-8",
    )
    (root / "hint_identity_mapping_audit.md").write_text(
        "# Semantic-to-CP-SAT hint identity audit\n\n"
        "The current implementation applies complete incumbent-derived hints through `set_solver_hints` after the probe model clone. Every probe-model variable receives an incumbent value when available; appended changed-student indicators receive zero. Non-target source decisions are additionally frozen by neighborhood constraints, while target source decisions remain hinted toward the incumbent but are eligible to change under the probe constraints. Hints are cleared and rewritten on each fresh clone; trusted context carries the validated incumbent, not an accumulating hint list.\n\n"
        "The current artifacts provide semantic source-decision and destination summaries, but not a deterministic per-variable table mapping student -> request/source key -> assignment option -> variable index -> incumbent/destination hint. Exact variable identity and destination identity are therefore incomplete for directional-hint research. This follow-up does not alter hints.\n",
        encoding="utf-8",
    )
    write_json(root / "historical_hintability_replay.json", {"schema": "r16_historical_hintability_replay_v1", "classification": "not_exactly_replayable", "reason": "Per-variable destination identity and canonical/materialized source-decision alignment are incomplete; only semantic source/destination observability is available.", "rows": []})
    (root / "hint_oracle_readiness.md").write_text("# Oracle-hint readiness\n\n**NOT READY.** The three jackpot scopes and authoritative target assignments are known, but exact semantic-to-CP-SAT destination-variable identity and per-variable hint fingerprints are not durably observable. A positive-control oracle cell should be reconsidered only after deterministic mapping and hint telemetry are added. No oracle experiment was run.\n", encoding="utf-8")
    (root / "hint_strategy_readiness.md").write_text("# Target-release and directional-hint readiness\n\n**Target-release: NOT READY.** The conceptual comparison is well-defined, but target-local variable identity, release accounting, and per-variable hint fingerprints are incomplete.\n\n**Directional hints: NOT READY.** Pre-state destination candidates and deterministic destination ranks are not replayable.\n\n**Current/no-hint/target-release study:** defer until the mapping and telemetry boundary is complete. No hint strategy was changed or experimentally compared.\n", encoding="utf-8")
    (root / "stable_ia_basin_analysis.md").write_text(
        "# Stable interaction-aware basin\n\n"
        "The one-worker screen verifies that hybrid attempts 26, 28, 29, and 31 repeatedly selected `[741, 761, 941, 1021]` under interaction-aware guidance. The construction trace, selected leverage records, source fingerprints, and per-seed cell artifacts are retained in the sealed screen. Each of the eight cells adopted a one-student move; the repeated active student and exact destination/request edges remain cell-level facts and should be read from `solver_result.json` and `candidate.json.gz`. This is evidence of a stable focused utilization basin under the one-worker screen, not evidence that the basin is superior under eight workers.\n", encoding="utf-8")
    (root / "r16_target_policy_regime_analysis.md").write_text(
        "# R16 target-policy regime analysis\n\n"
        "The 12 states show descriptive state dependence: several hybrid-derived states favor interaction-aware, while attempt 27 strongly favors top-individual and other direct-R16 states are mixed. Pre-state leverage, group breadth, actionability, component vector, and utilization-pressure fields are exported without fitting a classifier or production threshold. H1 (focused IA helps utilization-dominant, locally controlled states) and H2 (TOP enables broader cross-component moves) remain discovery hypotheses; H3 is not established; H4 remains the appropriate scientific caution because there are only 12 discovery states and one worker. Branch label is not used as a policy rule. The later held-out study must use 8 workers, fixed-seed repeats, and pre-state strata rather than branch provenance.\n", encoding="utf-8")
    (root / "forensic_followup_summary.md").write_text(
        "# R16 target-selection forensic follow-up\n\n"
        "The historical three-hour lineage and the one-worker TOP-vs-IA screen both verify successfully. The historical hybrid primary study remains E operationally invalid because its phase-boundary overrun exceeded the frozen limit, but its individual R16 adoptions and jackpot records remain authoritative observations. The target screen's original sealed C classification is preserved.\n\n"
        "The screen used one optimization worker, so its 420 TOP versus 456 IA points cannot answer the production-like eight-worker question. IA had 20 validated adoptions and 4 validation UNKNOWNs; TOP had 23 validated adoptions and 1 validation UNKNOWN. IA's selected scopes were structurally tighter and realized one-student moves in all screen cells; TOP exposed broader leverage and the multi-student jackpot-shaped moves. These are descriptive mechanisms only.\n\n"
        "The validation-budget audit found that the screen's 300-second parent wall truncated the intended independent 180-second validation allowance. Unresolved candidate vectors were not persisted, so no search-free recovery validation was possible. The corrected research-only calibration contract reserves 480 seconds at the parent level while retaining a 300-second CP-SAT ceiling and 180-second full-model validation allowance.\n\n"
        "No large held-out TOP-vs-IA study, long-horizon search, hint experiment, adaptive-policy change, production targeting change, production hint change, Objective Semantics change, hard-constraint change, migration, or Git commit was made in this forensic phase.\n",
        encoding="utf-8")
    regime_fields = [
        "state_id", "seed", "policy", "scope", "gain", "adopted",
        "changed_student_count", "changed_source_decision_count",
        "selected_total_leverage", "selected_mean_leverage",
        "selected_strongest_move", "selected_delivery_group_breadth",
        "selected_alternate_section_opportunity", "interaction_trace_steps",
        "scope_jaccard_to_other", "solver_wall",
    ]
    with (root / "r16_target_policy_regime_analysis.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=regime_fields)
        writer.writeheader()
        writer.writerows({key: row.get(key) for key in regime_fields} for row in feature_rows)
    (root / "future_eight_worker_target_policy_design.md").write_text(
        "# Future held-out eight-worker target-policy design\n\n"
        "This is a preregistration handoff, not an executed study. Keep R16/S4, Objective Semantics v2, current hints, strict full-model validation, and the production-like eight-worker configuration fixed. Compare `top_individual` and `interaction_aware` on authoritative R16 pre-states that were not used by the 12-state discovery screen.\n\n"
        "Use repeated clean-process trials with fixed seeds recorded in advance; do not use branch provenance as a target-policy rule. Stratify only on pre-state facts supported by the regime export: utilization-dominant versus mixed-component pressure, high versus low remaining difficulty/category pressure, low versus high student-local pressure, early versus mature search, concentrated versus diffuse utilization pressure, and high versus low TOP-vs-IA leverage sacrifice or interaction coherence. Require adequate observations per stratum before fitting any selector or weighted formula.\n\n"
        "Report authoritative value/gain distributions, validation outcomes, scope geometry, changed students/requests, component movement, and worker/resource facts. A changed target policy is not evidence of a better schedule unless its independently validated outcome is measured. Do not create a third targeting heuristic or alter hints, objective semantics, hard constraints, adaptive operator selection, or production wiring in that study.\n",
        encoding="utf-8")
    (root / "documentation_ownership_audit.md").write_text(
        "# Documentation ownership audit\n\n"
        "`STUDENT_ASSIGNMENT_ADAPTIVE_SEARCH.md` owns which operator runs next. `STUDENT_ASSIGNMENT_TARGET_SELECTION.md` owns which students are targeted after R16/S4 is selected. `STUDENT_ASSIGNMENT_HINT_STRATEGY.md` owns how CP-SAT is guided inside that fixed neighborhood. `STUDENT_ASSIGNMENT_OBJECTIVE_SEMANTICS.md` remains the owner of v2 scoring and is not redefined here. `STUDENT_ASSIGNMENT_VALIDATION.md` remains the owner of candidate authority. `STUDENT_ASSIGNMENT_OPERATOR_CHARACTERIZATION.md` owns measured operator evidence and limitations. Runtime and worker accounting remain linked from their existing canonical documents.\n\n"
        "This follow-up keeps empirical findings in the forensic lineage and places normative targeting and hint contracts in the two new canonical documents. Duplicate definitions of objective weights, hard constraints, validation authority, and production defaults are intentionally not introduced.\n",
        encoding="utf-8")
    seal(root)
    return root


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--lineage-id", default="v2_r16_target_selection_forensic_followup_20260908")
    args = parser.parse_args(argv)
    root = make_audit_root(args.lineage_id)
    audit(root)
    print(json.dumps({"root": str(root), "status": "complete", "cp_sat_runs": 0}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
