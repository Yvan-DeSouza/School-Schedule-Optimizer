"""Run one isolated Objective Semantics v2 operator-variance trial.

This is an offline diagnostic surface.  It consumes the durable detached
student-assignment benchmark and its Stage 1 source seed, validates that seed
through the existing full-model boundary, and runs one existing operator with
an explicitly controlled CP-SAT configuration.  It never persists a schedule,
mutates a benchmark checkpoint, or participates in production assignment.

The caller should launch one trial per clean process.  A trial's JSON output
contains the input and source fingerprints, target scope, CP-SAT controls, and
the existing complete-candidate/full-validation evidence needed to compare
transition distributions.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
from time import perf_counter

from .student_assignment.operator_characterization import (
    run_operator_characterization_trial,
)
from .student_assignment.calibration_supervisor import (
    CalibrationExecutionProfile,
    EXECUTION_COMPLETED,
    TARGET_SCALE_CALIBRATION_EXECUTION_PROFILE,
    supervise_json_worker,
)
from .student_assignment.runtime import semantic_student_assignment_input_fingerprint
from .student_assignment.stage2_benchmark import (
    read_durable_stage2_benchmark,
    validate_diagnostic_branch_checkpoint,
    write_diagnostic_branch_checkpoint,
)


VARIANCE_STUDY_SCHEMA = "student_assignment_adaptive_variance_trial_v1"
VARIANCE_STUDY_ID = "adaptive-policy-variance-v2-20260828"


def _write_worker_phase(path, phase, started, *, event="started", **facts):
    """Publish bounded progress for the parent watchdog."""

    if not path:
        return
    path = Path(path)
    try:
        previous = {}
        if path.exists():
            previous = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(previous, dict):
            previous = {}
        history = list(previous.get("phase_history") or [])
        history.append(
            {
                "phase": phase,
                "event": event,
                "elapsed_seconds": perf_counter() - started,
                **facts,
            }
        )
        payload = {
            "phase": phase,
            "event": event,
            "elapsed_seconds": perf_counter() - started,
            "phase_history": history[-256:],
            **facts,
        }
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.replace(path)
    except (OSError, TypeError, ValueError):
        # Progress is advisory.  A status-file failure must not alter the
        # diagnostic trial's solver or validation behavior.
        return


def _write_worker_output(path, payload):
    """Publish one complete worker payload atomically."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str),
        encoding="utf-8",
    )
    temporary.replace(path)


