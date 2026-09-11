"""Read-only post-hoc analysis for the sealed dynamic IA-versus-TOP study.

The source lineage is never opened for writing.  Large JSON and JSONL files are
parsed locally and reduced to compact CSV/JSON/Markdown artifacts in a sibling
post-hoc directory.  No solver is imported or invoked by this module.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import math
import os
import secrets
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


POLICIES = ("interaction_aware", "top_individual")
SHORT = {"interaction_aware": "IA", "top_individual": "TOP"}
COMPONENTS = (
    "section_utilization_balance",
    "difficulty_balance",
    "course_category_diversity",
    "student_semester_load_balance",
    "course_sequence_preferences",
)
CAPS = (60, 65, 70, 75, 80, 90, 120, 180, 300)
RELATIVE_CAPS = (0, 0.25, 0.5, 1, 2, 3, 5, 10, 20)
QUARTERS = ((0, 22.5), (22.5, 45), (45, 67.5), (67.5, 90))


def read_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_source_seal(root: Path) -> dict[str, Any]:
    """Verify the sealed lineage tree before deriving any research claim."""
    seal_path, manifest_path = root / "SEALED", root / "artifact_hashes.sha256"
    if not seal_path.exists() or not manifest_path.exists():
        raise RuntimeError("source lineage lacks SEALED or artifact hash manifest")
    seal = read_json(seal_path)
    manifest_digest = sha256_file(manifest_path)
    if manifest_digest != seal.get("artifact_hashes_sha256"):
        raise RuntimeError("source seal does not match artifact hash manifest")
    failures, count = [], 0
    for line in manifest_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        expected, relative = line.split("  ", 1)
        path = root / relative
        count += 1
        if not path.exists() or sha256_file(path) != expected:
            failures.append(relative)
    if failures:
        raise RuntimeError(f"source artifact hash verification failed: {failures[:3]}")
    return {"verified": True, "manifest_entries": count, "manifest_sha256": manifest_digest, "seal_schema": seal.get("schema")}


def scalar(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = sorted({key for row in rows for key in row}) if rows else []
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: scalar(row.get(key)) for key in fields})


def mean(values: Iterable[float]) -> float | None:
    values = [float(x) for x in values if x is not None and math.isfinite(float(x))]
    return statistics.fmean(values) if values else None


def median(values: Iterable[float]) -> float | None:
    values = [float(x) for x in values if x is not None and math.isfinite(float(x))]
    return statistics.median(values) if values else None


def quantile(values: Iterable[float], q: float) -> float | None:
    values = sorted(float(x) for x in values if x is not None and math.isfinite(float(x)))
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    place = (len(values) - 1) * q
    lo, hi = math.floor(place), math.ceil(place)
    return values[lo] + (values[hi] - values[lo]) * (place - lo)


def summary(values: Iterable[float]) -> dict[str, Any]:
    clean = [float(x) for x in values if x is not None and math.isfinite(float(x))]
    return {
        "n": len(clean), "mean": mean(clean), "median": median(clean),
        "min": min(clean) if clean else None, "p25": quantile(clean, .25),
        "p75": quantile(clean, .75), "p90": quantile(clean, .90),
        "max": max(clean) if clean else None,
    }


def jaccard(left: Iterable[int], right: Iterable[int]) -> float:
    left, right = set(left or ()), set(right or ())
    return len(left & right) / len(left | right) if left | right else 1.0


def ranks(values: list[float]) -> list[float]:
    ordered = sorted(enumerate(values), key=lambda pair: pair[1])
    result = [0.0] * len(values)
    i = 0
    while i < len(ordered):
        j = i + 1
        while j < len(ordered) and ordered[j][1] == ordered[i][1]:
            j += 1
        rank = (i + 1 + j) / 2.0
        for index, _ in ordered[i:j]:
            result[index] = rank
        i = j
    return result


def pearson(left: list[float], right: list[float]) -> float | None:
    if len(left) < 3:
        return None
    lx, ly = mean(left), mean(right)
    sx = math.sqrt(sum((x - lx) ** 2 for x in left))
    sy = math.sqrt(sum((y - ly) ** 2 for y in right))
    return None if not sx or not sy else sum((x - lx) * (y - ly) for x, y in zip(left, right)) / (sx * sy)


def spearman(rows: list[dict[str, Any]], x: str, y: str) -> dict[str, Any]:
    pairs = [(float(row[x]), float(row[y])) for row in rows if row.get(x) is not None and row.get(y) is not None]
    return {"n": len(pairs), "rho": pearson(ranks([a for a, _ in pairs]), ranks([b for _, b in pairs])) if pairs else None}


def component_key(name: str) -> str:
    return name.replace("_penalty", "").replace("student_semester_balance", "student_semester_load_balance")


def component_snapshot(quality: dict[str, Any]) -> dict[str, float]:
    result = {}
    for name, facts in (quality.get("components") or {}).items():
        if isinstance(facts, dict):
            result[component_key(name)] = float(facts.get("weighted_normalized_contribution", 0) or 0)
    return result


def selector_scope(selection: dict[str, Any] | None) -> list[int]:
    return list((selection or {}).get("selected_student_ids") or ())


def selection_leverage(selection: dict[str, Any] | None) -> float | None:
    rows = ((selection or {}).get("guidance_facts") or {}).get("selected_leverage") or []
    numbers = []
    for row in rows:
        if isinstance(row, dict):
            numbers.append(float(row.get("total_positive_leverage", row.get("positive_leverage", 0)) or 0))
    return sum(numbers) if numbers else None


def first_time_for_objective(timeline: list[dict[str, Any]], target: float) -> float | None:
    for point in timeline:
        objective = point.get("objective")
        if objective is not None and float(objective) <= target + 1e-9:
            return float(point.get("elapsed_seconds"))
    return None


def parse_attempt(path: Path, branch: str) -> dict[str, Any]:
    payload = read_json(path)
    attempt = payload["attempt"]
    inner = (attempt.get("inner_probe_summaries") or [{}])[0]
    telemetry = inner.get("search_start_telemetry") or {}
    timeline = [point for point in telemetry.get("improvement_timeline") or [] if point.get("objective") is not None]
    timeline.sort(key=lambda point: (float(point.get("elapsed_seconds", 0)), int(point.get("event_index", 0))))
    snapshot = payload.get("targeting_snapshot") or {}
    active = snapshot.get("active_selection") or {}
    shadow = snapshot.get("shadow_selection") or {}
    selector = snapshot.get("selector_state") or payload.get("selector_state") or {}
    active_scope = list(attempt.get("actual_target_scope") or attempt.get("target_scope") or snapshot.get("selected_student_ids") or ())
    shadow_scope = selector_scope(shadow)
    ranked = list(selector.get("utilization_ranked_student_ids") or ())
    rank_map = {student: index + 1 for index, student in enumerate(ranked)}
    starting = inner.get("starting_incumbent_value")
    final_value = inner.get("candidate_substantive_value")
    if final_value is None and timeline:
        final_value = min(float(point["objective"]) for point in timeline)
    final_gain = (float(starting) - float(final_value)) if starting is not None and final_value is not None else None
    last_best = first_time_for_objective(timeline, float(final_value)) if final_value is not None else None
    components = {name: float((attempt.get("objective_improvement_weighted_delta") or {}).get(name, 0) or 0) for name in COMPONENTS}
    elapsed = float(payload.get("branch_elapsed_seconds") or 0)
    return {
        "branch": branch, "policy": SHORT[branch], "attempt_index": int(payload.get("attempt_index") or 0),
        "branch_elapsed_seconds": elapsed, "branch_elapsed_minutes": elapsed / 60,
        "attempt_elapsed_seconds": float(attempt.get("elapsed_seconds") or 0),
        "adopted": bool(attempt.get("adopted")), "gain": float(attempt.get("gain") or 0),
        "candidate_discovery_gain": float(attempt.get("candidate_discovery_gain") or 0),
        "candidate_found": bool(attempt.get("candidate_found")), "candidate_validated": bool(attempt.get("candidate_validated")),
        "status": attempt.get("status"), "solver_wall_seconds": float(attempt.get("solver_wall_time_seconds") or inner.get("solver_wall_time_seconds") or 0),
        "validation_seconds": float(attempt.get("validation_seconds") or inner.get("validation_elapsed_seconds") or 0),
        "native_search_seconds": float(attempt.get("session_cp_sat_seconds") or inner.get("cumulative_native_solve_wall_seconds") or 0),
        "search_start_seconds": telemetry.get("search_start_seconds"), "first_feasible_seconds": telemetry.get("first_solution_seconds"),
        "first_bound_seconds": telemetry.get("first_bound_seconds"), "timeline_points": len(timeline),
        "starting_value": starting, "final_candidate_value": final_value, "final_discovery_gain": final_gain,
        "final_best_seconds": last_best, "proof_lag_seconds": (float(attempt.get("solver_wall_time_seconds") or inner.get("solver_wall_time_seconds") or 0) - last_best) if last_best is not None else None,
        "best_bound": inner.get("best_bound"), "absolute_gap": inner.get("objective_absolute_gap"), "relative_gap": inner.get("objective_relative_gap"),
        "branches": inner.get("branches"), "conflicts": inner.get("conflicts"), "search_termination": inner.get("search_termination_classification"),
        "changed_students": int(attempt.get("changed_student_count") or 0), "changed_decisions": int(attempt.get("changed_source_decision_count") or 0),
        "student_boundary_saturated": int(attempt.get("changed_student_count") or 0) >= 4,
        "decision_boundary_saturated": int(attempt.get("changed_source_decision_count") or 0) >= 16,
        "active_scope": active_scope, "shadow_scope": shadow_scope, "active_shadow_overlap": len(set(active_scope) & set(shadow_scope)),
        "active_shadow_jaccard": jaccard(active_scope, shadow_scope), "scope_fingerprint": snapshot.get("scope_fingerprint"),
        "shadow_scope_fingerprint": shadow.get("scope_fingerprint"), "utilization_ranks": [rank_map.get(student) for student in active_scope],
        "shadow_utilization_ranks": [rank_map.get(student) for student in shadow_scope],
        "mean_utilization_rank": mean([rank_map.get(student) for student in active_scope]),
        "max_utilization_rank": max([rank_map.get(student) for student in active_scope if rank_map.get(student) is not None], default=None),
        "active_selection_leverage": selection_leverage(active), "shadow_selection_leverage": selection_leverage(shadow),
        "utilization_headroom": selector.get("optimistic_utilization_leverage"),
        "utilization_weighted_value": selector.get("utilization_weighted_value"),
        "global_utilization_weighted_share": selector.get("global_utilization_weighted_share"),
        "student_local_weighted_share": selector.get("student_local_weighted_share"),
        "source_before": attempt.get("source_fingerprint_before"), "source_after": attempt.get("source_fingerprint_after"),
        "candidate_fingerprint": attempt.get("candidate_source_decision_fingerprint"), "timeline": timeline,
        **{f"component_{name}": value for name, value in components.items()},
    }


def phase_event_summary(path: Path) -> dict[str, Any]:
    counts = Counter()
    events = 0
    max_elapsed = None
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            events += 1
            event = json.loads(line)
            counts[event.get("event_type") or event.get("event") or "unknown"] += 1
            value = event.get("branch_elapsed_seconds")
            if value is not None:
                max_elapsed = max(float(value), max_elapsed or float(value))
    return {"line_count": events, "event_counts": dict(counts), "max_branch_elapsed_seconds": max_elapsed}


def resource_summary(path: Path) -> dict[str, Any]:
    samples = []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                samples.append(json.loads(line))
    elapsed = [float(row.get("elapsed_seconds", 0)) for row in samples]
    gaps = [b - a for a, b in zip(elapsed, elapsed[1:])]
    fields = ("tree_rss_bytes", "tree_uss_bytes", "system_available_memory_bytes", "thread_count", "tree_vms_bytes")
    result = {"sample_count": len(samples), "sample_gap_seconds": summary(gaps), "sleep_gap_count_over_10s": sum(g > 10 for g in gaps)}
    for field in fields:
        result[field] = summary([float(row.get(field, 0) or 0) for row in samples])
    return result


def trajectory_at(rows: list[dict[str, Any]], source_value: float, seconds: float) -> dict[str, Any]:
    prior = [row for row in rows if row["branch_elapsed_seconds"] <= seconds + 1e-9]
    output = {"score": source_value, "gain": 0.0, "adoptions": 0}
    for name in COMPONENTS:
        output[name] = 0.0
    for row in prior:
        if row["adopted"]:
            output["gain"] += row["gain"]
            output["adoptions"] += 1
            for name in COMPONENTS:
                output[name] += row[f"component_{name}"]
    output["score"] = source_value - output["gain"]
    return output


def quarter_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for start, end in QUARTERS:
        members = [row for row in rows if start * 60 < row["branch_elapsed_seconds"] <= end * 60]
        adopted = [row for row in members if row["adopted"]]
        native = sum(row["native_search_seconds"] for row in members)
        output = {
            "quarter": f"{start:g}-{end:g}m", "start_minute": start, "end_minute": end,
            "attempts": len(members), "adoptions": len(adopted), "total_gain": sum(row["gain"] for row in adopted),
            "gain_per_adoption": (sum(row["gain"] for row in adopted) / len(adopted)) if adopted else None,
            "native_search_minutes": native / 60, "gain_per_native_search_minute": (sum(row["gain"] for row in adopted) / (native / 60)) if native else None,
            "optimality_rate": sum(row["status"] == "optimal" for row in members) / len(members) if members else None,
            "mean_solver_wall_seconds": mean([row["solver_wall_seconds"] for row in members]),
            "mean_scope_rank": mean([row["mean_utilization_rank"] for row in members]),
            "mean_scope_jaccard_to_shadow": mean([row["active_shadow_jaccard"] for row in members]),
        }
        for name in COMPONENTS:
            output[f"component_{name}"] = sum(row[f"component_{name}"] for row in adopted)
        result.append(output)
    return result


def time_cap_rows(rows: list[dict[str, Any]], caps: tuple[float, ...], relative: bool = False) -> list[dict[str, Any]]:
    output = []
    substantive = [row for row in rows if row["final_discovery_gain"] is not None and row["final_discovery_gain"] > 0]
    for cap in caps:
        candidate, captured, best, proven = [], [], [], []
        for row in substantive:
            timeline = row["timeline"]
            first = row["first_feasible_seconds"]
            ceiling = (first + cap) if relative and first is not None else cap
            by_cap = [point for point in timeline if float(point.get("elapsed_seconds", 0)) <= ceiling + 1e-9]
            has = bool(by_cap)
            candidate.append(has)
            if has:
                best_objective = min(float(point["objective"]) for point in by_cap)
                captured.append(max(0.0, min(1.0, (float(row["starting_value"]) - best_objective) / row["final_discovery_gain"])))
                best.append(row["final_best_seconds"] is not None and row["final_best_seconds"] <= ceiling + 1e-9)
            else:
                captured.append(0.0)
                best.append(False)
            proven.append(row["status"] == "optimal" and row["solver_wall_seconds"] <= ceiling + 1e-9)
        output.append({
            "cap_seconds": cap, "relative_to_first_feasible": relative, "substantive_solves": len(substantive),
            "fraction_any_candidate": mean(candidate), "mean_final_gain_fraction": mean(captured), "median_final_gain_fraction": median(captured),
            "fraction_final_best_reached": mean(best), "fraction_proven_optimal": mean(proven),
        })
    return output


def scope_diversity(rows: list[dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    scopes = [tuple(sorted(row["active_scope"])) for row in rows]
    slots = [student for row in rows for student in row["active_scope"]]
    counts = Counter(slots)
    hhi = sum((count / len(slots)) ** 2 for count in counts.values()) if slots else None
    consecutive = [jaccard(a, b) for a, b in zip(scopes, scopes[1:])]
    nonconsecutive = [jaccard(scopes[i], scopes[j]) for i in range(len(scopes)) for j in range(i) if i - j > 1]
    stable_core = sorted(set.intersection(*(set(scope) for scope in scopes))) if scopes else []
    streaks, run = [], 1
    for previous, current in zip(scopes, scopes[1:]):
        if previous == current:
            run += 1
        else:
            streaks.append(run); run = 1
    if scopes:
        streaks.append(run)
    frequencies = [{"student_id": student, "selection_count": count, "selection_share": count / len(slots)} for student, count in sorted(counts.items())]
    return {
        "total_selection_slots": len(slots), "unique_students": len(counts), "repeated_students": sum(count > 1 for count in counts.values()),
        "hhi": hhi, "effective_target_population": (1 / hhi) if hhi else None, "unique_exact_scopes": len(set(scopes)),
        "consecutive_scope_jaccard": summary(consecutive), "nonconsecutive_scope_jaccard": summary(nonconsecutive),
        "stable_core": stable_core, "max_exact_scope_repeat_streak": max(streaks) if streaks else 0,
    }, frequencies


def endpoint_summary(root: Path, source_identity: dict[str, Any]) -> dict[str, Any]:
    result = {"source_identity": source_identity, "branches": {}}
    for branch in POLICIES:
        bootstrap = read_json(root / "branches" / branch / "bootstrap.json")
        final = read_json(root / "branches" / branch / "final_validation.json")
        validation = final.get("validation") or {}
        durable = (final.get("branch") or {}).get("validation") or {}
        result["branches"][branch] = {
            "bootstrap_source_quality": bootstrap.get("source_quality"), "bootstrap_source_validation": bootstrap.get("source_validation"),
            "endpoint_validation": validation, "durable_branch_validation": durable,
            # Branch checkpoints carry the materialized source-decision
            # fingerprint; the lineage separately records the canonical
            # semantic fingerprint.  They are intentionally different IDs.
            "same_source_fingerprint": (bootstrap.get("source_quality") or {}).get("source_fingerprint") == source_identity.get("materialized_source_fingerprint"),
        }
    return result


def inventory(root: Path) -> dict[str, Any]:
    rows = []
    for path in root.rglob("*"):
        if path.is_file():
            rows.append({"path": path.relative_to(root).as_posix(), "bytes": path.stat().st_size})
    return {"file_count": len(rows), "total_bytes": sum(row["bytes"] for row in rows), "files": sorted(rows, key=lambda row: row["path"])}


def checkpoint_rows(root: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows, audit = [], {}
    for branch in POLICIES:
        expected_parents = set()
        branch_rows = []
        for path in sorted((root / "branches" / branch / "checkpoints").glob("*.json.gz")):
            with gzip.open(path, "rt", encoding="utf-8") as stream:
                checkpoint = json.load(stream)
            quality, validation = checkpoint.get("quality") or {}, checkpoint.get("validation") or {}
            row = {
                "branch": branch, "checkpoint": path.name, "created_at_utc": checkpoint.get("created_at_utc"),
                "source_fingerprint": checkpoint.get("source_decision_fingerprint"), "parent_fingerprint": checkpoint.get("parent_source_decision_fingerprint"),
                "input_fingerprint": checkpoint.get("input_semantic_fingerprint"), "objective_semantics_version": checkpoint.get("objective_semantics_version"),
                "v2_score": quality.get("weighted_substantive_value"), "assignment_count": quality.get("assignment_count"),
                "commitment_count": quality.get("special_commitment_count"), "unmet_request_count": quality.get("unmet_request_count"),
                "full_model_validation": validation.get("full_model_validation"), "complete": validation.get("complete"),
                "required_decision_group_count": validation.get("required_source_decision_group_count"),
            }
            branch_rows.append(row)
            expected_parents.add(row["source_fingerprint"])
        rows.extend(branch_rows)
        audit[branch] = {
            "checkpoint_count": len(branch_rows), "all_complete": all(row["complete"] for row in branch_rows),
            "all_full_model_validated": all(row["full_model_validation"] for row in branch_rows),
            "all_expected_input": all(row["input_fingerprint"] == read_json(root / "source" / "source_identity.json")["input_fingerprint"] for row in branch_rows),
        }
    return rows, audit


def make_report(output: Path, source_value: float, all_rows: dict[str, list[dict[str, Any]]], trajectory: list[dict[str, Any]], quarters: dict[str, list[dict[str, Any]]], scopes: dict[str, dict[str, Any]], associations: dict[str, Any], headroom: list[dict[str, Any]], resources: dict[str, Any], hard: list[dict[str, Any]]) -> None:
    ia, top = all_rows["interaction_aware"], all_rows["top_individual"]
    totals = {branch: sum(row["gain"] for row in rows if row["adopted"]) for branch, rows in all_rows.items()}
    changes = lead_changes(all_rows, source_value)
    change_text = "; ".join(f"{row['leader_after']} at {row['trusted_elapsed_minutes']:.2f}m (IA−TOP {row['ia_minus_top_gain']:+.0f})" for row in changes[1:])
    max_ia = max((row["ia_minus_top_gain"] for row in trajectory), default=0)
    max_top = max((-row["ia_minus_top_gain"] for row in trajectory), default=0)
    late_ia, late_top = quarters["interaction_aware"][-1], quarters["top_individual"][-1]
    remaining = sorted(headroom, key=lambda row: row["top_remaining_weighted_penalty"], reverse=True)
    cp = {branch: cp_sat_aggregate(rows) for branch, rows in all_rows.items()}
    lines = [
        "# Dynamic IA versus TOP direct exact-v2: post-hoc findings", "",
        "## Design and authority", "",
        "This is a read-only analysis of one sealed dynamic trajectory per policy. It is descriptive: sequential attempts are not independent randomized observations, shadow scopes were not solved, and no routing rule is production-ready.", "",
        f"Both branches began from the same validated source (v2 score {source_value:.0f}) and both endpoints passed complete full-model validation. IA recorded {len(ia)} attempts / {sum(r['adopted'] for r in ia)} adoptions / {totals['interaction_aware']:.0f} points; TOP recorded {len(top)} / {sum(r['adopted'] for r in top)} / {totals['top_individual']:.0f} points.", "",
        "## Established observations", "",
        f"TOP finished {totals['top_individual'] - totals['interaction_aware']:.0f} v2 points ahead. The 5-minute reconstruction shows a maximum IA lead of {max_ia:.0f} and maximum TOP lead of {max_top:.0f}. Exact leader transitions at the union of authoritative timestamps were: {change_text}. This is a trajectory result, not evidence of universal TOP dominance.", "",
        f"IA's last quarter generated {late_ia['total_gain']:.0f} points in {late_ia['adoptions']} adoptions ({late_ia['gain_per_native_search_minute']:.2f} points/native-search-minute); TOP generated {late_top['total_gain']:.0f} in {late_top['adoptions']} ({late_top['gain_per_native_search_minute']:.2f}).",
        f"Scope diversity differed: IA used {scopes['interaction_aware']['unique_students']} unique students across {scopes['interaction_aware']['total_selection_slots']} slots (effective population {scopes['interaction_aware']['effective_target_population']:.1f}); TOP used {scopes['top_individual']['unique_students']} (effective population {scopes['top_individual']['effective_target_population']:.1f}).",
        "",
        "## Selector mechanism hypothesis", "",
        f"Within IA-created states, mean IA utilization rank and realized gain have Spearman rho {associations['interaction_aware']['rank_vs_gain']['rho']!r}; IA active ranks average {associations['path_dependence']['ia_active_on_ia_states_mean_rank']:.1f}, versus {associations['path_dependence']['ia_shadow_on_top_states_mean_rank']:.1f} when IA is shadow-computed on TOP-created states. These associations are routing hypotheses, not causal estimates. The active/shadow table shows whether agreement and fresh scopes coincide with stronger observed active outcomes, but never assigns a counterfactual gain to a shadow scope.", "",
        "",
        "## CP-SAT behavior", "",
        f"The progress tables measure actual incumbent-log timestamps. IA first feasible candidates captured a median {cp['interaction_aware']['first_feasible_fraction_of_final_gain']['median']:.3f} of eventual local gain; TOP captured {cp['top_individual']['first_feasible_fraction_of_final_gain']['median']:.3f}. Median first-feasible-to-final-best time was {cp['interaction_aware']['first_to_final_best_seconds']['median']:.2f}s for IA and {cp['top_individual']['first_to_final_best_seconds']['median']:.2f}s for TOP; median post-best proof lag on OPTIMAL solves was {cp['interaction_aware']['proof_lag_seconds']['median']:.2f}s and {cp['top_individual']['proof_lag_seconds']['median']:.2f}s. This is the appropriate basis for a future shorter-time qualification; it does not change any current search limit.", "",
        f"There were {len(hard)} non-OPTIMAL direct solves. Their compact per-solve records are in `hard_cases.csv`; FEASIBLE/UNKNOWN are not treated as infeasible.", "",
        "## Remaining headroom", "",
        "The largest TOP-endpoint weighted penalties are: " + ", ".join(f"{row['component']} ({row['top_remaining_weighted_penalty']:.0f})" for row in remaining[:3]) + ". Dedicated difficulty/category targeting is therefore a stronger next component-specific question than semester targeting, while full direct-v2 validation remains the authority.", "",
        "## Resource interpretation", "",
        f"Peak tree RSS was {resources['interaction_aware']['tree_rss_bytes']['max'] / 2**30:.2f} GiB for IA and {resources['top_individual']['tree_rss_bytes']['max'] / 2**30:.2f} GiB for TOP. Supervisor records and resource summaries are reported separately; host measurements alone are not used to attribute quality differences.", "",
        "## Limits", "",
        "The strongest established conclusion is that this particular TOP trajectory preserved higher long-horizon gain. A same-state matched IA-versus-TOP study is required before claiming that utilization rank, overlap, freshness, or selector agreement causes policy superiority.",
    ]
    (output / "research_findings.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def make_next_design(output: Path) -> None:
    text = """# Preregistered next experiments (not executed)

