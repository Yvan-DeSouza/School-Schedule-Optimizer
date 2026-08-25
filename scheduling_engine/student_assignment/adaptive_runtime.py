"""Diagnostic runner for the Objective Semantics v2 local-search allocator.

The allocator is intentionally a wrapper around existing diagnostic operators.
It owns neither CP-SAT constraints nor schedule authority: every attempted
candidate is produced by CP-SAT and must pass the existing full-model
validation before it can become the next incumbent.
"""

from __future__ import annotations

from dataclasses import dataclass
from time import monotonic
from uuid import uuid4

from .adaptive_search import (
    AdaptiveOperatorAttempt,
    AdaptiveSessionRecord,
    DEFAULT_ADAPTIVE_OPERATOR_PORTFOLIO,
    build_adaptive_search_state,
    choose_adaptive_operator,
)
from .core import (
    run_student_assignment_ordinary_repair_diagnostic,
    run_student_assignment_targeted_s1_diagnostic,
    run_student_assignment_targeted_s2_diagnostic,
)
from .runtime import semantic_student_assignment_input_fingerprint
from .search_experiments import source_decision_fingerprint
from .search_guidance import rank_students_by_quality_pressure
from .quality import evaluate_student_assignment_quality


@dataclass(frozen=True)
class AdaptiveSessionResult:
    """The diagnostic record plus the strongest validated result observed."""

    record: AdaptiveSessionRecord
    result: object
    source_decisions: tuple


def _quality_report(data, result):
    """Rebuild entity facts from the current result for policy state only."""

    return evaluate_student_assignment_quality(
        data,
        assignments=result.assignments,
        commitment_assignments=result.commitment_assignments,
        solver_objective_components=result.objective_components,
        include_entity_metrics=True,
    )


def _weighted_substantive_value(result):
    components = dict(result.objective_components or {})
    weighted = components.get("weighted_normalized_contributions")
    if weighted is not None:
        return float(sum(float(value or 0) for value in weighted.values()))
    # v1 diagnostic fixtures do not have normalized contributions. Keeping a
    # raw fallback makes the runner useful for controls without changing the
    # v2 production objective or pretending the scales are interchangeable.
    return float(
        sum(
            float(components.get(name, 0) or 0)
            for name in (
                "section_utilization_balance_penalty",
                "student_semester_balance_penalty",
                "difficulty_balance_penalty",
                "course_category_diversity_penalty",
            )
        )
    )


def _source_decisions_from_result(result):
    return tuple(
        (result.optimization_facts or {})
        .get("stage_2", {})
        .get("final_source_decisions", ())
    )


def _operator_result(data, spec, *, selected_student_ids, current_source_decisions,
                    time_limit_seconds, worker_count, collect_resource_telemetry,
                    hard_feasibility_time_limit_seconds=None,
                    hard_feasibility_validation_time_limit_seconds=None,
                    hard_feasibility_worker_count=None,
                    hard_feasibility_validation_worker_count=None):
    common = {
        "neighborhood_radius": spec.radius,
        "time_limit_seconds": time_limit_seconds,
        "total_time_limit_seconds": time_limit_seconds,
        "worker_count": worker_count,
        "alternate_source_decisions": current_source_decisions,
        "capture_final_source_decisions": True,
        "collect_resource_telemetry": collect_resource_telemetry,
    }
    if hard_feasibility_time_limit_seconds is not None:
        common["hard_feasibility_time_limit_seconds"] = hard_feasibility_time_limit_seconds
    if hard_feasibility_validation_time_limit_seconds is not None:
        common["hard_feasibility_validation_time_limit_seconds"] = (
            hard_feasibility_validation_time_limit_seconds
        )
    if hard_feasibility_worker_count is not None:
        common["hard_feasibility_worker_count"] = hard_feasibility_worker_count
    if hard_feasibility_validation_worker_count is not None:
        common["hard_feasibility_validation_worker_count"] = (
            hard_feasibility_validation_worker_count
        )
    if spec.name == "r2":
        ordinary_common = {
            key: value
            for key, value in common.items()
            if key not in {
                "hard_feasibility_time_limit_seconds",
                "hard_feasibility_worker_count",
            }
        }
        return run_student_assignment_ordinary_repair_diagnostic(
            data,
            max_changed_students=None,
            **ordinary_common,
        )
    if spec.name == "targeted_r8_s1":
        return run_student_assignment_targeted_s1_diagnostic(
            data,
            selected_student_id=selected_student_ids[0],
            **common,
        )
    if spec.name in {"targeted_r8_s2", "targeted_r4_s2"}:
        return run_student_assignment_targeted_s2_diagnostic(
            data,
            selected_student_ids=selected_student_ids,
            **common,
        )
    raise ValueError(f"Unsupported adaptive diagnostic operator: {spec.name}")


