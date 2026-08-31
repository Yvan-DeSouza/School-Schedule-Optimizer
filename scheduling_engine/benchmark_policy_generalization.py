"""Startup-aware, clean-process Objective Semantics v2 policy study.

This is an offline research runner.  It compares the existing adaptive,
stateless-role, and fixed-cycle selectors through the existing supervised
calibration boundary.  It does not participate in ordinary scheduling and
does not alter constraints, objectives, validation authority, or persisted
application state.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import statistics
from time import perf_counter

from .benchmark_adaptive_calibration import run_supervised_calibration_trial
from .student_assignment.adaptive_calibration import (
    CALIBRATION_PROFILES,
    STARTUP_AWARE_MAX_OPERATOR_SECONDS,
    STARTUP_AWARE_PROTOCOL_VERSION,
    STARTUP_AWARE_SESSION_OVERRIDES,
    STARTUP_AWARE_TOTAL_POLICY_SECONDS,
    profile_fingerprint,
)
from .student_assignment.stage2_benchmark import read_durable_stage2_benchmark


STARTUP_AWARE_STUDY_ID = "v2_policy_generalization_startup_aware_20260831"
STARTUP_AWARE_STUDY_SCHEMA = "student_assignment_policy_generalization_study_v1"
STARTUP_AWARE_RESULT_SCHEMA = "student_assignment_policy_generalization_result_v1"
STARTUP_AWARE_POLICIES = ("adaptive", "stateless_role", "fixed_cycle")
STARTUP_AWARE_SEEDS = (101, 202, 303)
STARTUP_AWARE_PROFILE = "balanced"
STARTUP_AWARE_VALIDATION_SECONDS = 180.0
STARTUP_AWARE_PARENT_HARD_WALL_SECONDS = 1800.0
STARTUP_AWARE_WORKER_COUNT = 1

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_BENCHMARK_ROOT = (
    _REPOSITORY_ROOT
    / "scheduling_engine"
    / "benchmarks"
    / "student_assignment"
    / "v2_policy_generalization_suite_20260829"
)
TARGET_SCENARIO_DIRECTORIES = {
    "reference_target": _BENCHMARK_ROOT / "reference_target",
    "special_commitment_pressure_target": (
        _BENCHMARK_ROOT / "special_commitment_pressure_target"
    ),
}


def _json_write_atomic(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative_path(path):
    try:
        return str(Path(path).resolve().relative_to(_REPOSITORY_ROOT))
    except ValueError:
        return str(Path(path))


def startup_aware_policy_budget_contract():
    """Return the preregistered fair-budget contract for the study."""

    return {
        "protocol_version": STARTUP_AWARE_PROTOCOL_VERSION,
        "profile": STARTUP_AWARE_PROFILE,
        "profile_fingerprint": profile_fingerprint(STARTUP_AWARE_PROFILE),
        "worker_count": STARTUP_AWARE_WORKER_COUNT,
        "cp_sat_random_seeds": list(STARTUP_AWARE_SEEDS),
        "per_operator_maximum_seconds": STARTUP_AWARE_MAX_OPERATOR_SECONDS,
        "cumulative_policy_budget_seconds": STARTUP_AWARE_TOTAL_POLICY_SECONDS,
        "candidate_validation_time_limit_seconds": (
            STARTUP_AWARE_VALIDATION_SECONDS
        ),
        "candidate_validation_worker_count": 1,
        "parent_hard_wall_seconds": STARTUP_AWARE_PARENT_HARD_WALL_SECONDS,
        "session_overrides": STARTUP_AWARE_SESSION_OVERRIDES,
        "full_model_validation_required": True,
        "unvalidated_candidate_adoption": False,
        "ordinary_stage2_between_iterations": False,
        "production_policy_wiring": False,
    }


def _scenario_manifest(scenario_id):
    directory = TARGET_SCENARIO_DIRECTORIES[scenario_id]
    benchmark = read_durable_stage2_benchmark(directory)
    manifest = benchmark["manifest"]
    return {
        "scenario_id": scenario_id,
        "benchmark_directory": _relative_path(directory),
        "input_fingerprint": benchmark["input_semantic_fingerprint"],
        "source_seed_fingerprint": manifest["seed_source_decision_fingerprint"],
        "counts": dict(manifest.get("counts") or {}),
        "objective_semantics_version": (
            benchmark["data"].objective_semantics_version
        ),
        "artifact_hashes": {
            name: metadata["sha256_compressed"]
            for name, metadata in (manifest.get("artifacts") or {}).items()
        },
    }


def build_startup_aware_study_manifest(
    *,
    study_directory,
    scenario_ids=("reference_target", "special_commitment_pressure_target"),
):
    """Build a new child-study manifest without mutating source studies."""

    unknown = set(scenario_ids) - set(TARGET_SCENARIO_DIRECTORIES)
    if unknown:
        raise ValueError(f"Unknown target scenarios: {sorted(unknown)}")
    return {
        "schema": STARTUP_AWARE_STUDY_SCHEMA,
        "study_id": STARTUP_AWARE_STUDY_ID,
        "purpose": (
            "Startup-aware target-scale comparison of existing adaptive, "
            "stateless-role, and fixed-cycle policy selectors."
        ),
        "synthetic_only": True,
        "production_wiring": False,
        "objective_semantics_version": "v2",
        "source_lineage": "v2_policy_generalization_suite_20260829",
        "policies": list(STARTUP_AWARE_POLICIES),
        "seeds": list(STARTUP_AWARE_SEEDS),
        "scenario_ids": list(scenario_ids),
        "budget_contract": startup_aware_policy_budget_contract(),
        "scenarios": {
            scenario_id: _scenario_manifest(scenario_id)
            for scenario_id in scenario_ids
        },
        "results": {},
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "study_directory": _relative_path(study_directory),
    }


def initialize_startup_aware_study(study_directory, *, scenario_ids=None):
    """Create the new study manifest, refusing to overwrite an existing one."""

    study_directory = Path(study_directory)
    manifest_path = study_directory / "study_manifest.json"
    if manifest_path.exists():
        raise FileExistsError(f"Study manifest already exists: {manifest_path}")
    manifest = build_startup_aware_study_manifest(
        study_directory=study_directory,
        scenario_ids=(
            tuple(scenario_ids)
            if scenario_ids is not None
            else tuple(TARGET_SCENARIO_DIRECTORIES)
        ),
    )
    _json_write_atomic(manifest_path, manifest)
    return manifest


def _result_filename(scenario_id, policy, seed):
    return f"{scenario_id}_{policy}_seed{int(seed)}.json"


def _policy_accounting(payload):
    attempts = tuple(payload.get("attempts") or ())
    cp_sat_seconds = sum(
        float(
            item.get("session_cp_sat_seconds")
            or item.get("solver_wall_time_seconds")
            or 0.0
        )
        for item in attempts
    )
    validation_seconds = sum(
        float(
            item.get("session_validation_seconds")
            or item.get("validation_seconds")
            or 0.0
        )
        for item in attempts
    )
    return {
        "attempt_count": len(attempts),
        "adopted_count": sum(bool(item.get("adopted")) for item in attempts),
        "cumulative_cp_sat_seconds": cp_sat_seconds,
        "cumulative_validation_seconds": validation_seconds,
        "policy_phase_timings": dict(
            (payload.get("timing") or {}).get("phase_timings") or {}
        ),
        "worker_phase_timings": dict(
            (payload.get("phase_timings") or {}).get("worker") or {}
        ),
    }


def run_startup_aware_policy_cell(
    *,
    study_directory,
    scenario_id,
    policy,
    seed,
    profile=STARTUP_AWARE_PROFILE,
    benchmark_directory=None,
):
    """Run one target cell and append its immutable result metadata."""

    if scenario_id not in TARGET_SCENARIO_DIRECTORIES:
        raise ValueError(f"Unknown target scenario: {scenario_id}")
    if policy not in STARTUP_AWARE_POLICIES:
        raise ValueError(f"Unknown policy: {policy}")
    if int(seed) not in STARTUP_AWARE_SEEDS:
        raise ValueError(f"Seed is not preregistered: {seed}")
    if profile not in CALIBRATION_PROFILES:
        raise ValueError(f"Unknown calibration profile: {profile}")

    study_directory = Path(study_directory)
    manifest_path = study_directory / "study_manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(
            "Initialize the startup-aware study before running cells"
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    scenario = manifest["scenarios"][scenario_id]
    benchmark_directory = Path(
        benchmark_directory or (
            _REPOSITORY_ROOT / scenario["benchmark_directory"]
        )
    )
    result_path = study_directory / "results" / _result_filename(
        scenario_id, policy, seed
    )
    if result_path.exists():
        raise FileExistsError(f"Result artifact already exists: {result_path}")

    started = perf_counter()
    try:
        payload = run_supervised_calibration_trial(
            policy=policy,
            profile=profile,
            benchmark_directory=benchmark_directory,
            total_time_limit_seconds=STARTUP_AWARE_TOTAL_POLICY_SECONDS,
            per_operator_time_limit_seconds=STARTUP_AWARE_MAX_OPERATOR_SECONDS,
            worker_count=STARTUP_AWARE_WORKER_COUNT,
            validation_time_limit_seconds=STARTUP_AWARE_VALIDATION_SECONDS,
            hard_wall_seconds=STARTUP_AWARE_PARENT_HARD_WALL_SECONDS,
            startup_aware=True,
            cp_sat_random_seed=int(seed),
        )
        payload = dict(payload)
        payload.update({
            "schema": STARTUP_AWARE_RESULT_SCHEMA,
            "study_id": STARTUP_AWARE_STUDY_ID,
            "scenario_id": scenario_id,
            "policy": policy,
            "seed": int(seed),
            "profile": profile,
            "source_lineage": {
                "input_fingerprint": scenario["input_fingerprint"],
                "source_seed_fingerprint": scenario["source_seed_fingerprint"],
            },
            "budget_contract": startup_aware_policy_budget_contract(),
            "policy_accounting": _policy_accounting(payload),
            "cell_elapsed_seconds": perf_counter() - started,
        })
    except Exception as error:
        # Preserve a machine-readable blocked/error result without implying a
        # schedule was produced.  In particular, a rejected stale source
        # artifact is not silently repaired or treated as a policy loss.
        payload = {
            "schema": STARTUP_AWARE_RESULT_SCHEMA,
            "study_id": STARTUP_AWARE_STUDY_ID,
            "scenario_id": scenario_id,
            "policy": policy,
            "seed": int(seed),
            "profile": profile,
            "status": "source_or_runner_error",
            "error_type": type(error).__name__,
            "error": str(error),
            "source_lineage": {
                "input_fingerprint": scenario["input_fingerprint"],
                "source_seed_fingerprint": scenario["source_seed_fingerprint"],
            },
            "budget_contract": startup_aware_policy_budget_contract(),
            "cell_elapsed_seconds": perf_counter() - started,
        }

    _json_write_atomic(result_path, payload)
    artifact_hash = _sha256_file(result_path)
    manifest["results"][_result_filename(scenario_id, policy, seed)] = {
        "path": _relative_path(result_path),
        "sha256": artifact_hash,
        "status": payload.get("execution_status") or payload.get("status"),
        "final_substantive_value": payload.get("final_substantive_value"),
        "candidate_complete": payload.get("candidate_complete"),
    }
    _json_write_atomic(manifest_path, manifest)
    return payload


def _load_startup_aware_results(study_directory):
    """Load and integrity-check the immutable result artifacts for a study."""

    study_directory = Path(study_directory)
    manifest_path = study_directory / "study_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    loaded = {}
    missing = []
    mismatched = []
    for filename, metadata in sorted((manifest.get("results") or {}).items()):
        path = Path(metadata["path"])
        if not path.is_absolute():
            path = _REPOSITORY_ROOT / path
        if not path.exists():
            missing.append(filename)
            continue
        digest = _sha256_file(path)
        if digest != metadata.get("sha256"):
            mismatched.append(filename)
            continue
        loaded[filename] = json.loads(path.read_text(encoding="utf-8"))
    if missing or mismatched:
        raise ValueError(
            "Study artifact integrity failure: "
            f"missing={missing!r}, mismatched={mismatched!r}"
        )
    return manifest, loaded


def _cell_summary(filename, payload, manifest):
    """Reduce one large result artifact to reportable experiment facts."""

    attempts = tuple(payload.get("attempts") or ())
    trajectory = []
    for attempt in attempts:
        inner = tuple(attempt.get("inner_probe_summaries") or ())
        adopted_values = [
            item.get("candidate_substantive_value")
            for item in inner
            if item.get("adopted")
            and item.get("candidate_substantive_value") is not None
        ]
        trajectory.append({
            "operator": attempt.get("operator"),
            "status": attempt.get("status"),
            "adopted": bool(attempt.get("adopted")),
            "candidate_validated": bool(attempt.get("candidate_validated")),
            "candidate_values": adopted_values,
            "stopping_reason": attempt.get("stopping_reason"),
            "solver_seconds": attempt.get("solver_wall_time_seconds"),
            "validation_seconds": attempt.get("validation_seconds"),
        })
    resource = payload.get("resource") or {}
    timing = payload.get("timing") or {}
    accounting = payload.get("policy_accounting") or {}
    scenario = manifest["scenarios"][payload["scenario_id"]]
    return {
        "artifact": filename,
        "scenario_id": payload.get("scenario_id"),
        "policy": payload.get("policy"),
        "seed": payload.get("seed"),
        "input_fingerprint": scenario.get("input_fingerprint"),
        "source_seed_fingerprint": scenario.get("source_seed_fingerprint"),
        "final_source_decision_fingerprint": payload.get(
            "final_source_decision_fingerprint"
        ),
        "execution_status": payload.get("execution_status") or payload.get("status"),
        "candidate_complete": payload.get("candidate_complete"),
        "final_unmet_count": payload.get("final_unmet_count"),
        "initial_substantive_value": payload.get("initial_substantive_value"),
        "final_substantive_value": payload.get("final_substantive_value"),
        "final_components": payload.get("final_components"),
        "final_objective_vector": payload.get("final_objective_vector"),
        "final_assignment_count": payload.get("final_assignment_count"),
        "final_special_commitment_count": payload.get(
            "final_special_commitment_count"
        ),
        "attempt_count": len(attempts),
        "adopted_count": sum(bool(item.get("adopted")) for item in attempts),
        "trajectory": trajectory,
        "policy_seconds": (payload.get("phase_timings") or {})
        .get("policy", {})
        .get("total"),
        "cell_seconds": payload.get("cell_elapsed_seconds")
        or timing.get("total_elapsed_seconds"),
        "cp_sat_seconds": accounting.get("cumulative_cp_sat_seconds"),
        "validation_seconds": accounting.get("cumulative_validation_seconds"),
        "peak_tree_working_set_bytes": resource.get("peak_tree_working_set_bytes"),
        "peak_tree_uss_bytes": resource.get("peak_tree_uss_bytes"),
        "branch_revalidated": bool(
            ((payload.get("preparation") or {}).get("parent_branch_validation") or {})
            .get("full_model_validation")
        ),
    }


def summarize_startup_aware_study(study_directory):
    """Create a compact, integrity-checked report without rerunning a solver."""

    manifest, payloads = _load_startup_aware_results(study_directory)
    cells = [
        _cell_summary(filename, payload, manifest)
        for filename, payload in sorted(payloads.items())
    ]
    grouped = {}
    for cell in cells:
        grouped.setdefault(
            (cell["scenario_id"], cell["policy"]),
            [],
        ).append(cell)

    policy_summary = {}
    for (scenario_id, policy), group in sorted(grouped.items()):
        values = [float(cell["final_substantive_value"]) for cell in group]
        policy_summary[f"{scenario_id}:{policy}"] = {
            "scenario_id": scenario_id,
            "policy": policy,
            "seeds": [cell["seed"] for cell in group],
            "final_values": values,
            "median_final_value": statistics.median(values),
            "best_final_value": min(values),
            "worst_final_value": max(values),
            "direct_gains": [
                float(cell["initial_substantive_value"])
                - float(cell["final_substantive_value"])
                for cell in group
            ],
            "all_complete": all(cell["candidate_complete"] for cell in group),
            "all_unmet_free": all(cell["final_unmet_count"] == 0 for cell in group),
            "all_source_branches_revalidated": all(
                cell["branch_revalidated"] for cell in group
            ),
            "median_cell_seconds": statistics.median(
                float(cell["cell_seconds"]) for cell in group
            ),
            "median_policy_seconds": statistics.median(
                float(cell["policy_seconds"]) for cell in group
            ),
            "median_cp_sat_seconds": statistics.median(
                float(cell["cp_sat_seconds"]) for cell in group
            ),
            "median_validation_seconds": statistics.median(
                float(cell["validation_seconds"]) for cell in group
            ),
            "peak_working_set_bytes": max(
                int(cell["peak_tree_working_set_bytes"] or 0) for cell in group
            ),
        }

    scenario_winners = {}
    for scenario_id in manifest["scenario_ids"]:
        candidates = [
            item for key, item in policy_summary.items()
            if item["scenario_id"] == scenario_id
        ]
        if candidates:
            winner = min(candidates, key=lambda item: item["median_final_value"])
            scenario_winners[scenario_id] = {
                "policy": winner["policy"],
                "median_final_value": winner["median_final_value"],
                "reason": "lowest median final substantive value in this diagnostic study",
            }

    return {
        "schema": "student_assignment_policy_generalization_summary_v1",
        "study_id": manifest["study_id"],
        "protocol_version": manifest["budget_contract"]["protocol_version"],
        "source_lineage": manifest["source_lineage"],
        "production_wiring": False,
        "artifact_integrity": {
            "manifest_result_count": len(manifest.get("results") or {}),
            "loaded_result_count": len(cells),
            "all_result_hashes_verified": True,
        },
        "budget_contract": manifest["budget_contract"],
        "scenarios": manifest["scenarios"],
        "scenario_winners": scenario_winners,
        "policy_summary": policy_summary,
        "cells": cells,
        "conclusion": {
            "fixed_cycle_is_best_on_all_completed_scenarios": all(
                item["policy"] == "fixed_cycle"
                for item in scenario_winners.values()
            ),
            "production_promotion": (
                "separate_production_promotion_study_required"
            ),
            "interpretation": (
                "Fixed-cycle is the strongest diagnostic policy in the completed "
                "startup-aware scenarios; this study does not wire it into production."
            ),
        },
    }


def write_startup_aware_study_summary(study_directory):
    """Write the compact summary artifact and register its hash in the manifest."""

    study_directory = Path(study_directory)
    summary = summarize_startup_aware_study(study_directory)
    summary_path = study_directory / "study_summary.json"
    _json_write_atomic(summary_path, summary)
    manifest_path = study_directory / "study_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["summary_artifact"] = {
        "path": _relative_path(summary_path),
        "sha256": _sha256_file(summary_path),
    }
    _json_write_atomic(manifest_path, manifest)
    return summary


def main(argv=None):  # pragma: no cover - offline experiment entrypoint
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--study-directory", type=Path, required=True)
    parser.add_argument(
        "--initialize",
        action="store_true",
        help="Create the new study manifest and exit.",
    )
    parser.add_argument(
        "--summarize",
        action="store_true",
        help="Verify result artifacts and write a compact study summary.",
    )
    parser.add_argument(
        "--scenario",
        choices=tuple(TARGET_SCENARIO_DIRECTORIES),
    )
    parser.add_argument(
        "--policy",
        choices=STARTUP_AWARE_POLICIES,
    )
    parser.add_argument("--seed", type=int)
    args = parser.parse_args(argv)
    if args.initialize and args.summarize:
        parser.error("--initialize and --summarize are mutually exclusive")
    if args.initialize:
        payload = initialize_startup_aware_study(args.study_directory)
    elif args.summarize:
        payload = write_startup_aware_study_summary(args.study_directory)
    else:
        if args.scenario is None or args.policy is None or args.seed is None:
            parser.error("--scenario, --policy, and --seed are required")
        payload = run_startup_aware_policy_cell(
            study_directory=args.study_directory,
            scenario_id=args.scenario,
            policy=args.policy,
            seed=args.seed,
        )
    if args.initialize or args.summarize:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        print(json.dumps({
            "study_id": payload.get("study_id"),
            "scenario_id": payload.get("scenario_id"),
            "policy": payload.get("policy"),
            "seed": payload.get("seed"),
            "status": payload.get("execution_status") or payload.get("status"),
            "initial_substantive_value": payload.get("initial_substantive_value"),
            "final_substantive_value": payload.get("final_substantive_value"),
            "candidate_complete": payload.get("candidate_complete"),
            "attempt_count": len(tuple(payload.get("attempts") or ())),
            "adopted_count": sum(
                bool(item.get("adopted"))
                for item in tuple(payload.get("attempts") or ())
            ),
            "cell_elapsed_seconds": payload.get("cell_elapsed_seconds"),
            "artifact_sha256": _sha256_file(
                Path(args.study_directory)
                / "results"
                / _result_filename(args.scenario, args.policy, args.seed)
            ),
        }, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "STARTUP_AWARE_POLICIES",
    "STARTUP_AWARE_PROFILE",
    "STARTUP_AWARE_RESULT_SCHEMA",
    "STARTUP_AWARE_SEEDS",
    "STARTUP_AWARE_STUDY_ID",
    "STARTUP_AWARE_STUDY_SCHEMA",
    "TARGET_SCENARIO_DIRECTORIES",
    "build_startup_aware_study_manifest",
    "initialize_startup_aware_study",
    "run_startup_aware_policy_cell",
    "summarize_startup_aware_study",
    "startup_aware_policy_budget_contract",
    "write_startup_aware_study_summary",
]