def run_variance_trial(
    benchmark_directory,
    *,
    operator,
    selected_student_ids,
    worker_count=1,
    cp_sat_random_seed=None,
    cp_sat_max_deterministic_time_seconds=None,
    total_time_limit_seconds=60.0,
    per_attempt_time_limit_seconds=30.0,
    validation_time_limit_seconds=None,
    validation_worker_count=1,
    collect_resource_telemetry=True,
    worker_status_path=None,
    capture_candidate_source_decisions=False,
):
    """Run one fixed-state, fixed-operator diagnostic transition."""

    started = perf_counter()
    _write_worker_phase(worker_status_path, "variance_trial", started)
    benchmark = read_durable_stage2_benchmark(benchmark_directory)
    data = benchmark["data"]
    manifest = benchmark["manifest"]
    input_fingerprint = semantic_student_assignment_input_fingerprint(data)
    source_decisions = tuple(benchmark["seed"]["seed_source_decisions"])
    source_fingerprint = manifest["seed_source_decision_fingerprint"]

    # Build a temporary transparent branch so the same branch reader and
    # current-model validator used by the established calibration workflow
    # prove the source incumbent before the operator transition begins.
    with tempfile.TemporaryDirectory(prefix="student-variance-") as directory:
        branch_path = Path(directory) / "incumbent.json.gz"
        stage1 = dict(manifest.get("stage1") or {})
        counts = dict(manifest.get("counts") or {})
        write_diagnostic_branch_checkpoint(
            branch_path,
            data=data,
            source_decisions=source_decisions,
            parent_source_decision_fingerprint=source_fingerprint,
            branch_id=f"{VARIANCE_STUDY_ID}-{operator}",
            provenance={
                "runner": "benchmark_adaptive_variance",
                "study_id": VARIANCE_STUDY_ID,
                "operator": operator,
            },
            objective_vector=stage1.get("objective_vector", ()),
            substantive_components=stage1.get("substantive_components", {}),
            validation={
                "full_model_validation": bool(stage1.get("validated")),
                "complete": bool(stage1.get("complete")),
                "assignment_count": stage1.get("source_decision_count"),
                "required_source_decision_group_count": counts.get(
                    "required_source_decision_group_count"
                ),
                "unmet_request_count": counts.get(
                    "unmet_required_request_count", 0
                ),
                "special_commitment_count": counts.get(
                    "special_commitment_count", 0
                ),
            },
        )
        preparation_started = perf_counter()
        checked = validate_diagnostic_branch_checkpoint(
            branch_path,
            data=data,
            time_limit_seconds=(
                float(validation_time_limit_seconds)
                if validation_time_limit_seconds is not None
                else max(30.0, float(data.time_limit_seconds))
            ),
            worker_count=int(validation_worker_count),
        )
        preparation_seconds = perf_counter() - preparation_started

        trial = run_operator_characterization_trial(
            data,
            initial_result=checked["result"],
            initial_source_decisions=source_decisions,
            benchmark_name=VARIANCE_STUDY_ID,
            operator=operator,
            selected_student_ids=tuple(selected_student_ids),
            target_policy="fixed",
            total_time_limit_seconds=float(total_time_limit_seconds),
            max_attempts=1,
            per_attempt_time_limit_seconds=float(per_attempt_time_limit_seconds),
            worker_count=int(worker_count),
            collect_resource_telemetry=bool(collect_resource_telemetry),
            hard_feasibility_validation_time_limit_seconds=(
                float(validation_time_limit_seconds)
                if validation_time_limit_seconds is not None
                else max(30.0, float(data.time_limit_seconds))
            ),
            hard_feasibility_validation_worker_count=int(validation_worker_count),
            cp_sat_random_seed=cp_sat_random_seed,
            cp_sat_max_deterministic_time_seconds=(
                cp_sat_max_deterministic_time_seconds
            ),
            capture_candidate_source_decisions=capture_candidate_source_decisions,
        )

    payload = trial.to_dict()
    payload.update(
        {
            "schema": VARIANCE_STUDY_SCHEMA,
            "study_id": VARIANCE_STUDY_ID,
            "benchmark_manifest_fingerprint": input_fingerprint,
            "source_seed_fingerprint": source_fingerprint,
            "selected_student_ids": list(selected_student_ids),
            "worker_count": int(worker_count),
            "total_time_limit_seconds": float(total_time_limit_seconds),
            "per_attempt_time_limit_seconds": float(
                per_attempt_time_limit_seconds
            ),
            "validation_time_limit_seconds": (
                float(validation_time_limit_seconds)
                if validation_time_limit_seconds is not None
                else max(30.0, float(data.time_limit_seconds))
            ),
            "validation_worker_count": int(validation_worker_count),
            "cp_sat_random_seed": (
                int(cp_sat_random_seed) if cp_sat_random_seed is not None else None
            ),
            "cp_sat_max_deterministic_time_seconds": (
                float(cp_sat_max_deterministic_time_seconds)
                if cp_sat_max_deterministic_time_seconds is not None
                else None
            ),
            "preparation_seconds": preparation_seconds,
            "total_process_operation_seconds": perf_counter() - started,
            "seed_validation": checked["validation"],
        }
    )
    _write_worker_phase(
        worker_status_path,
        "variance_trial",
        started,
        event="completed",
        candidate_found=bool(trial.candidate_found),
        candidate_validated=bool(trial.candidate_validated),
        candidate_adopted=bool(trial.candidate_adopted),
    )
    return payload