## Strongest next target-selection study: same-state matched IA versus TOP

Select 12 frozen authoritative states: four early (0-22.5m), four middle (22.5-67.5m), and four late (67.5-90m), stratified across IA utilization-rank depth, IA/TOP shadow overlap, recent gain, utilization headroom, and scope freshness. At each state, compute both policy scopes from the identical state and solve both with unchanged direct exact-v2 R16/S4, 8 search workers, 1 validation worker, current-incumbent hints, 300s requested search and 180s validation. Use 3 deterministic repeats per policy/state (72 cells), randomized policy order within state.

Primary preregistered outcome: validated strict v2 gain. Secondary outcomes: component gains, first-feasible latency, final-best latency, proof lag, boundary saturation, and resource guard outcomes. The primary analysis is paired within-state median gain and sign/direction consistency, not a pooled attempt-level classifier. A routing feature is eligible only if its interaction is stable across state strata and repeats.

## Strongest next neighborhood study

Run a same-state direct-v2 R16/S4 versus R32/S4 versus R64/S4 qualification on a balanced subset of the matched states, with full validation and a fixed total resource contract. Boundary saturation is the decision signal: persistent 16-decision saturation supports testing larger R before increasing S; frequent four-student saturation supports an S fork.

## Strongest next component-specific study