def run_adaptive_local_search_diagnostic(
    data,
    *,
    initial_result,
    initial_source_decisions=(),
    total_time_limit_seconds=180.0,
    per_operator_time_limit_seconds=60.0,
    worker_count=8,
    portfolio=DEFAULT_ADAPTIVE_OPERATOR_PORTFOLIO,
    max_iterations=None,
    collect_resource_telemetry=False,
    hard_feasibility_time_limit_seconds=None,
    hard_feasibility_validation_time_limit_seconds=None,
    hard_feasibility_worker_count=None,
    hard_feasibility_validation_worker_count=None,
    session_id=None,
):
    """Run the diagnostic v2 allocator inside one shared wall-clock budget.

    This is not called by ``solve_student_assignment``. It is suitable for
    offline comparison only. A failed, unknown, partial, or unvalidated
    candidate is recorded and never replaces the current incumbent.
    """

    if data.objective_semantics_version != "v2":
        raise ValueError("adaptive local search requires Objective Semantics v2")
    if initial_result.status != "complete" or initial_result.unmet_requests:
        raise ValueError("adaptive search requires a complete initial incumbent")
    current_source_decisions = tuple(initial_source_decisions) or _source_decisions_from_result(
        initial_result
    )
    if not current_source_decisions:
        raise ValueError("adaptive search requires semantic source decisions")

    configured_budget = max(0.001, float(total_time_limit_seconds))
    per_operator = max(0.001, float(per_operator_time_limit_seconds))
    started = monotonic()
    history = []
    decisions = []
    current_result = initial_result
    current_value = _weighted_substantive_value(current_result)
    initial_components = dict(current_result.objective_components or {})
    initial_quality = _quality_report(data, current_result)
    iteration_limit = (
        max(1, int(max_iterations)) if max_iterations is not None else None
    )
    stopping_reason = "shared_budget_exhausted"

    while monotonic() - started < configured_budget:
        if iteration_limit is not None and len(history) >= iteration_limit:
            stopping_reason = "iteration_budget_exhausted"
            break
        elapsed = monotonic() - started
        remaining = max(0.0, configured_budget - elapsed)
        quality = _quality_report(data, current_result)
        ranked = rank_students_by_quality_pressure(data, quality)
        state = build_adaptive_search_state(
            data,
            quality,
            elapsed_seconds=elapsed,
            remaining_seconds=remaining,
            history=tuple(history),
        )
        decision = choose_adaptive_operator(
            state,
            portfolio=portfolio,
            ranked_students=ranked,
        )
        if decision is None:
            stopping_reason = "no_eligible_operator"
            break
        decisions.append(decision.to_dict())
        selected = tuple(decision.selected_student_ids)
        operation_limit = min(per_operator, remaining)
        operation_started = monotonic()
        result = _operator_result(
            data,
            decision.operator,
            selected_student_ids=selected,
            current_source_decisions=current_source_decisions,
            time_limit_seconds=operation_limit,
            worker_count=worker_count,
            collect_resource_telemetry=collect_resource_telemetry,
            hard_feasibility_time_limit_seconds=hard_feasibility_time_limit_seconds,
            hard_feasibility_validation_time_limit_seconds=(
                hard_feasibility_validation_time_limit_seconds
            ),
            hard_feasibility_worker_count=hard_feasibility_worker_count,
            hard_feasibility_validation_worker_count=(
                hard_feasibility_validation_worker_count
            ),
        )
        operation_elapsed = monotonic() - operation_started
        local = dict((result.optimization_facts or {}).get("stage_2_local_bootstrap") or {})
        candidate_source = _source_decisions_from_result(result)
        candidate_value = _weighted_substantive_value(result)
        candidate_found = bool(local.get("candidate_found", False))
        candidate_validated = bool(local.get("candidate_validated", False))
        hard_complete = result.status == "complete" and not result.unmet_requests
        adopted = bool(
            hard_complete
            and candidate_found
            and candidate_validated
            and candidate_source
            and candidate_value < current_value
        )
        gain = max(0.0, current_value - candidate_value) if adopted else 0.0
        if adopted:
            current_result = result
            current_source_decisions = candidate_source
            current_value = candidate_value
            stopping_reason = "validated_improvement_adopted"
        status = str(local.get("status") or result.solver_outcome or "unknown")
        history.append(
            AdaptiveOperatorAttempt(
                operator=decision.operator.name,
                status=status,
                candidate_found=candidate_found,
                candidate_validated=candidate_validated,
                adopted=adopted,
                gain=gain,
                elapsed_seconds=operation_elapsed,
                solver_wall_time_seconds=local.get("solver_wall_time_seconds"),
                validation_seconds=local.get("validation_elapsed_seconds"),
                changed_student_count=int(local.get("changed_student_count", 0) or 0),
                changed_source_decision_count=int(
                    local.get("changed_source_decision_count", 0) or 0
                ),
                unknown=status == "unknown",
                infeasible=status == "infeasible",
                stopping_reason=local.get("stopping_reason"),
            )
        )
        if monotonic() - started >= configured_budget:
            stopping_reason = "shared_budget_exhausted"

    if stopping_reason == "validated_improvement_adopted" and monotonic() - started >= configured_budget:
        stopping_reason = "shared_budget_exhausted"
    final_components = dict(current_result.objective_components or {})
    final_quality = _quality_report(data, current_result)
    record = AdaptiveSessionRecord(
        session_id=str(session_id or uuid4()),
        policy_version=state.policy_version if "state" in locals() else "v2-local-allocator-diagnostic-1",
        input_fingerprint=semantic_student_assignment_input_fingerprint(data),
        source_seed_fingerprint=source_decision_fingerprint(initial_source_decisions or _source_decisions_from_result(initial_result)),
        objective_semantics_version=data.objective_semantics_version,
        counselor_scores=dict(data.objective_importance_scores),
        configured_budget_seconds=configured_budget,
        elapsed_seconds=monotonic() - started,
        initial_components=initial_quality.get("objective_semantics", {}).get("components", {}) or initial_components,
        final_components=final_quality.get("objective_semantics", {}).get("components", {}) or final_components,
        initial_objective_vector=tuple(
            (initial_result.optimization_facts or {}).get("stage_1", {}).get("objective_vector", ())
            or (initial_result.optimization_facts or {}).get("stage_1", {}).get("objective_values", ())
        ),
        final_objective_vector=tuple(
            (current_result.optimization_facts or {}).get("stage_2", {}).get("objective_values", ())
        ),
        attempts=tuple({
            **attempt.__dict__,
            "gain_per_minute": attempt.gain_per_minute,
        } for attempt in history),
        decisions=tuple(decisions),
        stopping_reason=stopping_reason,
        final_assignment_count=len(current_result.assignments),
        final_unmet_count=len(current_result.unmet_requests),
        final_special_commitment_count=len(current_result.commitment_assignments),
        resource=dict(
            (current_result.optimization_facts or {}).get("operation_resource_monitor") or {}
        ),
    )
    return AdaptiveSessionResult(
        record=record,
        result=current_result,
        source_decisions=current_source_decisions,
    )


__all__ = ["AdaptiveSessionResult", "run_adaptive_local_search_diagnostic"]