def _supervised_variance_termination_payload(
    *,
    benchmark,
    operator,
    selected_student_ids,
    worker_count,
    cp_sat_random_seed,
    cp_sat_max_deterministic_time_seconds,
    total_time_limit_seconds,
    per_attempt_time_limit_seconds,
    validation_time_limit_seconds,
    validation_worker_count,
    execution_profile,
    supervision,
    preparation_seconds,
):
    """Return only retained-incumbent facts after worker termination."""

    manifest = benchmark["manifest"]
    counts = dict(manifest.get("counts") or {})
    stage1 = dict(manifest.get("stage1") or {})
    initial_value = stage1.get("substantive_value")
    if initial_value is None:
        initial_value = stage1.get("substantive_objective_value")
    return {
        "schema": VARIANCE_STUDY_SCHEMA,
        "study_id": VARIANCE_STUDY_ID,
        "benchmark_manifest_fingerprint": semantic_student_assignment_input_fingerprint(
            benchmark["data"]
        ),
        "source_seed_fingerprint": manifest.get("seed_source_decision_fingerprint"),
        "selected_student_ids": list(selected_student_ids),
        "operator": operator,
        "worker_count": int(worker_count),
        "cp_sat_random_seed": (
            int(cp_sat_random_seed) if cp_sat_random_seed is not None else None
        ),
        "cp_sat_max_deterministic_time_seconds": (
            float(cp_sat_max_deterministic_time_seconds)
            if cp_sat_max_deterministic_time_seconds is not None
            else None
        ),
        "total_time_limit_seconds": float(total_time_limit_seconds),
        "per_attempt_time_limit_seconds": float(per_attempt_time_limit_seconds),
        "validation_time_limit_seconds": (
            float(validation_time_limit_seconds)
            if validation_time_limit_seconds is not None
            else max(30.0, float(benchmark["data"].time_limit_seconds))
        ),
        "validation_worker_count": int(validation_worker_count),
        "execution_status": supervision.execution_status,
        "hard_wall_seconds": float(execution_profile.hard_wall_seconds),
        "termination_grace_seconds": float(
            execution_profile.termination_grace_seconds
        ),
        "initial_substantive_value": initial_value,
        "final_substantive_value": initial_value,
        "final_assignment_count": counts.get("assignment_count"),
        "final_unmet_count": counts.get("unmet_required_request_count", 0),
        "final_special_commitment_count": counts.get("special_commitment_count", 0),
        "candidate_found": False,
        "candidate_validated": False,
        "candidate_adopted": False,
        "candidate_diagnostic": "not_authoritative_after_worker_termination",
        "preparation_seconds": float(preparation_seconds),
        "output_protocol_complete": True,
        "supervision": supervision.to_dict(),
    }


