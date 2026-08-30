"""Diagnostic harness for measuring authoritative candidate validation.

The harness deliberately calls the same full-model source-decision validator
used by diagnostic search.  It does not create a second validator, alter the
model, or make a candidate authoritative.  Its purpose is to expose a stable
phase/result record so validation-cost experiments can distinguish preparation,
CP-SAT validation, and result reconstruction.
"""

from __future__ import annotations

from collections import Counter
from time import monotonic

from .core import run_student_assignment_source_decision_validation_diagnostic
from .runtime import semantic_student_assignment_input_fingerprint


VALIDATION_BENCHMARK_SCHEMA = "student_assignment_validation_benchmark_v1"


def _result_summary(result):
    optimization_facts = result.optimization_facts or {}
    stage_1 = optimization_facts.get("stage_1") or {}
    stage_2 = optimization_facts.get("stage_2") or {}
    return {
        "status": result.status,
        "solver_outcome": result.solver_outcome,
        "assignment_count": len(result.assignments),
        "unmet_request_count": len(result.unmet_requests),
        "special_commitment_count": len(result.commitment_assignments),
        "diagnostic_code_counts": dict(Counter(
            item.get("code")
            for item in result.diagnostics
            if isinstance(item, dict) and item.get("code") is not None
        )),
        "objective_components": dict(result.objective_components or {}),
        "objective_values": tuple(stage_2.get("objective_values") or ()),
        "required_decision_group_count": stage_1.get("required_decision_group_count"),
        "full_model_variable_count": optimization_facts.get(
            "full_model_variable_count"
        ),
        "full_model_constraint_count": optimization_facts.get(
            "full_model_constraint_count"
        ),
        "model_family_variable_counts": dict(
            optimization_facts.get("model_family_variable_counts") or {}
        ),
        "model_family_constraint_counts": dict(
            optimization_facts.get("model_family_constraint_counts") or {}
        ),
        "stage_1_timings": dict(stage_1.get("timings") or {}),
        "stage_2_status": stage_2.get("solver_outcome"),
        "stage_2_operation_wall_time_seconds": stage_2.get(
            "operation_wall_time_seconds"
        ),
        "finalization_timings": dict(
            optimization_facts.get("finalization_timings") or {}
        ),
    }


