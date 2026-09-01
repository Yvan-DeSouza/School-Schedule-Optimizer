"""Clean-process Objective Semantics v2 policy-study runners.

The historical startup-aware runner compares the existing adaptive,
stateless-role, and fixed-cycle selectors.  The separate progressive runner
compares three named adaptive variants with fixed-cycle through the same
supervised calibration boundary.  These are offline research surfaces only;
they do not participate in ordinary scheduling or alter constraints,
objectives, validation authority, or persisted application state.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import statistics
from time import perf_counter
import threading
import time

from .benchmark_adaptive_calibration import run_supervised_calibration_trial
from .student_assignment.adaptive_calibration import (
    ADAPTIVE_POLICY_VARIANT_POLICIES,
    CALIBRATION_PROFILES,
    STARTUP_AWARE_MAX_OPERATOR_SECONDS,
    STARTUP_AWARE_PROTOCOL_VERSION,
    STARTUP_AWARE_SESSION_OVERRIDES,
    STARTUP_AWARE_TOTAL_POLICY_SECONDS,
    build_calibration_policy,
    fixed_cycle_control_request,
    profile_fingerprint,
)
from .student_assignment.adaptive_search import ADAPTIVE_ROLE_BIAS_MULTIPLIER
from .student_assignment.calibration_supervisor import process_tree_snapshot
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

# This is a separate, research-only study.  Historical startup-aware results
# intentionally keep their original policy set (including stateless_role).
PARALLEL_POLICY_STUDY_ID = "v2_policy_parallel_biased_adaptive_20260831"
PARALLEL_POLICY_STUDY_SCHEMA = "student_assignment_parallel_policy_study_v1"
PARALLEL_POLICY_RESULT_SCHEMA = "student_assignment_parallel_policy_result_v1"
PARALLEL_POLICY_PROTOCOL_VERSION = "adaptive-policy-parallel-study-v1"
PARALLEL_POLICY_STUDY_POLICIES = (
    "adaptive_balanced",
    "adaptive_student_pressure_biased",
    "adaptive_utilization_biased",
    "fixed_cycle",
)
PARALLEL_POLICY_STUDY_SEEDS = (101, 202, 303)
PARALLEL_POLICY_STUDY_PROFILE = "balanced"
PARALLEL_POLICY_STUDY_WORKER_COUNT = 1
PARALLEL_POLICY_STUDY_MAX_PARALLEL_TRIALS = 4
PARALLEL_POLICY_STUDY_DEFAULT_PARALLEL_TRIALS = 2
PARALLEL_POLICY_MEMORY_RESERVE_BYTES = 2 * 1024**3
PARALLEL_POLICY_MAX_SWAP_GROWTH_BYTES = 256 * 1024**2

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


def parallel_policy_fingerprint(policy, *, profile=PARALLEL_POLICY_STUDY_PROFILE):
    """Return the immutable policy identity used by the parallel study."""

    if policy not in PARALLEL_POLICY_STUDY_POLICIES:
        raise ValueError(f"Unknown parallel-study policy: {policy}")
    if profile not in CALIBRATION_PROFILES:
        raise ValueError(f"Unknown calibration profile: {profile}")
    config = build_calibration_policy(policy)
    payload = {
        "protocol_version": PARALLEL_POLICY_PROTOCOL_VERSION,
        "policy": policy,
        "profile": profile,
        "profile_fingerprint": profile_fingerprint(profile),
        "selection_policy": config["selection_policy"],
        "adaptive_policy_variant": config["adaptive_policy_variant"],
        "role_bias_multiplier": ADAPTIVE_ROLE_BIAS_MULTIPLIER,
        "fixed_cycle": [item.name for item in config["fixed_cycle"]],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def parallel_policy_budget_contract():
    """Return the fixed, diagnostic-only contract for the four-policy study."""

    return {
        "protocol_version": PARALLEL_POLICY_PROTOCOL_VERSION,
        "profile": PARALLEL_POLICY_STUDY_PROFILE,
        "profile_fingerprint": profile_fingerprint(
            PARALLEL_POLICY_STUDY_PROFILE
        ),
        "policies": list(PARALLEL_POLICY_STUDY_POLICIES),
        "policy_fingerprints": {
            policy: parallel_policy_fingerprint(policy)
            for policy in PARALLEL_POLICY_STUDY_POLICIES
        },
        "cp_sat_workers_per_trial": PARALLEL_POLICY_STUDY_WORKER_COUNT,
        "cp_sat_random_seeds": list(PARALLEL_POLICY_STUDY_SEEDS),
        "per_operator_maximum_seconds": STARTUP_AWARE_MAX_OPERATOR_SECONDS,
        "cumulative_policy_budget_seconds": STARTUP_AWARE_TOTAL_POLICY_SECONDS,
        "candidate_validation_time_limit_seconds": STARTUP_AWARE_VALIDATION_SECONDS,
        "parent_hard_wall_seconds": STARTUP_AWARE_PARENT_HARD_WALL_SECONDS,
        "full_model_validation_required": True,
        "unvalidated_candidate_adoption": False,
        "ordinary_stage2_between_iterations": False,
        "production_policy_wiring": False,
        "default_max_parallel_trials": PARALLEL_POLICY_STUDY_DEFAULT_PARALLEL_TRIALS,
        "maximum_parallel_trials": PARALLEL_POLICY_STUDY_MAX_PARALLEL_TRIALS,
        "memory_reserve_bytes": PARALLEL_POLICY_MEMORY_RESERVE_BYTES,
        "progressive_concurrency": [2, 3, 4],
    }


def build_parallel_policy_study_manifest(
    *,
    study_directory,
    scenario_ids=("reference_target", "special_commitment_pressure_target"),
):
    """Build a manifest for the new four-policy research study."""

    unknown = set(scenario_ids) - set(TARGET_SCENARIO_DIRECTORIES)
    if unknown:
        raise ValueError(f"Unknown target scenarios: {sorted(unknown)}")
    return {
        "schema": PARALLEL_POLICY_STUDY_SCHEMA,
        "study_id": PARALLEL_POLICY_STUDY_ID,
        "study_kind": "progressive_parallel_policy_comparison",
        "purpose": (
            "Research-only comparison of balanced adaptive, two bounded "
            "adaptive biases, and the existing fixed-cycle control."
        ),
        "synthetic_only": True,
        "production_wiring": False,
        "objective_semantics_version": "v2",
        "source_lineage": "v2_policy_generalization_suite_20260829",
        "policies": list(PARALLEL_POLICY_STUDY_POLICIES),
        "policy_fingerprints": {
            policy: parallel_policy_fingerprint(policy)
            for policy in PARALLEL_POLICY_STUDY_POLICIES
        },
        "seeds": list(PARALLEL_POLICY_STUDY_SEEDS),
        "scenario_ids": list(scenario_ids),
        "budget_contract": parallel_policy_budget_contract(),
        "scenarios": {
            scenario_id: _scenario_manifest(scenario_id)
            for scenario_id in scenario_ids
        },
        "results": {},
        "batches": [],
        "concurrency_history": [],
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "study_directory": _relative_path(study_directory),
    }


def initialize_parallel_policy_study(study_directory, *, scenario_ids=None):
    """Create the parallel-study manifest without overwriting existing state."""

    study_directory = Path(study_directory)
    manifest_path = study_directory / "study_manifest.json"
    if manifest_path.exists():
        raise FileExistsError(f"Study manifest already exists: {manifest_path}")
    manifest = build_parallel_policy_study_manifest(
        study_directory=study_directory,
        scenario_ids=(
            tuple(scenario_ids)
            if scenario_ids is not None
            else tuple(TARGET_SCENARIO_DIRECTORIES)
        ),
    )
    _json_write_atomic(manifest_path, manifest)
    return manifest


def _parallel_resource_snapshot(active_workers):
    """Capture aggregate resource facts for the currently running trial roots."""

    per_trial = {}
    for cell_key, pid in sorted(active_workers.items()):
        per_trial[cell_key] = process_tree_snapshot(pid)
    snapshots = tuple(per_trial.values())
    available_values = [
        item.get("system_available_memory_bytes")
        for item in snapshots
        if item.get("system_available_memory_bytes") is not None
    ]
    try:
        import psutil

        cpu_percent = float(psutil.cpu_percent(interval=None))
        logical_cpu_count = psutil.cpu_count(logical=True)
        swap_used = int(psutil.swap_memory().used)
    except (ImportError, AttributeError, OSError, ValueError):
        cpu_percent = None
        logical_cpu_count = None
        swap_used = None
    if not available_values:
        try:
            import psutil

            available_values = [int(psutil.virtual_memory().available)]
        except (ImportError, AttributeError, OSError, ValueError):
            available_values = []
    return {
        "active_trial_count": len(active_workers),
        "per_trial": per_trial,
        "aggregate_tree_rss_bytes": sum(
            int(item.get("tree_rss_bytes") or 0) for item in snapshots
        ),
        "aggregate_tree_uss_bytes": sum(
            int(item.get("tree_uss_bytes") or 0) for item in snapshots
        ),
        "minimum_system_available_memory_bytes": (
            min(available_values) if available_values else None
        ),
        "cpu_percent": cpu_percent,
        "logical_cpu_count": logical_cpu_count,
        "swap_used_bytes": swap_used,
    }


def _parallel_resource_monitor(active_workers, active_lock, stop_event, state):
    """Sample active trial trees until the parent completes the batch."""

    while not stop_event.is_set():
        with active_lock:
            active = dict(active_workers)
        snapshot = _parallel_resource_snapshot(active)
        with active_lock:
            state["samples"].append(snapshot)
            state["samples"] = state["samples"][-512:]
        stop_event.wait(0.25)


def _parallel_resource_summary(state, *, initial_available_memory_bytes=None):
    samples = tuple(state.get("samples") or ())
    per_trial_peak = {}
    per_trial_peak_uss = {}
    for sample in samples:
        for cell_key, facts in (sample.get("per_trial") or {}).items():
            per_trial_peak[cell_key] = max(
                int(per_trial_peak.get(cell_key, 0)),
                int(facts.get("tree_rss_bytes") or 0),
            )
            per_trial_peak_uss[cell_key] = max(
                int(per_trial_peak_uss.get(cell_key, 0)),
                int(facts.get("tree_uss_bytes") or 0),
            )
    available = [
        item.get("minimum_system_available_memory_bytes")
        for item in samples
        if item.get("minimum_system_available_memory_bytes") is not None
    ]
    cpu = [item.get("cpu_percent") for item in samples if item.get("cpu_percent") is not None]
    swap = [item.get("swap_used_bytes") for item in samples if item.get("swap_used_bytes") is not None]
    return {
        "sample_count": len(samples),
        "initial_available_memory_bytes": initial_available_memory_bytes,
        "minimum_available_memory_bytes": min(available) if available else None,
        "aggregate_peak_tree_rss_bytes": max(
            (int(item.get("aggregate_tree_rss_bytes") or 0) for item in samples),
            default=0,
        ),
        "aggregate_peak_tree_uss_bytes": max(
            (int(item.get("aggregate_tree_uss_bytes") or 0) for item in samples),
            default=0,
        ),
        "per_trial_peak_tree_rss_bytes": per_trial_peak,
        "per_trial_peak_tree_uss_bytes": per_trial_peak_uss,
        "max_cpu_percent": max(cpu, default=None),
        "mean_cpu_percent": (
            sum(cpu) / len(cpu) if cpu else None
        ),
        "logical_cpu_count": next(
            (item.get("logical_cpu_count") for item in reversed(samples)
             if item.get("logical_cpu_count") is not None),
            None,
        ),
        "initial_swap_used_bytes": min(swap) if swap else None,
        "peak_swap_used_bytes": max(swap) if swap else None,
        "swap_growth_bytes": (
            max(swap) - min(swap) if swap else None
        ),
    }


def qualify_parallel_concurrency(
    resource_summary,
    *,
    completed_payloads=(),
    reserve_bytes=PARALLEL_POLICY_MEMORY_RESERVE_BYTES,
):
    """Decide whether one more concurrent trial is safe from measured facts."""

    peak_by_trial = dict(
        resource_summary.get("per_trial_peak_tree_rss_bytes") or {}
    )
    largest_peak = max(peak_by_trial.values(), default=0)
    minimum_available = resource_summary.get("minimum_available_memory_bytes")
    projected_available = (
        minimum_available - largest_peak
        if minimum_available is not None and largest_peak
        else None
    )
    payloads = tuple(completed_payloads)
    statuses = [
        str(item.get("execution_status") or item.get("status") or "")
        for item in payloads
    ]
    cleanups = [
        (item.get("supervision") or {}).get("cleanup") or {}
        for item in payloads
    ]
    resource_limited = any(
        status in {
            "resource_guard_terminated",
            "hard_deadline_terminated",
            "parent_cancelled",
        }
        for status in statuses
    )
    cleanup_clean = bool(cleanups) and all(
        cleanup.get("descendants_clean") is True for cleanup in cleanups
    )
    swap_growth = resource_summary.get("swap_growth_bytes")
    swap_ok = swap_growth is None or swap_growth <= PARALLEL_POLICY_MAX_SWAP_GROWTH_BYTES
    logical_cpu_count = resource_summary.get("logical_cpu_count")
    max_cpu = resource_summary.get("max_cpu_percent")
    cpu_contention = bool(
        logical_cpu_count
        and max_cpu is not None
        and max_cpu >= 98.0
        and len(payloads) >= int(logical_cpu_count)
    )
    reasons = []
    if not largest_peak:
        reasons.append("no_per_trial_peak_observed")
    if projected_available is None or projected_available < reserve_bytes:
        reasons.append("projected_memory_reserve_below_2_gib")
    if resource_limited:
        reasons.append("resource_limited_or_abnormal_completion")
    if not cleanup_clean:
        reasons.append("child_cleanup_not_proven")
    if not swap_ok:
        reasons.append("pagefile_growth_exceeded_guard")
    if cpu_contention:
        reasons.append("cpu_contention_observed")
    return {
        "qualified": not reasons,
        "reasons": reasons,
        "largest_observed_trial_peak_rss_bytes": largest_peak,
        "minimum_observed_available_memory_bytes": minimum_available,
        "projected_available_after_one_more_trial_bytes": projected_available,
        "reserve_bytes": reserve_bytes,
        "resource_limited": resource_limited,
        "cleanup_clean": cleanup_clean,
        "swap_growth_bytes": swap_growth,
        "cpu_contention": cpu_contention,
    }


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
        fixed_cycle_request = None
        if policy == "fixed_cycle":
            fixed_cycle_request = fixed_cycle_control_request(
                profile=profile,
                benchmark_directory=benchmark_directory,
                input_fingerprint=scenario["input_fingerprint"],
                source_seed_fingerprint=scenario["source_seed_fingerprint"],
                cp_sat_random_seed=int(seed),
                total_time_limit_seconds=STARTUP_AWARE_TOTAL_POLICY_SECONDS,
                per_operator_time_limit_seconds=STARTUP_AWARE_MAX_OPERATOR_SECONDS,
                worker_count=STARTUP_AWARE_WORKER_COUNT,
                validation_time_limit_seconds=STARTUP_AWARE_VALIDATION_SECONDS,
                parent_hard_wall_seconds=STARTUP_AWARE_PARENT_HARD_WALL_SECONDS,
            )
            payload = run_supervised_calibration_trial(
                **fixed_cycle_request["run_kwargs"]
            )
        else:
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
        if fixed_cycle_request is not None:
            payload.update({
                "origin": "startup_aware_parent",
                "fixed_cycle_request": fixed_cycle_request["request"],
                "fixed_cycle_request_fingerprint": fixed_cycle_request[
                    "request_fingerprint"
                ],
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
        if policy == "fixed_cycle":
            fixed_cycle_request = fixed_cycle_control_request(
                profile=profile,
                benchmark_directory=benchmark_directory,
                input_fingerprint=scenario["input_fingerprint"],
                source_seed_fingerprint=scenario["source_seed_fingerprint"],
                cp_sat_random_seed=int(seed),
                total_time_limit_seconds=STARTUP_AWARE_TOTAL_POLICY_SECONDS,
                per_operator_time_limit_seconds=STARTUP_AWARE_MAX_OPERATOR_SECONDS,
                worker_count=STARTUP_AWARE_WORKER_COUNT,
                validation_time_limit_seconds=STARTUP_AWARE_VALIDATION_SECONDS,
                parent_hard_wall_seconds=STARTUP_AWARE_PARENT_HARD_WALL_SECONDS,
            )
            payload.update({
                "origin": "startup_aware_parent",
                "fixed_cycle_request": fixed_cycle_request["request"],
                "fixed_cycle_request_fingerprint": fixed_cycle_request[
                    "request_fingerprint"
                ],
            })

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


def _parallel_cell_key(cell):
    return f"{cell['scenario_id']}:{cell['policy']}:{int(cell['seed'])}"


def execute_parallel_policy_cell(
    *,
    manifest,
    scenario_id,
    policy,
    seed,
    worker_started_callback=None,
    cancel_requested=None,
):
    """Execute one new-study cell without writing shared study state.

    The function is deliberately persistence-free so it can run in parallel
    threads in the coordinator.  The actual CP-SAT work still happens in the
    existing supervised child process, and the parent remains the only owner
    of result/manifest publication.
    """

    if scenario_id not in manifest.get("scenarios", {}):
        raise ValueError(f"Unknown parallel-study scenario: {scenario_id}")
    if policy not in PARALLEL_POLICY_STUDY_POLICIES:
        raise ValueError(f"Unknown parallel-study policy: {policy}")
    if int(seed) not in PARALLEL_POLICY_STUDY_SEEDS:
        raise ValueError(f"Seed is not preregistered: {seed}")
    scenario = manifest["scenarios"][scenario_id]
    benchmark_directory = _REPOSITORY_ROOT / scenario["benchmark_directory"]
    started = perf_counter()
    try:
        fixed_cycle_request = None
        if policy == "fixed_cycle":
            fixed_cycle_request = fixed_cycle_control_request(
                profile=PARALLEL_POLICY_STUDY_PROFILE,
                benchmark_directory=benchmark_directory,
                input_fingerprint=scenario["input_fingerprint"],
                source_seed_fingerprint=scenario["source_seed_fingerprint"],
                cp_sat_random_seed=int(seed),
                total_time_limit_seconds=STARTUP_AWARE_TOTAL_POLICY_SECONDS,
                per_operator_time_limit_seconds=STARTUP_AWARE_MAX_OPERATOR_SECONDS,
                worker_count=PARALLEL_POLICY_STUDY_WORKER_COUNT,
                validation_time_limit_seconds=STARTUP_AWARE_VALIDATION_SECONDS,
                parent_hard_wall_seconds=STARTUP_AWARE_PARENT_HARD_WALL_SECONDS,
            )
            run_kwargs = dict(fixed_cycle_request["run_kwargs"])
        else:
            run_kwargs = {
                "policy": policy,
                "profile": PARALLEL_POLICY_STUDY_PROFILE,
                "benchmark_directory": benchmark_directory,
                "total_time_limit_seconds": STARTUP_AWARE_TOTAL_POLICY_SECONDS,
                "per_operator_time_limit_seconds": STARTUP_AWARE_MAX_OPERATOR_SECONDS,
                "worker_count": PARALLEL_POLICY_STUDY_WORKER_COUNT,
                "validation_time_limit_seconds": STARTUP_AWARE_VALIDATION_SECONDS,
                "hard_wall_seconds": STARTUP_AWARE_PARENT_HARD_WALL_SECONDS,
                "startup_aware": True,
                "cp_sat_random_seed": int(seed),
            }
        run_kwargs.update({
            "cancel_requested": cancel_requested,
            "worker_started_callback": worker_started_callback,
        })
        payload = run_supervised_calibration_trial(**run_kwargs)
        payload = dict(payload)
        payload.update({
            "schema": PARALLEL_POLICY_RESULT_SCHEMA,
            "study_id": manifest["study_id"],
            "scenario_id": scenario_id,
            "policy": policy,
            "seed": int(seed),
            "profile": PARALLEL_POLICY_STUDY_PROFILE,
            "adaptive_policy_variant": build_calibration_policy(policy)[
                "adaptive_policy_variant"
            ],
            "policy_fingerprint": manifest["policy_fingerprints"][policy],
            "source_lineage": {
                "input_fingerprint": scenario["input_fingerprint"],
                "source_seed_fingerprint": scenario["source_seed_fingerprint"],
            },
            "budget_contract": manifest["budget_contract"],
            "cell_elapsed_seconds": perf_counter() - started,
        })
        if fixed_cycle_request is not None:
            payload.update({
                "origin": "parallel_policy_study",
                "fixed_cycle_request": fixed_cycle_request["request"],
                "fixed_cycle_request_fingerprint": fixed_cycle_request[
                    "request_fingerprint"
                ],
            })
        return payload
    except Exception as error:
        payload = {
            "schema": PARALLEL_POLICY_RESULT_SCHEMA,
            "study_id": manifest["study_id"],
            "scenario_id": scenario_id,
            "policy": policy,
            "seed": int(seed),
            "profile": PARALLEL_POLICY_STUDY_PROFILE,
            "adaptive_policy_variant": build_calibration_policy(policy)[
                "adaptive_policy_variant"
            ],
            "policy_fingerprint": manifest["policy_fingerprints"][policy],
            "status": "source_or_runner_error",
            "error_type": type(error).__name__,
            "error": str(error),
            "source_lineage": {
                "input_fingerprint": scenario["input_fingerprint"],
                "source_seed_fingerprint": scenario["source_seed_fingerprint"],
            },
            "budget_contract": manifest["budget_contract"],
            "cell_elapsed_seconds": perf_counter() - started,
        }
        if policy == "fixed_cycle":
            fixed_cycle_request = fixed_cycle_control_request(
                profile=PARALLEL_POLICY_STUDY_PROFILE,
                benchmark_directory=benchmark_directory,
                input_fingerprint=scenario["input_fingerprint"],
                source_seed_fingerprint=scenario["source_seed_fingerprint"],
                cp_sat_random_seed=int(seed),
                total_time_limit_seconds=STARTUP_AWARE_TOTAL_POLICY_SECONDS,
                per_operator_time_limit_seconds=STARTUP_AWARE_MAX_OPERATOR_SECONDS,
                worker_count=PARALLEL_POLICY_STUDY_WORKER_COUNT,
                validation_time_limit_seconds=STARTUP_AWARE_VALIDATION_SECONDS,
                parent_hard_wall_seconds=STARTUP_AWARE_PARENT_HARD_WALL_SECONDS,
            )
            payload.update({
                "origin": "parallel_policy_study",
                "fixed_cycle_request": fixed_cycle_request["request"],
                "fixed_cycle_request_fingerprint": fixed_cycle_request[
                    "request_fingerprint"
                ],
            })
        return payload


def _persist_parallel_policy_result(study_directory, manifest, payload):
    """Publish one result and its hash; called only by the coordinator."""

    result_path = Path(study_directory) / "results" / _result_filename(
        payload["scenario_id"], payload["policy"], payload["seed"]
    )
    if result_path.exists():
        raise FileExistsError(f"Result artifact already exists: {result_path}")
    _json_write_atomic(result_path, payload)
    artifact_hash = _sha256_file(result_path)
    filename = result_path.name
    manifest["results"][filename] = {
        "path": _relative_path(result_path),
        "sha256": artifact_hash,
        "status": payload.get("execution_status") or payload.get("status"),
        "final_substantive_value": payload.get("final_substantive_value"),
        "candidate_complete": payload.get("candidate_complete"),
    }
    return {
        "filename": filename,
        "path": _relative_path(result_path),
        "sha256": artifact_hash,
    }


def run_parallel_policy_batch(
    *,
    manifest,
    max_parallel_trials=PARALLEL_POLICY_STUDY_DEFAULT_PARALLEL_TRIALS,
    cells,
    qualified_for_requested_parallelism=False,
    cancel_requested=None,
):
    """Run one parent-coordinated batch of independent policy cells."""

    max_parallel_trials = int(max_parallel_trials)
    if not 1 <= max_parallel_trials <= PARALLEL_POLICY_STUDY_MAX_PARALLEL_TRIALS:
        raise ValueError(
            "max_parallel_trials must be between 1 and "
            f"{PARALLEL_POLICY_STUDY_MAX_PARALLEL_TRIALS}"
        )
    if max_parallel_trials > PARALLEL_POLICY_STUDY_DEFAULT_PARALLEL_TRIALS and not qualified_for_requested_parallelism:
        raise ValueError(
            "parallelism above two requires a prior measured qualification"
        )
    cells = tuple(cells)
    if not cells:
        return {
            "payloads": (),
            "resource": {},
            "qualification": {
                "qualified": False,
                "reasons": ["empty_batch"],
            },
        }

    active_workers = {}
    active_lock = threading.Lock()
    monitor_stop = threading.Event()
    monitor_state = {"samples": []}
    initial_resource = _parallel_resource_snapshot({})
    initial_available = initial_resource.get("system_available_memory_bytes")
    monitor = threading.Thread(
        target=_parallel_resource_monitor,
        args=(active_workers, active_lock, monitor_stop, monitor_state),
        name="parallel-policy-resource-monitor",
        daemon=True,
    )
    monitor.start()
    cancellation = threading.Event()

    def _cancel_requested():
        if cancellation.is_set():
            return True
        if cancel_requested is None:
            return False
        try:
            return bool(cancel_requested())
        except Exception:
            return False

    def _run(cell):
        key = _parallel_cell_key(cell)

        def _started(pid):
            with active_lock:
                active_workers[key] = int(pid)

        try:
            return execute_parallel_policy_cell(
                manifest=manifest,
                scenario_id=cell["scenario_id"],
                policy=cell["policy"],
                seed=cell["seed"],
                worker_started_callback=_started,
                cancel_requested=_cancel_requested,
            )
        finally:
            with active_lock:
                active_workers.pop(key, None)

    results_by_key = {}
    with ThreadPoolExecutor(max_workers=min(max_parallel_trials, len(cells))) as executor:
        future_by_key = {
            executor.submit(_run, cell): _parallel_cell_key(cell)
            for cell in cells
        }
        for future in as_completed(future_by_key):
            key = future_by_key[future]
            try:
                results_by_key[key] = future.result()
            except Exception as error:  # pragma: no cover - defensive boundary
                cancellation.set()
                results_by_key[key] = {
                    "schema": PARALLEL_POLICY_RESULT_SCHEMA,
                    "study_id": manifest["study_id"],
                    "status": "parallel_coordinator_error",
                    "error_type": type(error).__name__,
                    "error": str(error),
                }
    monitor_stop.set()
    monitor.join(timeout=5.0)
    resource = _parallel_resource_summary(
        monitor_state,
        initial_available_memory_bytes=initial_available,
    )
    payloads = tuple(results_by_key[_parallel_cell_key(cell)] for cell in cells)
    qualification = qualify_parallel_concurrency(
        resource,
        completed_payloads=payloads,
    )
    for payload in payloads:
        payload["parallel_execution"] = {
            "batch_concurrency": max_parallel_trials,
            "parent_owned_manifest": True,
            "resource_qualification": qualification,
        }
    return {
        "payloads": payloads,
        "resource": resource,
        "qualification": qualification,
    }


def run_parallel_policy_study(
    study_directory,
    *,
    max_parallel_trials=PARALLEL_POLICY_STUDY_MAX_PARALLEL_TRIALS,
):
    """Run the four-policy matrix with measured 2 -> 3 -> 4 expansion."""

    max_parallel_trials = int(max_parallel_trials)
    if not 2 <= max_parallel_trials <= PARALLEL_POLICY_STUDY_MAX_PARALLEL_TRIALS:
        raise ValueError(
            "max_parallel_trials must be between 2 and "
            f"{PARALLEL_POLICY_STUDY_MAX_PARALLEL_TRIALS}"
        )
    study_directory = Path(study_directory)
    manifest_path = study_directory / "study_manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(
            "Initialize the parallel policy study before running it"
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != PARALLEL_POLICY_STUDY_SCHEMA:
        raise ValueError("Study manifest is not a parallel-policy study")
    pending = [
        {
            "scenario_id": scenario_id,
            "policy": policy,
            "seed": int(seed),
        }
        for scenario_id in manifest["scenario_ids"]
        for policy in manifest["policies"]
        for seed in manifest["seeds"]
        if _result_filename(scenario_id, policy, seed)
        not in manifest.get("results", {})
    ]
    current_parallelism = PARALLEL_POLICY_STUDY_DEFAULT_PARALLEL_TRIALS
    while pending:
        batch_cells = tuple(pending[:current_parallelism])
        qualified_levels = {
            int(item.get("completed_parallel_trials"))
            for item in manifest.get("concurrency_history") or ()
            if item.get("qualified_for_next_slot")
            and item.get("completed_parallel_trials") is not None
        }
        batch = run_parallel_policy_batch(
            manifest=manifest,
            max_parallel_trials=current_parallelism,
            cells=batch_cells,
            qualified_for_requested_parallelism=(
                current_parallelism <= 2
                or max(qualified_levels, default=0) >= current_parallelism - 1
            ),
        )
        persisted = []
        for payload in batch["payloads"]:
            persisted.append(_persist_parallel_policy_result(
                study_directory,
                manifest,
                payload,
            ))
        manifest["batches"].append({
            "batch_number": len(manifest["batches"]) + 1,
            "requested_parallel_trials": current_parallelism,
            "cells": list(batch_cells),
            "persisted_results": persisted,
            "resource": batch["resource"],
            "qualification": batch["qualification"],
        })
        manifest["concurrency_history"].append({
            "completed_parallel_trials": current_parallelism,
            "qualified_for_next_slot": bool(batch["qualification"]["qualified"]),
            "reasons": list(batch["qualification"].get("reasons") or ()),
        })
        _json_write_atomic(manifest_path, manifest)
        if (
            batch["qualification"].get("qualified")
            and current_parallelism < max_parallel_trials
        ):
            current_parallelism += 1
        elif (
            not batch["qualification"].get("qualified")
            and current_parallelism > PARALLEL_POLICY_STUDY_DEFAULT_PARALLEL_TRIALS
        ):
            current_parallelism -= 1
        pending = pending[len(batch_cells):]
    return manifest


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
        "adaptive_policy_variant": payload.get("adaptive_policy_variant"),
        "policy_fingerprint": payload.get("policy_fingerprint"),
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
        "parallel_execution": payload.get("parallel_execution"),
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
        values = [
            float(cell["final_substantive_value"])
            for cell in group
            if cell.get("final_substantive_value") is not None
        ]
        policy_summary[f"{scenario_id}:{policy}"] = {
            "scenario_id": scenario_id,
            "policy": policy,
            "seeds": [cell["seed"] for cell in group],
            "final_values": values,
            "median_final_value": statistics.median(values) if values else None,
            "best_final_value": min(values) if values else None,
            "worst_final_value": max(values) if values else None,
            "direct_gains": [
                float(cell["initial_substantive_value"])
                - float(cell["final_substantive_value"])
                for cell in group
                if cell.get("initial_substantive_value") is not None
                and cell.get("final_substantive_value") is not None
            ],
            "all_complete": bool(group) and all(
                cell.get("candidate_complete") is True for cell in group
            ),
            "all_unmet_free": bool(group) and all(
                cell.get("final_unmet_count") == 0 for cell in group
            ),
            "all_source_branches_revalidated": all(
                cell["branch_revalidated"] for cell in group
            ),
            "median_cell_seconds": statistics.median(
                float(cell["cell_seconds"]) for cell in group
                if cell.get("cell_seconds") is not None
            ) if any(cell.get("cell_seconds") is not None for cell in group) else None,
            "median_policy_seconds": statistics.median(
                float(cell["policy_seconds"]) for cell in group
                if cell.get("policy_seconds") is not None
            ) if any(cell.get("policy_seconds") is not None for cell in group) else None,
            "median_cp_sat_seconds": statistics.median(
                float(cell["cp_sat_seconds"]) for cell in group
                if cell.get("cp_sat_seconds") is not None
            ) if any(cell.get("cp_sat_seconds") is not None for cell in group) else None,
            "median_validation_seconds": statistics.median(
                float(cell["validation_seconds"]) for cell in group
                if cell.get("validation_seconds") is not None
            ) if any(cell.get("validation_seconds") is not None for cell in group) else None,
            "peak_working_set_bytes": max(
                int(cell["peak_tree_working_set_bytes"] or 0) for cell in group
            ),
        }

    scenario_winners = {}
    for scenario_id in manifest["scenario_ids"]:
        candidates = [
            item for key, item in policy_summary.items()
            if item["scenario_id"] == scenario_id
            and item["median_final_value"] is not None
        ]
        if candidates:
            winner = min(candidates, key=lambda item: item["median_final_value"])
            scenario_winners[scenario_id] = {
                "policy": winner["policy"],
                "median_final_value": winner["median_final_value"],
                "reason": "lowest median final substantive value in this diagnostic study",
            }

    is_parallel_study = manifest.get("study_kind") == (
        "progressive_parallel_policy_comparison"
    )
    return {
        "schema": (
            "student_assignment_parallel_policy_summary_v1"
            if is_parallel_study
            else "student_assignment_policy_generalization_summary_v1"
        ),
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
                (
                    "This progressive parallel study is diagnostic only; policy "
                    "conclusions require sequential confirmation."
                    if is_parallel_study
                    else "Fixed-cycle is the strongest diagnostic policy in the completed "
                    "startup-aware scenarios; this study does not wire it into production."
                )
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
        "--initialize-parallel",
        action="store_true",
        help="Create the four-policy progressive-parallel study manifest.",
    )
    parser.add_argument(
        "--run-parallel",
        action="store_true",
        help="Run the four-policy study with measured concurrency expansion.",
    )
    parser.add_argument(
        "--max-parallel-trials",
        type=int,
        default=PARALLEL_POLICY_STUDY_MAX_PARALLEL_TRIALS,
        help="Maximum qualified research trial slots (2 through 4).",
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
    selected_modes = sum(bool(value) for value in (
        args.initialize,
        args.summarize,
        args.initialize_parallel,
        args.run_parallel,
    ))
    if selected_modes > 1:
        parser.error("study action flags are mutually exclusive")
    if args.initialize:
        payload = initialize_startup_aware_study(args.study_directory)
    elif args.initialize_parallel:
        payload = initialize_parallel_policy_study(args.study_directory)
    elif args.run_parallel:
        payload = run_parallel_policy_study(
            args.study_directory,
            max_parallel_trials=args.max_parallel_trials,
        )
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
    if args.initialize or args.summarize or args.initialize_parallel or args.run_parallel:
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
    "PARALLEL_POLICY_MEMORY_RESERVE_BYTES",
    "PARALLEL_POLICY_MAX_SWAP_GROWTH_BYTES",
    "PARALLEL_POLICY_PROTOCOL_VERSION",
    "PARALLEL_POLICY_RESULT_SCHEMA",
    "PARALLEL_POLICY_STUDY_DEFAULT_PARALLEL_TRIALS",
    "PARALLEL_POLICY_STUDY_ID",
    "PARALLEL_POLICY_STUDY_MAX_PARALLEL_TRIALS",
    "PARALLEL_POLICY_STUDY_POLICIES",
    "PARALLEL_POLICY_STUDY_PROFILE",
    "PARALLEL_POLICY_STUDY_SCHEMA",
    "PARALLEL_POLICY_STUDY_SEEDS",
    "PARALLEL_POLICY_STUDY_WORKER_COUNT",
    "STARTUP_AWARE_POLICIES",
    "STARTUP_AWARE_PROFILE",
    "STARTUP_AWARE_RESULT_SCHEMA",
    "STARTUP_AWARE_SEEDS",
    "STARTUP_AWARE_STUDY_ID",
    "STARTUP_AWARE_STUDY_SCHEMA",
    "TARGET_SCENARIO_DIRECTORIES",
    "build_startup_aware_study_manifest",
    "build_parallel_policy_study_manifest",
    "execute_parallel_policy_cell",
    "initialize_startup_aware_study",
    "initialize_parallel_policy_study",
    "parallel_policy_budget_contract",
    "parallel_policy_fingerprint",
    "qualify_parallel_concurrency",
    "run_parallel_policy_batch",
    "run_parallel_policy_study",
    "run_startup_aware_policy_cell",
    "summarize_startup_aware_study",
    "startup_aware_policy_budget_contract",
    "write_startup_aware_study_summary",
]