def run_supervised_variance_trial(
    benchmark_directory,
    *,
    operator,
    selected_student_ids,
    worker_count=1,
    cp_sat_random_seed=None,
    cp_sat_max_deterministic_time_seconds=None,
    total_time_limit_seconds=60.0,
    per_attempt_time_limit_seconds=30.0,
    validation_time_limit_seconds=None,
    validation_worker_count=1,
    collect_resource_telemetry=True,
    hard_wall_seconds=None,
    termination_grace_seconds=5.0,
    max_process_tree_rss_bytes=None,
    min_system_available_memory_bytes=None,
    poll_interval_seconds=0.25,
    capture_candidate_source_decisions=False,
):
    """Run one variance trial under the repository's hard watchdog.

    The direct variance runner remains useful for short local probes.  Target
    scale trials use this boundary so model construction, CP-SAT, and full
    validation are all covered by one parent-side deadline.  A worker killed
    by that deadline cannot publish an authoritative candidate.
    """

    started = perf_counter()
    benchmark = read_durable_stage2_benchmark(benchmark_directory)
    data = benchmark["data"]
    if hard_wall_seconds is None:
        hard_wall_seconds = total_time_limit_seconds
    defaults = TARGET_SCALE_CALIBRATION_EXECUTION_PROFILE
    profile = CalibrationExecutionProfile(
        hard_wall_seconds=float(hard_wall_seconds),
        termination_grace_seconds=float(termination_grace_seconds),
        max_process_tree_rss_bytes=(
            defaults.max_process_tree_rss_bytes
            if max_process_tree_rss_bytes is None
            else int(max_process_tree_rss_bytes)
        ),
        min_system_available_memory_bytes=(
            defaults.min_system_available_memory_bytes
            if min_system_available_memory_bytes is None
            else int(min_system_available_memory_bytes)
        ),
        poll_interval_seconds=float(poll_interval_seconds),
    )
    with tempfile.TemporaryDirectory(
        prefix="student-supervised-variance-"
    ) as directory:
        directory = Path(directory)
        output_path = directory / "result.json"
        status_path = directory / "status.json"
        command = [
            sys.executable,
            "-m",
            "scheduling_engine.benchmark_adaptive_variance",
            "--worker",
            "--benchmark-directory",
            str(Path(benchmark_directory).resolve()),
            "--operator",
            operator,
            "--selected-student-ids",
            *[str(int(student_id)) for student_id in selected_student_ids],
            "--workers",
            str(int(worker_count)),
            "--total-seconds",
            str(float(total_time_limit_seconds)),
            "--per-attempt-seconds",
            str(float(per_attempt_time_limit_seconds)),
            "--validation-workers",
            str(int(validation_worker_count)),
            "--worker-output",
            str(output_path),
            "--worker-status",
            str(status_path),
        ]
        if cp_sat_random_seed is not None:
            command.extend(["--cp-sat-random-seed", str(int(cp_sat_random_seed))])
        if cp_sat_max_deterministic_time_seconds is not None:
            command.extend(
                [
                    "--cp-sat-max-deterministic-time-seconds",
                    str(float(cp_sat_max_deterministic_time_seconds)),
                ]
            )
        if validation_time_limit_seconds is not None:
            command.extend(["--validation-seconds", str(float(validation_time_limit_seconds))])
        if not collect_resource_telemetry:
            command.append("--no-resource-telemetry")
        if capture_candidate_source_decisions:
            command.append("--capture-candidate-source-decisions")
        supervision = supervise_json_worker(
            command,
            output_path=output_path,
            status_path=status_path,
            execution_profile=profile,
            cwd=str(Path.cwd()),
        )
    if supervision.execution_status == EXECUTION_COMPLETED:
        payload = dict(supervision.payload or {})
        payload["execution_status"] = EXECUTION_COMPLETED
        payload["hard_wall_seconds"] = float(profile.hard_wall_seconds)
        payload["termination_grace_seconds"] = float(
            profile.termination_grace_seconds
        )
        payload["supervision"] = supervision.to_dict()
        payload["parent_elapsed_seconds"] = perf_counter() - started
        return payload
    return _supervised_variance_termination_payload(
        benchmark=benchmark,
        operator=operator,
        selected_student_ids=selected_student_ids,
        worker_count=worker_count,
        cp_sat_random_seed=cp_sat_random_seed,
        cp_sat_max_deterministic_time_seconds=(
            cp_sat_max_deterministic_time_seconds
        ),
        total_time_limit_seconds=total_time_limit_seconds,
        per_attempt_time_limit_seconds=per_attempt_time_limit_seconds,
        validation_time_limit_seconds=validation_time_limit_seconds,
        validation_worker_count=validation_worker_count,
        execution_profile=profile,
        supervision=supervision,
        preparation_seconds=perf_counter() - started,
    )


