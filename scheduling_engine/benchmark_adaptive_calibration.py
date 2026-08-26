"""Clean-process runner for matched Objective Semantics v2 calibration trials.

This module is an offline diagnostic surface.  It prepares one complete
CP-SAT-validated incumbent, verifies a transparent detached branch, and then
delegates policy execution to the existing adaptive runtime.  It does not
participate in ordinary student assignment or persist application state.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
from time import perf_counter

from .realistic_student_assignment_validation import (
    build_mixed_grade_v2_fixture,
)
from .student_assignment.adaptive_calibration import (
    CALIBRATION_PROFILES,
    apply_calibration_profile,
    run_matched_calibration_trial,
)
from .student_assignment.core import (
    run_student_assignment_stage2_diagnostic,
)
from .student_assignment.runtime import semantic_student_assignment_input_fingerprint
from .student_assignment.stage2_benchmark import (
    read_diagnostic_branch_checkpoint,
    read_durable_stage2_benchmark,
    semantic_stage1_seed_source_fingerprint,
    validate_diagnostic_branch_checkpoint,
    write_diagnostic_branch_checkpoint,
)
from .student_assignment.quality import evaluate_student_assignment_quality


def _source_decisions_from_result(result):
    return tuple(
        (result.optimization_facts or {}).get("stage_2", {}).get(
            "final_source_decisions", ()
        )
    )


def _prepare_generated_incumbent(
    data,
    *,
    initial_time_limit_seconds,
    initial_worker_count,
):
    """Create one bounded complete incumbent for a generated medium fixture."""

    data = replace(data, time_limit_seconds=float(initial_time_limit_seconds))
    result = run_student_assignment_stage2_diagnostic(
        data,
        total_time_limit_seconds=float(initial_time_limit_seconds),
        hard_feasibility_time_limit_seconds=float(initial_time_limit_seconds),
        hard_feasibility_validation_time_limit_seconds=(
            float(initial_time_limit_seconds)
        ),
        hard_feasibility_worker_count=int(initial_worker_count),
        hard_feasibility_validation_worker_count=1,
        optimization_worker_count=int(initial_worker_count),
        capture_final_source_decisions=True,
        collect_resource_telemetry=False,
    )
    if result.status != "complete" or result.unmet_requests:
        raise RuntimeError(
            "The generated calibration fixture did not produce a complete "
            f"incumbent: status={result.status}, "
            f"solver_outcome={result.solver_outcome}, "
            f"unmet={len(result.unmet_requests)}."
        )
    source_decisions = _source_decisions_from_result(result)
    if not source_decisions:
        raise RuntimeError("The initial calibration result has no source decisions")
    return data, result, source_decisions


def _prepare_detached_incumbent(directory, *, profile):
    """Load a durable input/seed pair for the shared branch gate."""

    benchmark = read_durable_stage2_benchmark(directory)
    data = benchmark["data"]
    source_decisions = tuple(benchmark["seed"]["seed_source_decisions"])
    data = replace(data, objective_semantics_version="v2")
    data = apply_calibration_profile(data, profile)
    return data, source_decisions, benchmark["manifest"]


def _branch_validation_facts(result):
    facts = dict(result.optimization_facts or {})
    stage_1 = dict(facts.get("stage_1") or {})
    stage_2 = dict((result.optimization_facts or {}).get("stage_2") or {})
    return {
        "full_model_validation": bool(
            stage_2.get("alternate_seed_validated")
            or stage_1.get("seed_validated_against_full_model")
        ),
        "complete": result.status == "complete",
        "assignment_count": len(result.assignments),
        "required_source_decision_group_count": (
            stage_1.get("required_decision_group_count")
        ),
        "unmet_request_count": len(result.unmet_requests),
        "special_commitment_count": len(result.commitment_assignments),
    }


def run_calibration_trial(
    *,
    policy,
    profile="balanced",
    students=120,
    benchmark_directory=None,
    initial_time_limit_seconds=5.0,
    initial_worker_count=2,
    total_time_limit_seconds=60.0,
    per_operator_time_limit_seconds=60.0,
    worker_count=2,
    branch_output=None,
):
    """Prepare, branch-validate, and execute one clean-process trial."""

    if profile not in CALIBRATION_PROFILES:
        raise ValueError(f"Unknown calibration profile: {profile}")
    started = perf_counter()
    manifest = None
    if benchmark_directory:
        data, source_decisions, manifest = (
            _prepare_detached_incumbent(benchmark_directory, profile=profile)
        )
    else:
        data = apply_calibration_profile(
            build_mixed_grade_v2_fixture(student_count=int(students)),
            profile,
        )
        data, initial_result, source_decisions = _prepare_generated_incumbent(
            data,
            initial_time_limit_seconds=initial_time_limit_seconds,
            initial_worker_count=initial_worker_count,
        )

    if manifest is not None:
        stage1 = dict(manifest.get("stage1") or {})
        quality = {}
        validation = {
            "full_model_validation": bool(stage1.get("validated")),
            "complete": bool(stage1.get("complete")),
            "assignment_count": stage1.get("source_decision_count"),
            "required_source_decision_group_count": (
                manifest.get("counts", {}).get("required_source_decision_group_count")
            ),
            "unmet_request_count": manifest.get("counts", {}).get(
                "unmet_required_request_count", 0
            ),
            "special_commitment_count": manifest.get("counts", {}).get(
                "special_commitment_count", 0
            ),
        }
        initial_result = None
    else:
        quality = evaluate_student_assignment_quality(
            data,
            assignments=initial_result.assignments,
            commitment_assignments=initial_result.commitment_assignments,
            solver_objective_components=initial_result.objective_components,
        )
        validation = _branch_validation_facts(initial_result)
    branch_path = Path(branch_output) if branch_output else None
    with tempfile.TemporaryDirectory(prefix="student-calibration-") as temp_dir:
        path = branch_path or (Path(temp_dir) / "incumbent.json.gz")
        write_diagnostic_branch_checkpoint(
            path,
            data=data,
            source_decisions=source_decisions,
            parent_source_decision_fingerprint=(
                semantic_stage1_seed_source_fingerprint(data, source_decisions)
            ),
            branch_id=f"{policy}-{profile}",
            provenance={
                "runner": "benchmark_adaptive_calibration",
                "policy": policy,
                "profile": profile,
            },
            objective_vector=(
                manifest.get("stage1", {}).get("objective_vector", ())
                if manifest is not None
                else (
                    (initial_result.optimization_facts or {})
                    .get("stage_2", {})
                    .get("objective_values", ())
                )
            ),
            substantive_components=(
                dict(manifest.get("stage1", {}).get("substantive_components", {}))
                if manifest is not None
                else dict(initial_result.objective_components or {})
            ),
            quality=quality,
            validation=validation,
        )
        checked = validate_diagnostic_branch_checkpoint(
            path,
            data=data,
            time_limit_seconds=max(30.0, float(data.time_limit_seconds)),
            worker_count=1,
        )
        branch = read_diagnostic_branch_checkpoint(path, data=data)
        initial_result = checked["result"]
        trial = run_matched_calibration_trial(
            data,
            initial_result=checked["result"],
            initial_source_decisions=branch["source_decisions"],
            policy=policy,
            profile=profile,
            total_time_limit_seconds=total_time_limit_seconds,
            per_operator_time_limit_seconds=per_operator_time_limit_seconds,
            worker_count=worker_count,
            hard_feasibility_validation_time_limit_seconds=max(
                30.0, float(data.time_limit_seconds)
            ),
            hard_feasibility_validation_worker_count=1,
        )

    payload = trial.to_dict()
    payload["preparation"] = {
        "elapsed_seconds": perf_counter() - started,
        "input_fingerprint": semantic_student_assignment_input_fingerprint(data),
        "initial_status": initial_result.status,
        "initial_solver_outcome": initial_result.solver_outcome,
        "initial_assignment_count": len(initial_result.assignments),
        "initial_unmet_count": len(initial_result.unmet_requests),
        "branch_revalidated": True,
        "branch_validation": checked["validation"],
        "durable_manifest": (
            {
                "input_semantic_fingerprint": manifest.get(
                    "input_semantic_fingerprint"
                ),
                "seed_source_decision_fingerprint": manifest.get(
                    "seed_source_decision_fingerprint"
                ),
            }
            if manifest is not None
            else None
        ),
    }
    return payload


def main(argv=None):  # pragma: no cover - clean-process experiment surface
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--policy",
        choices=(
            "adaptive",
            "stateless_role",
            "r2_only",
            "student_repair_only",
            "utilization_only",
            "fixed_cycle",
        ),
        default="adaptive",
    )
    parser.add_argument("--profile", choices=tuple(CALIBRATION_PROFILES), default="balanced")
    parser.add_argument("--students", type=int, default=120)
    parser.add_argument("--benchmark-directory", type=Path)
    parser.add_argument("--initial-seconds", type=float, default=5.0)
    parser.add_argument("--initial-workers", type=int, default=2)
    parser.add_argument("--total-seconds", type=float, default=60.0)
    parser.add_argument("--per-operator-seconds", type=float, default=60.0)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--branch-output", type=Path)
    args = parser.parse_args(argv)
    payload = run_calibration_trial(
        policy=args.policy,
        profile=args.profile,
        students=args.students,
        benchmark_directory=args.benchmark_directory,
        initial_time_limit_seconds=args.initial_seconds,
        initial_worker_count=args.initial_workers,
        total_time_limit_seconds=args.total_seconds,
        per_operator_time_limit_seconds=args.per_operator_seconds,
        worker_count=args.workers,
        branch_output=args.branch_output,
    )
    json.dump(payload, sys.stdout, indent=2, sort_keys=True, default=str)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