Prioritize difficulty-specific and category-diversity-specific neighborhood proposals, then validate through unchanged direct exact-v2. Semester-load targeting ranks lower unless fresh data demonstrate movable semester headroom. Do not alter objective weights or make the targeter a quality authority.

## Other ranked evidence

1. Replicated dynamic IA/TOP trajectories: needed for external validity.
2. Same-state matched IA/TOP: needed before adaptive routing.
3. R16/R32/R64: justified only to resolve boundary saturation.
4. Shorter-time qualification: use the retrospective incumbent curves to select caps.
5. Difficulty/category proposals: justified by remaining endpoint headroom.
6. Adaptive routing: last, contingent on the matched-state result.
"""
    (output / "next_experiment_design.md").write_text(text, encoding="utf-8")


def lead_changes(rows_by_branch: dict[str, list[dict[str, Any]]], source_value: float) -> list[dict[str, Any]]:
    """Use the union of authoritative adoption timestamps, not only 5-minute bins."""
    times = sorted({0.0, *(row["branch_elapsed_seconds"] for rows in rows_by_branch.values() for row in rows)})
    output, previous = [], "tie"
    for seconds in times:
        ia = trajectory_at(rows_by_branch["interaction_aware"], source_value, seconds)["gain"]
        top = trajectory_at(rows_by_branch["top_individual"], source_value, seconds)["gain"]
        leader = "IA" if ia > top else "TOP" if top > ia else "tie"
        if leader != previous:
            output.append({"trusted_elapsed_seconds": seconds, "trusted_elapsed_minutes": seconds / 60, "leader_before": previous, "leader_after": leader, "ia_gain": ia, "top_gain": top, "ia_minus_top_gain": ia - top})
            previous = leader
    return output


def cp_sat_aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    substantive = [row for row in rows if row["final_discovery_gain"] is not None and row["final_discovery_gain"] > 0]
    first_gain_fractions = []
    for row in substantive:
        if row["timeline"] and row["starting_value"] is not None:
            first_gain_fractions.append((float(row["starting_value"]) - float(row["timeline"][0]["objective"])) / row["final_discovery_gain"])
    return {
        "attempts": len(rows), "substantive_solves": len(substantive),
        "status_counts": dict(Counter(str(row["status"]) for row in rows)),
        "final_gain": summary([row["final_discovery_gain"] for row in substantive]),
        "first_feasible_seconds": summary([row["first_feasible_seconds"] for row in substantive]),
        "first_feasible_fraction_of_final_gain": summary(first_gain_fractions),
        "first_to_final_best_seconds": summary([(row["final_best_seconds"] - row["first_feasible_seconds"]) for row in substantive if row["final_best_seconds"] is not None and row["first_feasible_seconds"] is not None]),
        "proof_lag_seconds": summary([row["proof_lag_seconds"] for row in substantive if row["status"] == "optimal"]),
        "solver_wall_seconds": summary([row["solver_wall_seconds"] for row in rows]),
        "final_best_seconds": summary([row["final_best_seconds"] for row in substantive]),
    }


def write_representative_timelines(output: Path, rows_by_branch: dict[str, list[dict[str, Any]]]) -> None:
    lines = ["# Representative observed CP-SAT incumbent timelines", "", "Times are native-solver elapsed seconds from the recorded CP-SAT progress log. Values are direct-v2 objectives (lower is better). These are observations, not re-solved traces.", ""]
    for branch, rows in rows_by_branch.items():
        candidates = [row for row in rows if row["timeline"] and row["final_discovery_gain"] and row["final_discovery_gain"] > 0]
        chosen = []
        if candidates:
            chosen = [max(candidates, key=lambda row: row["final_discovery_gain"]), max(candidates, key=lambda row: row["proof_lag_seconds"] or -1)]
        lines.extend([f"## {SHORT[branch]}", ""])
        seen = set()
        for row in chosen:
            if row["attempt_index"] in seen:
                continue
            seen.add(row["attempt_index"])
            points = row["timeline"]
            # Keep the first two, a few evenly spaced improvements, and final best.
            indexes = sorted({0, min(1, len(points)-1), *(round(i * (len(points)-1) / 4) for i in range(1, 4)), len(points)-1})
            compact = " -> ".join(f"{float(points[i]['elapsed_seconds']):.2f}s {float(row['starting_value']) - float(points[i]['objective']):+.0f}" for i in indexes)
            lines.append(f"- attempt {row['attempt_index']}: {compact}; final-best {row['final_best_seconds']:.2f}s; solver wall {row['solver_wall_seconds']:.2f}s; proof lag {row['proof_lag_seconds']:.2f}s; status {row['status']}.")
        lines.append("")
    (output / "representative_cp_sat_timelines.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def maybe_plot(output: Path, trajectory: list[dict[str, Any]]) -> None:
    try:
        import matplotlib.pyplot as plt  # optional, analysis remains stdlib-only
    except ImportError:
        return
    fig, axis = plt.subplots(figsize=(8, 4.5))
    for policy, color in (("IA", "#4477aa"), ("TOP", "#cc6677")):
        rows = [row for row in trajectory if row["policy"] == policy]
        axis.plot([row["minute"] for row in rows], [row["cumulative_gain"] for row in rows], marker="o", label=policy, color=color)
    axis.set(xlabel="trusted branch elapsed time (minutes)", ylabel="cumulative validated v2 gain", title="Dynamic IA versus TOP trajectory")
    axis.grid(alpha=.25); axis.legend(); fig.tight_layout(); fig.savefig(output / "trajectory_5min.png", dpi=180); plt.close(fig)


def analyze(lineage: Path, output: Path) -> dict[str, Any]:
    lineage = lineage.resolve()
    if not (lineage / "SEALED").exists():
        raise RuntimeError("source lineage is not sealed")
    if output.exists():
        raise FileExistsError(f"output already exists: {output}")
    output.mkdir(parents=True)
    seal_verification = verify_source_seal(lineage)
    write_json(output / "source_seal_verification.json", seal_verification)
    source_identity = read_json(lineage / "source" / "source_identity.json")
    endpoint = endpoint_summary(lineage, source_identity)
    for branch in POLICIES:
        facts = endpoint["branches"][branch]
        boot, fresh, durable = facts["bootstrap_source_validation"], facts["endpoint_validation"], facts["durable_branch_validation"]
        expected_assignments = int(source_identity["assignment_count"])
        expected_commitments = int(source_identity["special_commitment_count"])
        expected_groups = int(source_identity["required_decision_group_count"])
        if (
            not facts["same_source_fingerprint"] or not boot.get("full_model_validation") or not boot.get("complete")
            or not fresh.get("full_model_validation") or not fresh.get("complete")
            or int(fresh.get("assignment_count", 0) or 0) != expected_assignments
            or int(fresh.get("special_commitment_count", 0) or 0) != expected_commitments
            or int(fresh.get("unmet_request_count", -1) or 0) != 0
            or int(durable.get("required_source_decision_group_count", 0) or 0) != expected_groups
        ):
            raise RuntimeError(f"source authority failure for {branch}")
    write_json(output / "source_and_endpoint_validation.json", endpoint)
    write_json(output / "lineage_inventory.json", inventory(lineage))
    checkpoints, checkpoint_audit = checkpoint_rows(lineage)
    write_csv(output / "checkpoint_metadata_summary.csv", checkpoints)
    write_json(output / "checkpoint_metadata_audit.json", checkpoint_audit)

    all_rows = {branch: [parse_attempt(path, branch) for path in sorted((lineage / "branches" / branch / "attempts").glob("attempt_*.json"))] for branch in POLICIES}
    for branch in POLICIES:
        previous = None
        seen_students: set[int] = set()
        for row in all_rows[branch]:
            row["previous_scope_jaccard"] = jaccard(previous, row["active_scope"]) if previous is not None else None
            row["scope_freshness"] = 1 - max((jaccard(prior["active_scope"], row["active_scope"]) for prior in all_rows[branch] if prior["attempt_index"] < row["attempt_index"]), default=0)
            row["reused_student_count"] = len(set(row["active_scope"]) & seen_students)
            row["reused_student_share"] = row["reused_student_count"] / len(row["active_scope"]) if row["active_scope"] else None
            seen_students.update(row["active_scope"])
            previous = row["active_scope"]
    flat_rows = [row for branch in POLICIES for row in all_rows[branch]]
    write_csv(output / "attempt_summary.csv", [{key: value for key, value in row.items() if key != "timeline"} for row in flat_rows])

    source_value = float(source_identity["source_value"])
    trajectory = []
    for minute in range(0, 91, 5):
        states = {branch: trajectory_at(all_rows[branch], source_value, minute * 60) for branch in POLICIES}
        difference = states["interaction_aware"]["gain"] - states["top_individual"]["gain"]
        leader = "IA" if difference > 1e-9 else "TOP" if difference < -1e-9 else "tie"
        for branch in POLICIES:
            row = {"minute": minute, "policy": SHORT[branch], "branch": branch, "current_v2_score": states[branch]["score"], "cumulative_gain": states[branch]["gain"], "cumulative_adoptions": states[branch]["adoptions"], "ia_minus_top_gain": difference, "leader": leader}
            row.update({f"cumulative_{name}": states[branch][name] for name in COMPONENTS})
            trajectory.append(row)
    write_csv(output / "trajectory_5min.csv", trajectory)
    write_csv(output / "trajectory_leader_changes.csv", lead_changes(all_rows, source_value))
    quarters = {branch: quarter_rows(rows) for branch, rows in all_rows.items()}
    write_csv(output / "trajectory_quartiles.csv", [{"branch": branch, "policy": SHORT[branch], **row} for branch, values in quarters.items() for row in values])

    progress = []
    for row in flat_rows:
        timeline = row["timeline"]
        item = {key: value for key, value in row.items() if key not in {"timeline", "active_scope", "shadow_scope", "utilization_ranks", "shadow_utilization_ranks"}}
        item["first_feasible_gain"] = (float(row["starting_value"]) - float(timeline[0]["objective"])) if timeline and row["starting_value"] is not None else None
        item["first_to_final_gain_seconds"] = (row["final_best_seconds"] - row["first_feasible_seconds"]) if row["final_best_seconds"] is not None and row["first_feasible_seconds"] is not None else None
        for pct in (.25, .5, .75, .9, .95, .99):
            target = float(row["starting_value"]) - float(row["final_discovery_gain"] or 0) * pct if row["starting_value"] is not None else None
            item[f"gain_{int(pct*100)}pct_seconds"] = first_time_for_objective(timeline, target) if target is not None else None
        progress.append(item)
    write_csv(output / "cp_sat_progress_summary.csv", progress)
    write_json(output / "cp_sat_aggregate.json", {branch: cp_sat_aggregate(rows) for branch, rows in all_rows.items()})
    write_representative_timelines(output, all_rows)
    write_csv(output / "time_cap_retrospective.csv", [{"branch": branch, "policy": SHORT[branch], **row} for branch, rows in all_rows.items() for row in time_cap_rows(rows, CAPS)] + [{"branch": branch, "policy": SHORT[branch], **row} for branch, rows in all_rows.items() for row in time_cap_rows(rows, RELATIVE_CAPS, relative=True)])
    # A deadline-created placeholder can have no native solve at all.  Keep it
    # out of the direct-solve hard-case table while retaining it in the attempt
    # ledger and phase-event inventory.
    hard = [row for row in progress if row["solver_wall_seconds"] > 0 and row["status"] != "optimal"]
    write_csv(output / "hard_cases.csv", hard)

    active_shadow = []
    for row in flat_rows:
        active_shadow.append({
            "branch": row["branch"], "policy": row["policy"], "attempt_index": row["attempt_index"], "minute": row["branch_elapsed_minutes"], "gain": row["gain"],
            "active_scope": row["active_scope"], "shadow_scope": row["shadow_scope"], "overlap_count": row["active_shadow_overlap"], "jaccard": row["active_shadow_jaccard"],
            "active_mean_utilization_rank": row["mean_utilization_rank"], "shadow_mean_utilization_rank": mean(row["shadow_utilization_ranks"]),
            "active_only": sorted(set(row["active_scope"]) - set(row["shadow_scope"])), "shadow_only": sorted(set(row["shadow_scope"]) - set(row["active_scope"])),
            "utilization_headroom": row["utilization_headroom"], "active_selection_leverage": row["active_selection_leverage"], "shadow_selection_leverage": row["shadow_selection_leverage"],
        })
    write_csv(output / "active_shadow_comparison.csv", active_shadow)
    scope_summaries, frequencies = {}, []
    for branch, rows in all_rows.items():
        scope_summaries[branch], items = scope_diversity(rows)
        frequencies.extend([{"branch": branch, "policy": SHORT[branch], **item} for item in items])
    write_json(output / "scope_selection_summary.json", scope_summaries)
    write_csv(output / "scope_selection_summary.csv", [{"branch": branch, "policy": SHORT[branch], **facts} for branch, facts in scope_summaries.items()])
    write_csv(output / "student_target_frequency.csv", frequencies)

    component_rows = []
    for branch, rows in all_rows.items():
        cumulative = {name: 0.0 for name in COMPONENTS}
        for row in rows:
            if row["adopted"]:
                for name in COMPONENTS:
                    cumulative[name] += row[f"component_{name}"]
            component_rows.append({"branch": branch, "policy": SHORT[branch], "attempt_index": row["attempt_index"], "minute": row["branch_elapsed_minutes"], **{f"cumulative_{name}": value for name, value in cumulative.items()}})
    write_csv(output / "component_trajectory.csv", component_rows)
    boundary = []
    for branch, rows in all_rows.items():
        for quarter in quarter_rows(rows):
            start, end = quarter["start_minute"] * 60, quarter["end_minute"] * 60
            members = [row for row in rows if start < row["branch_elapsed_seconds"] <= end]
            boundary.append({"branch": branch, "policy": SHORT[branch], "quarter": quarter["quarter"], "attempts": len(members), "adoptions": sum(r["adopted"] for r in members), "student_boundary_saturation_rate": mean([r["student_boundary_saturated"] for r in members]), "decision_boundary_saturation_rate": mean([r["decision_boundary_saturated"] for r in members])})
    write_csv(output / "boundary_saturation.csv", boundary)

    phase = {branch: phase_event_summary(lineage / "branches" / branch / "phase_events.jsonl") for branch in POLICIES}
    resources = {branch: resource_summary(lineage / "branches" / branch / "resource_samples.jsonl") for branch in POLICIES}
    for branch in POLICIES:
        resources[branch]["supervisor"] = read_json(lineage / "branches" / branch / "supervisor_event.json")
    write_json(output / "phase_event_summary.json", phase)
    write_json(output / "resource_summary.json", resources)

    associations = {}
    for branch, rows in all_rows.items():
        associations[branch] = {
            "rank_vs_gain": spearman(rows, "mean_utilization_rank", "gain"), "max_rank_vs_gain": spearman(rows, "max_utilization_rank", "gain"),
            "shadow_jaccard_vs_gain": spearman(rows, "active_shadow_jaccard", "gain"), "scope_freshness_vs_gain": spearman(rows, "scope_freshness", "gain"),
            "previous_scope_overlap_vs_gain": spearman(rows, "previous_scope_jaccard", "gain"), "student_reuse_vs_gain": spearman(rows, "reused_student_count", "gain"), "headroom_vs_gain": spearman(rows, "utilization_headroom", "gain"),
            "high_agreement_mean_gain": mean([r["gain"] for r in rows if r["active_shadow_overlap"] >= 3]), "low_agreement_mean_gain": mean([r["gain"] for r in rows if r["active_shadow_overlap"] <= 1]),
        }
    # Shadow-policy behavior on the opposite policy's created state.
    associations["path_dependence"] = {
        "ia_active_on_ia_states_mean_rank": mean([r["mean_utilization_rank"] for r in all_rows["interaction_aware"]]),
        "ia_shadow_on_top_states_mean_rank": mean([mean(r["shadow_utilization_ranks"]) for r in all_rows["top_individual"]]),
        "top_active_on_top_states_mean_rank": mean([r["mean_utilization_rank"] for r in all_rows["top_individual"]]),
        "top_shadow_on_ia_states_mean_rank": mean([mean(r["shadow_utilization_ranks"]) for r in all_rows["interaction_aware"]]),
    }
    write_json(output / "statistical_associations.json", associations)
    agreement = {}
    for branch, rows in all_rows.items():
        groups = defaultdict(list)
        for row in rows:
            groups[str(row["active_shadow_overlap"])].append(row["gain"])
        agreement[branch] = {overlap: {"n": len(values), "mean_gain": mean(values), "median_gain": median(values)} for overlap, values in sorted(groups.items())}
    write_json(output / "active_shadow_agreement_summary.json", agreement)
    rank_analysis = {}
    selector_quarters = []
    for branch, rows in all_rows.items():
        selected_ranks = [rank for row in rows for rank in row["utilization_ranks"] if rank is not None]
        component_rank = {name: spearman(rows, "mean_utilization_rank", f"component_{name}") for name in COMPONENTS}
        by_quarter = []
        for start, end in QUARTERS:
            members = [row for row in rows if start * 60 < row["branch_elapsed_seconds"] <= end * 60]
            facts = {
                "quarter": f"{start:g}-{end:g}m", "attempts": len(members),
                "active_rank": summary([rank for row in members for rank in row["utilization_ranks"] if rank is not None]),
                "shadow_rank": summary([rank for row in members for rank in row["shadow_utilization_ranks"] if rank is not None]),
                "active_shadow_jaccard": summary([row["active_shadow_jaccard"] for row in members]),
                "mean_gain": mean([row["gain"] for row in members]),
            }
            by_quarter.append(facts)
            selector_quarters.append({"branch": branch, "policy": SHORT[branch], **facts})
        rank_analysis[branch] = {"selected_rank_distribution": summary(selected_ranks), "rank_vs_component_gain": component_rank, "by_quarter": by_quarter}
    write_json(output / "utilization_rank_analysis.json", rank_analysis)
    write_csv(output / "selector_quarter_summary.csv", selector_quarters)

    source_quality = endpoint["branches"]["interaction_aware"]["bootstrap_source_quality"]
    source_components = component_snapshot(source_quality)
    headroom = []
    for component in COMPONENTS:
        entry = {"component": component, "source_weighted_penalty": source_components.get(component, 0.0)}
        for branch in POLICIES:
            final_quality = endpoint["branches"][branch]["endpoint_validation"].get("branch", {}).get("quality", {})
            # endpoint envelope keeps branch alongside validation at top level, not inside validation.
            if not final_quality:
                final_doc = read_json(lineage / "branches" / branch / "final_validation.json")
                final_quality = (final_doc.get("branch") or {}).get("quality") or {}
            final_penalty = component_snapshot(final_quality).get(component, 0.0)
            entry[f"{SHORT[branch].lower()}_final_weighted_penalty"] = final_penalty
            entry[f"{SHORT[branch].lower()}_reduction"] = entry["source_weighted_penalty"] - final_penalty
            entry[f"{SHORT[branch].lower()}_reduction_percent"] = (entry[f"{SHORT[branch].lower()}_reduction"] / entry["source_weighted_penalty"] * 100) if entry["source_weighted_penalty"] else None
        entry["top_remaining_weighted_penalty"] = entry["top_final_weighted_penalty"]
        headroom.append(entry)
    write_csv(output / "objective_headroom.csv", headroom)
    make_report(output, source_value, all_rows, trajectory, quarters, scope_summaries, associations, headroom, resources, hard)
    make_next_design(output)
    maybe_plot(output, trajectory)
    manifest = []
    for path in sorted(output.rglob("*")):
        if path.is_file():
            manifest.append({"path": path.relative_to(output).as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size})
    write_json(output / "derived_artifact_manifest.json", {"source_lineage": str(lineage), "created_at_utc": datetime.now(timezone.utc).isoformat(), "files": manifest})
    return {"output": str(output), "attempts": {branch: len(rows) for branch, rows in all_rows.items()}, "source_value": source_value}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lineage", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    if args.output_dir:
        output = args.output_dir
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        output = args.lineage.parent / f"{args.lineage.name}_posthoc_{stamp}_{secrets.token_hex(4)}"
    result = analyze(args.lineage, output)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