def main(argv=None):  # pragma: no cover - offline experiment entry point
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark-directory", type=Path, required=True)
    parser.add_argument("--operator", required=True)
    parser.add_argument("--selected-student-ids", type=int, nargs="+", required=True)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--cp-sat-random-seed", type=int)
    parser.add_argument("--cp-sat-max-deterministic-time-seconds", type=float)
    parser.add_argument("--total-seconds", type=float, default=60.0)
    parser.add_argument("--per-attempt-seconds", type=float, default=30.0)
    parser.add_argument("--validation-seconds", type=float)
    parser.add_argument("--validation-workers", type=int, default=1)
    parser.add_argument("--no-resource-telemetry", action="store_true")
    parser.add_argument(
        "--supervised",
        action="store_true",
        help="run the trial under the hard parent-side watchdog",
    )
    parser.add_argument("--hard-wall-seconds", type=float)
    parser.add_argument("--termination-grace-seconds", type=float, default=5.0)
    parser.add_argument("--max-process-tree-rss-bytes", type=int)
    parser.add_argument("--min-system-available-memory-bytes", type=int)
    parser.add_argument("--poll-interval-seconds", type=float, default=0.25)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--worker-output", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--worker-status", type=Path, help=argparse.SUPPRESS)
    parser.add_argument(
        "--capture-candidate-source-decisions",
        action="store_true",
        help="include raw source decisions for one diagnostic branch replay",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    trial_kwargs = dict(
        benchmark_directory=args.benchmark_directory,
        operator=args.operator,
        selected_student_ids=args.selected_student_ids,
        worker_count=args.workers,
        cp_sat_random_seed=args.cp_sat_random_seed,
        cp_sat_max_deterministic_time_seconds=(
            args.cp_sat_max_deterministic_time_seconds
        ),
        total_time_limit_seconds=args.total_seconds,
        per_attempt_time_limit_seconds=args.per_attempt_seconds,
        validation_time_limit_seconds=args.validation_seconds,
        validation_worker_count=args.validation_workers,
        collect_resource_telemetry=not args.no_resource_telemetry,
        worker_status_path=args.worker_status if args.worker else None,
        capture_candidate_source_decisions=args.capture_candidate_source_decisions,
    )
    if args.worker:
        if not args.worker_output or not args.worker_status:
            raise ValueError("worker mode requires worker output/status paths")
        worker_started = perf_counter()
        payload = run_variance_trial(**trial_kwargs)
        payload["output_protocol_complete"] = True
        _write_worker_phase(
            args.worker_status,
            "serialization",
            worker_started,
            event="completed",
        )
        _write_worker_output(args.worker_output, payload)
        return 0
    if args.supervised and not args.worker:
        payload = run_supervised_variance_trial(
            args.benchmark_directory,
            operator=args.operator,
            selected_student_ids=args.selected_student_ids,
            worker_count=args.workers,
            cp_sat_random_seed=args.cp_sat_random_seed,
            cp_sat_max_deterministic_time_seconds=(
                args.cp_sat_max_deterministic_time_seconds
            ),
            total_time_limit_seconds=args.total_seconds,
            per_attempt_time_limit_seconds=args.per_attempt_seconds,
            validation_time_limit_seconds=args.validation_seconds,
            validation_worker_count=args.validation_workers,
            collect_resource_telemetry=not args.no_resource_telemetry,
            hard_wall_seconds=args.hard_wall_seconds,
            termination_grace_seconds=args.termination_grace_seconds,
            max_process_tree_rss_bytes=args.max_process_tree_rss_bytes,
            min_system_available_memory_bytes=(
                args.min_system_available_memory_bytes
            ),
            poll_interval_seconds=args.poll_interval_seconds,
            capture_candidate_source_decisions=args.capture_candidate_source_decisions,
        )
    else:
        payload = run_variance_trial(**trial_kwargs)
    encoded = json.dumps(payload, indent=2, sort_keys=True, default=str)
    if args.output:
        args.output.write_text(encoded + "\n", encoding="utf-8")
    else:
        print(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