def run_source_decision_validation_benchmark(
    data,
    source_decisions,
    *,
    candidate_name="candidate",
    expected_input_fingerprint=None,
    expected_source_decision_fingerprint=None,
    time_limit_seconds=120.0,
    worker_count=1,
    collect_resource_telemetry=True,
    collect_validation_presolve_telemetry=False,
    collect_validation_search_start_telemetry=False,
):
    """Run and summarize one bounded full-validator benchmark.

    ``source_decisions`` are semantic decisions from the same detached input;
    they are never treated as a trusted schedule.  The authoritative engine
    validates them through CP-SAT before the result is summarized.  Fingerprint
    checks fail before the solve when a caller accidentally pairs a candidate
    with a different input or lineage.
    """

    # Import lazily because stage2_benchmark imports the realistic fixture
    # builders, while those builders import the public student-assignment
    # package during module initialization.
    from .stage2_benchmark import (
        semantic_stage1_seed_source_fingerprint,
        stage1_seed_source_fingerprint,
    )

    preparation_started = monotonic()
    input_fingerprint = semantic_student_assignment_input_fingerprint(data)
    if (
        expected_input_fingerprint is not None
        and input_fingerprint != expected_input_fingerprint
    ):
        raise ValueError("Student-assignment input fingerprint does not match")
    source_decisions = tuple(sorted(source_decisions or (), key=repr))
    try:
        source_fingerprint = semantic_stage1_seed_source_fingerprint(
            data,
            source_decisions,
        )
    except ValueError:
        # Some older detached DTO fixtures omit explicit TimeSlotDTO rows even
        # though sections carry timeslot IDs.  Preserve their existing raw
        # source fingerprint behavior; this is an identity/reporting fallback,
        # never a bypass of CP-SAT validation.
        source_fingerprint = stage1_seed_source_fingerprint(source_decisions)
    if expected_source_decision_fingerprint is not None:
        expected_matches = source_fingerprint == expected_source_decision_fingerprint
        if not expected_matches:
            try:
                expected_matches = (
                    stage1_seed_source_fingerprint(source_decisions)
                    == expected_source_decision_fingerprint
                )
            except Exception:
                expected_matches = False
        if not expected_matches:
            raise ValueError("Source-decision fingerprint does not match")
    preparation_seconds = monotonic() - preparation_started

    captured_validation_outcome = {}

    def capture_validation_outcome(outcome, *, elapsed_seconds):
        captured_validation_outcome["outcome"] = outcome
        captured_validation_outcome["elapsed_seconds"] = elapsed_seconds

    validation_started = monotonic()
    validation_error = None
    try:
        result = run_student_assignment_source_decision_validation_diagnostic(
            data,
            source_decisions=source_decisions,
            time_limit_seconds=time_limit_seconds,
            worker_count=worker_count,
            capture_final_source_decisions=True,
            collect_resource_telemetry=collect_resource_telemetry,
            collect_validation_presolve_telemetry=(
                collect_validation_presolve_telemetry
            ),
            collect_validation_search_start_telemetry=(
                collect_validation_search_start_telemetry
            ),
            validation_telemetry_callback=capture_validation_outcome,
        )
    except ValueError as error:  # pragma: no cover - exercised by invalid probes
        # The engine intentionally fails closed when semantic source decisions
        # cannot be represented by the current model.  Preserve that outcome
        # in benchmark form instead of turning an invalid candidate into a
        # benchmark-process crash.
        result = None
        validation_error = f"{type(error).__name__}: {error}"
    operation_seconds = monotonic() - validation_started

    optimization_facts = (result.optimization_facts or {}) if result else {}
    stage_2 = optimization_facts.get("stage_2") or {}
    telemetry = dict(stage_2.get("alternate_seed_validation_telemetry") or {})
    final_source_decisions = tuple(stage_2.get("final_source_decisions") or ())
    identity_matches = stage_2.get("alternate_source_decision_identity_matches")
    if final_source_decisions:
        try:
            final_source_fingerprint = semantic_stage1_seed_source_fingerprint(
                data,
                final_source_decisions,
            )
        except ValueError:
            final_source_fingerprint = stage1_seed_source_fingerprint(
                final_source_decisions
            )
    else:
        final_source_fingerprint = None

    captured_outcome = captured_validation_outcome.get("outcome")
    if result is None and captured_outcome is not None:
        validation_facts = {
            "classification": captured_outcome.classification,
            "solver_outcome": captured_outcome.solver_outcome,
            "operation_elapsed_seconds": operation_seconds,
            "telemetry": dict(captured_outcome.telemetry),
            "error": captured_outcome.error,
            "source_decision_identity_checked": False,
            "source_decision_identity_matches": None,
            "candidate_source_decision_count": len(source_decisions),
            "validated_source_decision_count": 0,
            "final_source_decision_fingerprint": None,
        }
        result_facts = None
    elif result is None:
        validation_facts = {
            "classification": "validation_error",
            "solver_outcome": "error",
            "operation_elapsed_seconds": operation_seconds,
            "telemetry": {},
            "error": validation_error,
        }
        result_facts = None
    else:
        validation_facts = {
            "classification": stage_2.get(
                "alternate_seed_validation_classification"
            ),
            "solver_outcome": stage_2.get(
                "alternate_seed_validation_solver_outcome"
            ),
            "operation_elapsed_seconds": operation_seconds,
            "telemetry": telemetry,
            "error": None,
            "source_decision_identity_checked": stage_2.get(
                "alternate_source_decision_identity_checked"
            ),
            "source_decision_identity_matches": identity_matches,
            "candidate_source_decision_count": stage_2.get(
                "alternate_source_decision_count"
            ),
            "validated_source_decision_count": stage_2.get(
                "validated_source_decision_count"
            ),
            "final_source_decision_fingerprint": final_source_fingerprint,
        }
        result_facts = _result_summary(result)

    return {
        "schema": VALIDATION_BENCHMARK_SCHEMA,
        "candidate_name": candidate_name,
        "input_semantic_fingerprint": input_fingerprint,
        "source_decision_fingerprint": source_fingerprint,
        "requested": {
            "time_limit_seconds": float(time_limit_seconds),
            "worker_count": int(worker_count),
        },
        "preparation": {
            "elapsed_seconds": preparation_seconds,
        },
        "validation": validation_facts,
        "result": result_facts,
        "resource": dict(optimization_facts.get("operation_resource_monitor") or {}),
    }
