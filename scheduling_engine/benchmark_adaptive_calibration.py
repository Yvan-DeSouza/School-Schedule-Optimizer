"""Clean-process runner for matched Objective Semantics v2 calibration trials.

This module is an offline diagnostic surface.  It prepares one complete
CP-SAT-validated incumbent, verifies a transparent detached branch, and then
delegates policy execution to the existing adaptive runtime.  It does not
participate in ordinary student assignment or persist application state.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import tempfile
from time import perf_counter
from types import SimpleNamespace

from .realistic_student_assignment_validation import (
    build_mixed_grade_v2_fixture,
)
from .student_assignment.adaptive_calibration import (
    CALIBRATION_PROFILES,
    STARTUP_AWARE_SESSION_OVERRIDES,
    apply_calibration_profile,
    profile_fingerprint,
    run_matched_calibration_trial,
)
from .student_assignment.core import (
    run_student_assignment_stage2_diagnostic,
)
from .dto import (
    StudentAssignmentDTO,
    StudentAssignmentUnmetRequestDTO,
    StudentScheduleCommitmentAssignmentDTO,
)
from .student_assignment.runtime import semantic_student_assignment_input_fingerprint
from .student_assignment.stage2_benchmark import (
    read_diagnostic_branch_checkpoint,
    read_durable_stage2_benchmark,
    semantic_stage1_seed_source_fingerprint,
    validate_diagnostic_branch_checkpoint,
    write_diagnostic_branch_checkpoint,
)
from .student_assignment.calibration_supervisor import (
    CalibrationExecutionProfile,
    EXECUTION_COMPLETED,
    TARGET_SCALE_CALIBRATION_EXECUTION_PROFILE,
    supervise_json_worker,
)
from .student_assignment.quality import evaluate_student_assignment_quality


CALIBRATION_SUPERVISED_SCHEMA = "student_assignment_adaptive_calibration_trial_v2"
CALIBRATION_SUPERVISED_PROTOCOL_VERSION = "adaptive-calibration-v2"
# Keep the detached policy runner's validation boundary aligned with the
# standalone operator-characterization trials.  The production-shaped input
# carries a 20-second engine default, which is sufficient for ordinary work
# but was too short and inconsistent for the repeated full-model validation
# used by this offline policy study.
TARGET_SCALE_CALIBRATION_VALIDATION_TIME_LIMIT_SECONDS = 60.0


def _calibration_validation_time_limit(data, override=None):
    return max(
        TARGET_SCALE_CALIBRATION_VALIDATION_TIME_LIMIT_SECONDS
        if override is None
        else float(override),
        float(data.time_limit_seconds),
    )


def _source_decisions_from_result(result):
    return tuple(
        (result.optimization_facts or {}).get("stage_2", {}).get(
            "final_source_decisions", ()
        )
    )


def _tuple_tree(value):
    """Normalize JSON-decoded source-decision trees for branch writing."""

    if isinstance(value, (list, tuple)):
        return tuple(_tuple_tree(item) for item in value)
    return value


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


def _calibration_weighted_value(result, data):
    """Return the profile-valued substantive total for a prepared incumbent."""

    quality = evaluate_student_assignment_quality(
        data,
        assignments=result.assignments,
        commitment_assignments=result.commitment_assignments,
        solver_objective_components=result.objective_components,
    )
    components = quality.get("objective_semantics", {}).get("components", {})
    weighted = [
        value.get("weighted_normalized_contribution")
        for value in components.values()
        if isinstance(value, dict)
        and value.get("weighted_normalized_contribution") is not None
    ]
    if weighted:
        return float(sum(float(value or 0) for value in weighted)), quality
    return float(
        sum(
            float((result.objective_components or {}).get(name, 0) or 0)
            for name in (
                "section_utilization_balance_penalty",
                "student_semester_balance_penalty",
                "difficulty_balance_penalty",
                "course_category_diversity_penalty",
            )
        )
    ), quality


def _write_worker_phase(path, phase, started, **facts):
    if not path:
        return
    payload = {
        "phase": str(phase),
        "elapsed_seconds": perf_counter() - started,
        **facts,
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Keep a bounded breadcrumb history in addition to the latest phase.  A
    # hard-wall termination can happen between two callbacks, so the latest
    # phase alone is insufficient to distinguish a slow model-build substep
    # from a slow native solve.  This is observational telemetry only and is
    # intentionally capped so status writes cannot grow with session length.
    phase_history = []
    if path.exists():
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(previous, dict):
                phase_history = list(previous.get("phase_history") or ())
        except (OSError, ValueError, TypeError):
            phase_history = []
    phase_history.append(dict(payload))
    payload["phase_history"] = phase_history[-256:]
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    temporary.replace(path)


def _write_worker_output(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    temporary.replace(path)


def _write_prepared_incumbent(path, *, data, branch, result):
    """Serialize parent-validated facts for one isolated policy worker.

    The parent remains the authority for the full-model validation.  The
    worker receives only the immutable result facts needed by policy quality
    evaluation, plus both semantic fingerprints, so it does not repeat the
    same CP-SAT validation before its hard policy clock begins.
    """

    stage_1 = dict((result.optimization_facts or {}).get("stage_1") or {})
    stage_2 = dict((result.optimization_facts or {}).get("stage_2") or {})
    stage_1_objective_vector = (
        stage_1.get("objective_vector")
        or stage_1.get("objective_values")
        or branch.get("objective_vector")
        or ()
    )
    payload = {
        "schema": "student_assignment_calibration_prepared_incumbent_v1",
        "input_fingerprint": semantic_student_assignment_input_fingerprint(data),
        "source_fingerprint": branch["source_decision_fingerprint"],
        "validation": {
            "full_model_validation": bool(
                result.status == "complete" and not result.unmet_requests
            ),
            "complete": result.status == "complete",
            "unmet_request_count": len(result.unmet_requests),
        },
        "result": {
            "status": result.status,
            "solver_outcome": result.solver_outcome,
            "assignments": [asdict(item) for item in result.assignments],
            "unmet_requests": [asdict(item) for item in result.unmet_requests],
            "commitment_assignments": [
                asdict(item) for item in result.commitment_assignments
            ],
            "objective_components": dict(result.objective_components or {}),
            "stage_1_objective_vector": list(stage_1_objective_vector),
            "stage_2_objective_vector": list(
                stage_2.get("objective_values") or ()
            ),
            "source_decisions": [
                [list(key), list(value)]
                for key, value in branch["source_decisions"]
            ],
        },
    }
    _write_worker_output(path, payload)
    return payload


def _read_prepared_incumbent(path, *, data, branch):
    """Rehydrate facts already validated by the supervising parent."""

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("schema") != "student_assignment_calibration_prepared_incumbent_v1":
        raise ValueError("Unsupported prepared incumbent schema")
    if payload.get("input_fingerprint") != semantic_student_assignment_input_fingerprint(data):
        raise ValueError("Prepared incumbent input fingerprint does not match")
    if payload.get("source_fingerprint") != branch["source_decision_fingerprint"]:
        raise ValueError("Prepared incumbent source fingerprint does not match")
    validation = payload.get("validation") or {}
    if (
        not validation.get("full_model_validation")
        or not validation.get("complete")
        or int(validation.get("unmet_request_count", 0) or 0) != 0
    ):
        raise ValueError("Prepared incumbent is not a complete validated result")
    result_payload = payload.get("result") or {}
    source_decisions = tuple(
        (_tuple_tree(item[0]), _tuple_tree(item[1]))
        for item in result_payload.get("source_decisions", ())
    )
    if source_decisions != tuple(branch["source_decisions"]):
        raise ValueError("Prepared incumbent source decisions do not match branch")
    return SimpleNamespace(
        status=result_payload["status"],
        solver_outcome=result_payload["solver_outcome"],
        assignments=tuple(
            StudentAssignmentDTO(**item)
            for item in result_payload.get("assignments", ())
        ),
        unmet_requests=tuple(
            StudentAssignmentUnmetRequestDTO(**item)
            for item in result_payload.get("unmet_requests", ())
        ),
        commitment_assignments=tuple(
            StudentScheduleCommitmentAssignmentDTO(**item)
            for item in result_payload.get("commitment_assignments", ())
        ),
        objective_components=dict(result_payload.get("objective_components") or {}),
        optimization_facts={
            "stage_1": {
                "objective_vector": tuple(
                    result_payload.get("stage_1_objective_vector") or ()
                )
            },
            "stage_2": {
                "objective_values": tuple(
                    result_payload.get("stage_2_objective_vector") or ()
                ),
                "final_source_decisions": source_decisions,
            },
        },
    )


def _write_validated_supervised_branch(
    path,
    *,
    data,
    parent_branch,
    payload,
    policy,
    profile,
):
    """Persist only a strict, worker-validated derived incumbent."""

    if not path or payload.get("execution_status") != EXECUTION_COMPLETED:
        return None
    if not payload.get("candidate_complete"):
        return None
    if float(payload.get("final_substantive_value", 0.0) or 0.0) >= float(
        payload.get("initial_substantive_value", 0.0) or 0.0
    ):
        return None
    decisions = payload.get("final_source_decisions") or ()
    if not decisions:
        return None
    source_decisions = tuple(
        (tuple(key), tuple(value)) for key, value in decisions
    )
    branch_path = Path(path)
    branch_payload = write_diagnostic_branch_checkpoint(
        branch_path,
        data=data,
        source_decisions=source_decisions,
        parent_source_decision_fingerprint=parent_branch[
            "source_decision_fingerprint"
        ],
        branch_id=f"supervised-{policy}-{profile}-derived",
        provenance={
            "runner": "benchmark_adaptive_calibration_supervisor",
            "policy": policy,
            "profile": profile,
            "parent_source_decision_fingerprint": parent_branch[
                "source_decision_fingerprint"
            ],
        },
        objective_vector=tuple(payload.get("final_objective_vector") or ()),
        substantive_components=dict(payload.get("final_components") or {}),
        validation={
            "full_model_validation": True,
            "complete": True,
            "assignment_count": payload.get("final_assignment_count"),
            "unmet_request_count": payload.get("final_unmet_count", 0),
            "special_commitment_count": payload.get(
                "final_special_commitment_count", 0
            ),
        },
    )
    payload["derived_branch"] = {
        "path": str(branch_path),
        "source_fingerprint": branch_payload["source_decision_fingerprint"],
        "parent_source_fingerprint": parent_branch[
            "source_decision_fingerprint"
        ],
        "full_model_validated": True,
    }
    return payload["derived_branch"]


def _write_immediate_worker_branch(
    path,
    *,
    data,
    parent_branch,
    facts,
    policy,
    profile,
):
    """Persist a worker-validated improvement before policy finalization.

    A supervised worker may be stopped after a candidate passes the existing
    full-model validator but before the outer adaptive policy returns its
    final JSON record.  The branch is diagnostic-only and remains subject to
    parent-side revalidation before reuse; writing it here prevents a valid
    intermediate state from disappearing at the process boundary.
    """

    if not path or not facts.get("adopted"):
        return None
    if not facts.get("candidate_complete") or not facts.get("candidate_validated"):
        return None
    raw_decisions = facts.get("candidate_source_decisions") or ()
    if not raw_decisions:
        return None
    source_decisions = tuple(
        (_tuple_tree(item[0]), _tuple_tree(item[1]))
        for item in raw_decisions
    )
    branch_path = Path(path)
    payload = write_diagnostic_branch_checkpoint(
        branch_path,
        data=data,
        source_decisions=source_decisions,
        parent_source_decision_fingerprint=parent_branch[
            "source_decision_fingerprint"
        ],
        branch_id=(
            f"supervised-{policy}-{profile}-derived-"
            f"iteration-{int(facts.get('iteration', 0) or 0)}"
        ),
        provenance={
            "runner": "benchmark_adaptive_calibration_supervisor",
            "policy": policy,
            "profile": profile,
            "parent_source_decision_fingerprint": parent_branch[
                "source_decision_fingerprint"
            ],
            "immediate_worker_persistence": True,
            "candidate_processing_elapsed_seconds": facts.get(
                "elapsed_seconds"
            ),
        },
        objective_vector=tuple(facts.get("candidate_objective_vector") or ()),
        substantive_components=dict(facts.get("candidate_components") or {}),
        validation={
            "full_model_validation": True,
            "complete": True,
            "assignment_count": facts.get("candidate_assignment_count"),
            "unmet_request_count": facts.get("candidate_unmet_count", 0),
            "special_commitment_count": facts.get(
                "candidate_special_commitment_count", 0
            ),
        },
    )
    return {
        "path": str(branch_path),
        "source_fingerprint": payload["source_decision_fingerprint"],
        "parent_source_fingerprint": parent_branch[
            "source_decision_fingerprint"
        ],
        "full_model_validated": True,
        "immediate_worker_persistence": True,
        "iteration": int(facts.get("iteration", 0) or 0),
    }


def _run_supervised_worker(args):
    """Execute one prepared calibration branch inside the isolated worker."""

    started = perf_counter()
    phase_timings = {}

    branch = None
    immediate_branch = None

    def _worker_phase_callback(phase, event="completed", **facts):
        """Persist the latest engine phase for hard-stop diagnosis."""

        nonlocal immediate_branch
        if phase == "candidate_processing" and event == "completed":
            try:
                immediate_branch = _write_immediate_worker_branch(
                    getattr(args, "validated_branch_output", None),
                    data=data,
                    parent_branch=branch,
                    facts=facts,
                    policy=args.policy,
                    profile=args.profile,
                ) or immediate_branch
            except (OSError, ValueError, TypeError, KeyError):
                # Branch persistence is diagnostic evidence.  It must never
                # change the solver's candidate or validation behavior.
                pass

        _write_worker_phase(
            args.worker_status,
            phase,
            started,
            event=event,
            **facts,
        )

    phase_started = perf_counter()
    _write_worker_phase(args.worker_status, "benchmark_load", started)
    benchmark = read_durable_stage2_benchmark(args.benchmark_directory)
    data = apply_calibration_profile(benchmark["data"], args.profile)
    phase_timings["benchmark_load_seconds"] = perf_counter() - phase_started

    phase_started = perf_counter()
    _write_worker_phase(args.worker_status, "branch_rehydration", started)
    branch = read_diagnostic_branch_checkpoint(args.branch_input, data=data)
    if args.prepared_incumbent:
        prepared_result = _read_prepared_incumbent(
            args.prepared_incumbent,
            data=data,
            branch=branch,
        )
        checked = {
            "result": prepared_result,
            "validation": {
                "full_model_validation": True,
                "complete": True,
                "unmet_request_count": 0,
                "reused_parent_validation": True,
            },
        }
    else:
        # Keep the hidden worker entry point safe when invoked manually. The
        # supervised runner always supplies the prepared artifact and thus
        # performs this expensive authority check exactly once per trial.
        checked = validate_diagnostic_branch_checkpoint(
            args.branch_input,
            data=data,
            time_limit_seconds=_calibration_validation_time_limit(
                data, getattr(args, "validation_seconds", None)
            ),
            worker_count=1,
        )
        branch = read_diagnostic_branch_checkpoint(args.branch_input, data=data)
    phase_timings["branch_rehydration_seconds"] = perf_counter() - phase_started

    phase_started = perf_counter()
    _write_worker_phase(
        args.worker_status,
        "policy_trial",
        started,
        branch_source_fingerprint=branch["source_decision_fingerprint"],
    )
    trial = run_matched_calibration_trial(
        data,
        initial_result=checked["result"],
        initial_source_decisions=branch["source_decisions"],
        policy=args.policy,
        profile=args.profile,
        total_time_limit_seconds=float(args.total_seconds),
        per_operator_time_limit_seconds=float(args.per_operator_seconds),
        worker_count=int(args.workers),
        session_overrides=(
            STARTUP_AWARE_SESSION_OVERRIDES
            if getattr(args, "startup_aware", False)
            else None
        ),
        candidate_validation_time_limit_seconds=(
            _calibration_validation_time_limit(
                data, getattr(args, "validation_seconds", None)
            )
        ),
        hard_feasibility_validation_time_limit_seconds=(
            _calibration_validation_time_limit(
                data, getattr(args, "validation_seconds", None)
            )
        ),
        hard_feasibility_validation_worker_count=1,
        cp_sat_random_seed=getattr(args, "cp_sat_random_seed", None),
        cp_sat_max_deterministic_time_seconds=(
            getattr(args, "cp_sat_max_deterministic_time_seconds", None)
        ),
        fixed_cycle_names=getattr(args, "fixed_cycle_names", None),
        phase_callback=_worker_phase_callback,
    )
    phase_timings["policy_trial_seconds"] = perf_counter() - phase_started
    payload = trial.to_dict()
    payload["schema"] = CALIBRATION_SUPERVISED_SCHEMA
    payload["protocol_version"] = CALIBRATION_SUPERVISED_PROTOCOL_VERSION
    payload["execution_status"] = EXECUTION_COMPLETED
    payload["solver_reached"] = any(
        int(item.get("session_attempt_count", 0) or 0) > 0
        for item in payload.get("attempts", ())
    )
    # Prefer a worker-persisted branch when the caller requested one.  The
    # trial record also carries the actual terminal semantic state so a
    # supervised run without a durable branch-output path does not report the
    # parent checkpoint as its final result after adopting an improvement.
    final_branch = None
    if immediate_branch is not None:
        try:
            final_branch = read_diagnostic_branch_checkpoint(
                immediate_branch["path"],
                data=data,
            )
        except (OSError, ValueError, TypeError, KeyError):
            # A diagnostic branch that cannot be rehydrated must not replace
            # the parent-validated incumbent in the worker result.
            final_branch = None
    if final_branch is not None:
        # The branch reader exposes materialized IDs for solving, but its
        # stored fingerprint is over the canonical rank-based representation.
        # Preserve that canonical lineage in the supervised payload.
        payload["final_source_decisions"] = [
            [list(key), list(value)]
            for key, value in final_branch["source_decisions"]
        ]
        payload["final_source_decision_fingerprint"] = final_branch[
            "source_decision_fingerprint"
        ]
        payload["final_objective_vector"] = list(
            final_branch.get("objective_vector") or ()
        )
    elif payload.get("final_source_decisions"):
        # No branch path was supplied.  The calibration record's source state
        # came directly from the actual terminal AdaptiveSessionResult, so
        # retain it and recompute the canonical fingerprint through the same
        # current-input boundary used by branch checkpoints.
        trial_source = tuple(
            (_tuple_tree(item[0]), _tuple_tree(item[1]))
            for item in payload["final_source_decisions"]
        )
        payload["final_source_decisions"] = [
            [list(key), list(value)] for key, value in trial_source
        ]
        payload["final_source_decision_fingerprint"] = (
            semantic_stage1_seed_source_fingerprint(data, trial_source)
        )
        payload["final_objective_vector"] = list(
            payload.get("final_objective_vector") or ()
        )
    else:
        # Preserve the historical fallback for mocked/legacy trial records
        # that contain no terminal source state.
        final_branch = branch
        payload["final_source_decisions"] = [
            [list(key), list(value)]
            for key, value in final_branch["source_decisions"]
        ]
        payload["final_source_decision_fingerprint"] = final_branch[
            "source_decision_fingerprint"
        ]
        payload["final_objective_vector"] = list(
            final_branch.get("objective_vector") or ()
        )
    if immediate_branch is not None:
        payload["immediate_worker_branch"] = immediate_branch
    payload["output_protocol_complete"] = True
    payload["phase_timings"] = {
        "worker": {
            **phase_timings,
            "worker_total_seconds": perf_counter() - started,
        },
        "policy": dict(
            (payload.get("timing") or {}).get("phase_timings") or {}
        ),
    }
    payload["worker_preparation"] = {
        "branch_validation": checked["validation"],
        "reused_parent_validation": bool(
            checked["validation"].get("reused_parent_validation")
        ),
        "branch_source_fingerprint": branch["source_decision_fingerprint"],
    }
    _write_worker_phase(args.worker_status, "serialization", started)
    _write_worker_output(args.worker_output, payload)
    return 0


def _supervised_termination_payload(
    *,
    data,
    initial_result,
    branch,
    policy,
    profile_name,
    execution_profile,
    supervision,
    preparation_elapsed_seconds,
):
    initial_value, initial_quality = _calibration_weighted_value(
        initial_result,
        data,
    )
    execution_status = supervision.execution_status
    return {
        "schema": CALIBRATION_SUPERVISED_SCHEMA,
        "protocol_version": CALIBRATION_SUPERVISED_PROTOCOL_VERSION,
        "policy": policy,
        "profile": profile_name,
        "profile_fingerprint": profile_fingerprint(profile_name),
        "input_fingerprint": semantic_student_assignment_input_fingerprint(data),
        "source_seed_fingerprint": branch["source_decision_fingerprint"],
        "configured_budget_seconds": float(
            execution_profile.hard_wall_seconds
        ),
        "per_operator_time_limit_seconds": None,
        "worker_count": None,
        "initial_substantive_value": initial_value,
        "final_substantive_value": initial_value,
        "final_assignment_count": len(initial_result.assignments),
        "final_unmet_count": len(initial_result.unmet_requests),
        "final_special_commitment_count": len(
            initial_result.commitment_assignments
        ),
        "candidate_complete": False,
        "candidate_from_terminated_worker": False,
        "attempts": (),
        "decisions": (),
        "timing": {
            "policy_selection_seconds": 0.0,
            "operator_execution_seconds": 0.0,
            "finalization_seconds": 0.0,
            "external_overrun_seconds": max(
                0.0,
                supervision.elapsed_seconds - execution_profile.hard_wall_seconds,
            ),
            "total_elapsed_seconds": supervision.elapsed_seconds,
        },
        "phase_timings": {
            "parent": {
                "total_seconds": preparation_elapsed_seconds,
            },
            "worker": {
                "last_observed_phase": dict(supervision.last_worker_phase),
            },
            "policy": {},
            # Keep this flat compatibility field for consumers that read the
            # original termination payload shape.
            "parent_preparation_seconds": preparation_elapsed_seconds,
            "worker_last_phase": dict(supervision.last_worker_phase),
        },
        "final_components": initial_quality.get(
            "objective_semantics", {}
        ).get("components", {}),
        "resource": dict(
            supervision.resource_guard_facts.get("snapshot")
            or supervision.resource_snapshot
            or {}
        ),
        "execution_status": execution_status,
        "hard_wall_seconds": float(execution_profile.hard_wall_seconds),
        "termination_grace_seconds": float(
            execution_profile.termination_grace_seconds
        ),
        "hard_deadline_elapsed_seconds": supervision.hard_deadline_elapsed_seconds,
        "resource_guard": {
            "max_process_tree_rss_bytes": (
                execution_profile.max_process_tree_rss_bytes
            ),
            "min_system_available_memory_bytes": (
                execution_profile.min_system_available_memory_bytes
            ),
            "facts": dict(supervision.resource_guard_facts),
        },
        "worker_pid": supervision.worker_pid,
        "worker_exit_code": supervision.worker_exit_code,
        "output_protocol_complete": True,
        "solver_reached": False,
        "retained_source_fingerprint": branch["source_decision_fingerprint"],
        "retained_incumbent": {
            "complete": True,
            "full_model_validated": True,
            "source_fingerprint": branch["source_decision_fingerprint"],
            "assignment_count": len(initial_result.assignments),
            "unmet_request_count": len(initial_result.unmet_requests),
            "special_commitment_count": len(
                initial_result.commitment_assignments
            ),
            "substantive_value": initial_value,
        },
        "candidate_diagnostic": "not_authoritative_after_worker_termination",
        "supervision": supervision.to_dict(),
    }


def run_supervised_calibration_trial(
    *,
    policy,
    profile="balanced",
    benchmark_directory,
    branch_input=None,
    total_time_limit_seconds=1800.0,
    per_operator_time_limit_seconds=180.0,
    worker_count=8,
    validation_time_limit_seconds=None,
    hard_wall_seconds=None,
    termination_grace_seconds=5.0,
    max_process_tree_rss_bytes=None,
    min_system_available_memory_bytes=None,
    poll_interval_seconds=0.25,
    validated_branch_output=None,
    cp_sat_random_seed=None,
    cp_sat_max_deterministic_time_seconds=None,
    startup_aware=False,
    fixed_cycle_names=None,
    cancel_requested=None,
    worker_started_callback=None,
):
    """Run one detached calibration trial under a true parent-side deadline."""

    if profile not in CALIBRATION_PROFILES:
        raise ValueError(f"Unknown calibration profile: {profile}")
    if not benchmark_directory:
        raise ValueError(
            "Supervised calibration requires a detached benchmark directory"
        )
    started = perf_counter()
    benchmark_load_started = perf_counter()
    benchmark = read_durable_stage2_benchmark(benchmark_directory)
    data = apply_calibration_profile(benchmark["data"], profile)
    manifest = benchmark["manifest"]
    benchmark_load_seconds = perf_counter() - benchmark_load_started
    branch_directory = None
    branch_materialization_started = perf_counter()
    try:
        if branch_input is None:
            branch_directory = tempfile.TemporaryDirectory(
                prefix="student-supervised-calibration-"
            )
            branch_input = Path(branch_directory.name) / "incumbent.json.gz"
            stage1 = dict(manifest.get("stage1") or {})
            write_diagnostic_branch_checkpoint(
                branch_input,
                data=data,
                source_decisions=tuple(benchmark["seed"]["seed_source_decisions"]),
                parent_source_decision_fingerprint=manifest.get(
                    "seed_source_decision_fingerprint"
                ),
                branch_id=f"supervised-{policy}-{profile}",
                provenance={
                    "runner": "benchmark_adaptive_calibration_supervisor",
                    "policy": policy,
                    "profile": profile,
                },
                objective_vector=stage1.get("objective_vector", ()),
                substantive_components=stage1.get("substantive_components", {}),
                validation={
                    "full_model_validation": bool(stage1.get("validated")),
                    "complete": bool(stage1.get("complete")),
                    "assignment_count": stage1.get("source_decision_count"),
                    "required_source_decision_group_count": manifest.get(
                        "counts", {}
                    ).get("required_source_decision_group_count"),
                    "unmet_request_count": manifest.get("counts", {}).get(
                        "unmet_required_request_count", 0
                    ),
                    "special_commitment_count": manifest.get("counts", {}).get(
                        "special_commitment_count", 0
                    ),
                },
            )
        branch_materialization_seconds = (
            perf_counter() - branch_materialization_started
        )
        checked = validate_diagnostic_branch_checkpoint(
            branch_input,
            data=data,
            time_limit_seconds=_calibration_validation_time_limit(
                data, validation_time_limit_seconds
            ),
            worker_count=1,
        )
        branch = read_diagnostic_branch_checkpoint(branch_input, data=data)

        if hard_wall_seconds is None:
            hard_wall_seconds = total_time_limit_seconds
        default_execution_profile = TARGET_SCALE_CALIBRATION_EXECUTION_PROFILE
        profile_config = CalibrationExecutionProfile(
            hard_wall_seconds=float(hard_wall_seconds),
            termination_grace_seconds=float(termination_grace_seconds),
            max_process_tree_rss_bytes=(
                default_execution_profile.max_process_tree_rss_bytes
                if max_process_tree_rss_bytes is None
                else max_process_tree_rss_bytes
            ),
            min_system_available_memory_bytes=(
                default_execution_profile.min_system_available_memory_bytes
                if min_system_available_memory_bytes is None
                else min_system_available_memory_bytes
            ),
            poll_interval_seconds=float(poll_interval_seconds),
        )
        with tempfile.TemporaryDirectory(
            prefix="student-supervised-worker-"
        ) as worker_directory:
            worker_output = Path(worker_directory) / "result.json"
            worker_status = Path(worker_directory) / "status.json"
            if validated_branch_output:
                # A caller may reuse a diagnostic output path.  Never mistake
                # a branch from an earlier process for an improvement from
                # this trial.
                Path(validated_branch_output).unlink(missing_ok=True)
            command = [
                sys.executable,
                "-m",
                "scheduling_engine.benchmark_adaptive_calibration",
                "--worker",
                "--benchmark-directory",
                str(benchmark_directory),
                "--branch-input",
                str(branch_input),
                "--worker-output",
                str(worker_output),
                "--worker-status",
                str(worker_status),
                "--prepared-incumbent",
                str(
                    Path(worker_directory) / "prepared-incumbent.json"
                ),
                "--policy",
                policy,
                "--profile",
                profile,
                "--total-seconds",
                str(float(total_time_limit_seconds)),
                "--per-operator-seconds",
                str(float(per_operator_time_limit_seconds)),
                "--workers",
                str(int(worker_count)),
            ]
            if validation_time_limit_seconds is not None:
                command.extend(
                    ["--validation-seconds", str(float(validation_time_limit_seconds))]
                )
            if cp_sat_random_seed is not None:
                command.extend(["--cp-sat-random-seed", str(int(cp_sat_random_seed))])
            if cp_sat_max_deterministic_time_seconds is not None:
                command.extend([
                    "--cp-sat-max-deterministic-time-seconds",
                    str(float(cp_sat_max_deterministic_time_seconds)),
                ])
            if startup_aware:
                command.append("--startup-aware")
            if fixed_cycle_names is not None:
                command.extend(["--fixed-cycle-names", *tuple(fixed_cycle_names)])
            if validated_branch_output:
                command.extend(
                    [
                        "--validated-branch-output",
                        str(Path(validated_branch_output).resolve()),
                    ]
                )
            _write_prepared_incumbent(
                Path(worker_directory) / "prepared-incumbent.json",
                data=data,
                branch=branch,
                result=checked["result"],
            )
            parent_preparation_elapsed = perf_counter() - started
            supervision = supervise_json_worker(
                command,
                output_path=worker_output,
                status_path=worker_status,
                execution_profile=profile_config,
                cwd=str(Path.cwd()),
                cancel_requested=cancel_requested,
                worker_started_callback=worker_started_callback,
            )
            if supervision.execution_status == EXECUTION_COMPLETED:
                payload = dict(supervision.payload or {})
                payload["schema"] = CALIBRATION_SUPERVISED_SCHEMA
                payload["protocol_version"] = CALIBRATION_SUPERVISED_PROTOCOL_VERSION
                payload["execution_status"] = EXECUTION_COMPLETED
                payload["hard_wall_seconds"] = float(profile_config.hard_wall_seconds)
                payload["termination_grace_seconds"] = float(
                    profile_config.termination_grace_seconds
                )
                payload["supervision"] = supervision.to_dict()
                payload["parent_preparation_seconds"] = parent_preparation_elapsed
                payload["worker_pid"] = supervision.worker_pid
                payload["worker_exit_code"] = supervision.worker_exit_code
                payload["output_protocol_complete"] = True
                existing_phase_timings = dict(payload.get("phase_timings") or {})
                existing_phase_timings["parent"] = {
                    "benchmark_load_seconds": benchmark_load_seconds,
                    "branch_materialization_seconds": branch_materialization_seconds,
                    "branch_validation_seconds": float(
                        checked["validation"].get("elapsed_seconds", 0.0) or 0.0
                    ),
                    "preparation_total_seconds": parent_preparation_elapsed,
                }
                existing_phase_timings["supervision"] = {
                    "worker_elapsed_seconds": supervision.elapsed_seconds,
                    "cleanup_seconds": float(
                        (supervision.cleanup or {}).get(
                            "cleanup_elapsed_seconds", 0.0
                        )
                        or 0.0
                    ),
                }
                payload["phase_timings"] = existing_phase_timings
                payload["retained_source_fingerprint"] = branch[
                    "source_decision_fingerprint"
                ]
            else:
                payload = _supervised_termination_payload(
                    data=data,
                    initial_result=checked["result"],
                    branch=branch,
                    policy=policy,
                    profile_name=profile,
                    execution_profile=profile_config,
                    supervision=supervision,
                    preparation_elapsed_seconds=parent_preparation_elapsed,
                )
            if (
                validated_branch_output
                and Path(validated_branch_output).exists()
                and not payload.get("derived_branch")
            ):
                # The worker can persist a validated improvement before the
                # outer policy returns.  Revalidate it in the parent before
                # exposing it as reusable study state.
                try:
                    worker_branch = read_diagnostic_branch_checkpoint(
                        validated_branch_output,
                        data=data,
                    )
                    branch_validation = validate_diagnostic_branch_checkpoint(
                        validated_branch_output,
                        data=data,
                        time_limit_seconds=_calibration_validation_time_limit(
                            data, validation_time_limit_seconds
                        ),
                        worker_count=1,
                    )
                    if (
                        branch_validation["validation"].get(
                            "full_model_validation"
                        )
                        and branch_validation["validation"].get("complete")
                        and int(
                            branch_validation["validation"].get(
                                "unmet_request_count", 0
                            )
                            or 0
                        )
                        == 0
                    ):
                        payload["derived_branch"] = {
                            "path": str(Path(validated_branch_output)),
                            "source_fingerprint": worker_branch[
                                "source_decision_fingerprint"
                            ],
                            "parent_source_fingerprint": branch[
                                "source_decision_fingerprint"
                            ],
                            "full_model_validated": True,
                            "parent_revalidated_after_worker": True,
                            "validation": branch_validation["validation"],
                        }
                except (OSError, ValueError, TypeError, KeyError):
                    # A partially written or invalid artifact is never
                    # promoted.  The normal termination payload remains the
                    # authoritative result.
                    pass
            if not payload.get("derived_branch"):
                # When the worker-written branch has already been revalidated
                # above, retain that parent-authority metadata. Rewriting the
                # same branch here would preserve the file but erase the
                # proof that the parent performed the current-model check.
                _write_validated_supervised_branch(
                    validated_branch_output,
                    data=data,
                    parent_branch=branch,
                    payload=payload,
                    policy=policy,
                    profile=profile,
                )
    finally:
        if branch_directory is not None:
            branch_directory.cleanup()
    payload["preparation"] = {
        "elapsed_seconds": parent_preparation_elapsed,
        "benchmark_load_seconds": benchmark_load_seconds,
        "branch_materialization_seconds": branch_materialization_seconds,
        "branch_validation_seconds": float(
            checked["validation"].get("elapsed_seconds", 0.0) or 0.0
        ),
        "parent_branch_validation": checked["validation"],
        "input_fingerprint": semantic_student_assignment_input_fingerprint(data),
        "initial_status": checked["result"].status,
        "initial_solver_outcome": checked["result"].solver_outcome,
        "initial_assignment_count": len(checked["result"].assignments),
        "initial_unmet_count": len(checked["result"].unmet_requests),
        "branch_revalidated": True,
        "branch_source_fingerprint": branch["source_decision_fingerprint"],
    }
    return payload


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
    cp_sat_random_seed=None,
    cp_sat_max_deterministic_time_seconds=None,
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
            time_limit_seconds=_calibration_validation_time_limit(data),
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
            hard_feasibility_validation_time_limit_seconds=(
                _calibration_validation_time_limit(data)
            ),
            hard_feasibility_validation_worker_count=1,
            cp_sat_random_seed=cp_sat_random_seed,
            cp_sat_max_deterministic_time_seconds=(
                cp_sat_max_deterministic_time_seconds
            ),
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
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--policy",
        choices=(
            "adaptive",
            "adaptive_balanced",
            "adaptive_student_pressure_biased",
            "adaptive_utilization_biased",
            "stateless_role",
            "r2_only",
            "student_repair_only",
            "student_repair_r8_only",
            "utilization_only",
            "fixed_cycle",
        ),
        default="adaptive",
    )
    parser.add_argument("--profile", choices=tuple(CALIBRATION_PROFILES), default="balanced")
    parser.add_argument("--students", type=int, default=120)
    parser.add_argument("--benchmark-directory", type=Path)
    parser.add_argument("--branch-input", type=Path)
    parser.add_argument("--worker-output", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--worker-status", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--prepared-incumbent", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--initial-seconds", type=float, default=5.0)
    parser.add_argument("--initial-workers", type=int, default=2)
    parser.add_argument("--total-seconds", type=float, default=60.0)
    parser.add_argument("--per-operator-seconds", type=float, default=60.0)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--validation-seconds", type=float)
    parser.add_argument("--branch-output", type=Path)
    parser.add_argument("--hard-wall-seconds", type=float)
    parser.add_argument("--termination-grace-seconds", type=float, default=5.0)
    parser.add_argument("--max-process-tree-rss-bytes", type=int)
    parser.add_argument("--min-system-available-memory-bytes", type=int)
    parser.add_argument("--poll-interval-seconds", type=float, default=0.25)
    parser.add_argument("--validated-branch-output", type=Path)
    parser.add_argument("--cp-sat-random-seed", type=int)
    parser.add_argument("--cp-sat-max-deterministic-time-seconds", type=float)
    parser.add_argument(
        "--startup-aware",
        action="store_true",
        help="Use the diagnostic startup-aware policy comparison budget.",
    )
    parser.add_argument(
        "--fixed-cycle-names",
        nargs="+",
        help="Diagnostic override for the existing fixed-cycle operator tuple.",
    )
    args = parser.parse_args(argv)
    if args.worker:
        if not args.worker_output or not args.worker_status:
            raise ValueError("worker mode requires worker output/status paths")
        return _run_supervised_worker(args)
    if args.benchmark_directory:
        payload = run_supervised_calibration_trial(
            policy=args.policy,
            profile=args.profile,
            benchmark_directory=args.benchmark_directory,
            branch_input=args.branch_input,
            total_time_limit_seconds=args.total_seconds,
            per_operator_time_limit_seconds=args.per_operator_seconds,
            worker_count=args.workers,
            validation_time_limit_seconds=args.validation_seconds,
            hard_wall_seconds=args.hard_wall_seconds,
            termination_grace_seconds=args.termination_grace_seconds,
            max_process_tree_rss_bytes=args.max_process_tree_rss_bytes,
            min_system_available_memory_bytes=(
                args.min_system_available_memory_bytes
            ),
            poll_interval_seconds=args.poll_interval_seconds,
            validated_branch_output=args.validated_branch_output,
            cp_sat_random_seed=args.cp_sat_random_seed,
            cp_sat_max_deterministic_time_seconds=(
                args.cp_sat_max_deterministic_time_seconds
            ),
            startup_aware=args.startup_aware,
            fixed_cycle_names=args.fixed_cycle_names,
        )
    else:
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
            cp_sat_random_seed=args.cp_sat_random_seed,
            cp_sat_max_deterministic_time_seconds=(
                args.cp_sat_max_deterministic_time_seconds
            ),
        )
    json.dump(payload, sys.stdout, indent=2, sort_keys=True, default=str)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
