"""Diagnostic runner for the Objective Semantics v2 local-search allocator.

The allocator is intentionally a wrapper around existing diagnostic operators.
It owns neither CP-SAT constraints nor schedule authority: every attempted
candidate is produced by CP-SAT and must pass the existing full-model
validation before it can become the next incumbent.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from time import monotonic
from uuid import uuid4

from .adaptive_search import (
    AdaptiveOperatorAttempt,
    AdaptiveSessionRecord,
    DEFAULT_ADAPTIVE_OPERATOR_PORTFOLIO,
    build_operator_session_request,
    build_adaptive_search_state,
    choose_adaptive_operator,
    select_fixed_cycle_operator,
    select_stateless_role_operator,
    _role_signals,
)
from .core import (
    run_student_assignment_operator_session_diagnostic,
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


def _role_pressure_facts(state, ranked_students, selected_student_ids=(), *, role_signals=None):
    """Capture existing opportunity signals without creating a new metric."""

    selected = set(int(student_id) for student_id in selected_student_ids)
    selected_pressure = {
        str(item.student_id): {
            "weighted_current_penalty": item.weighted_current_penalty,
            "opportunity_signal": item.opportunity_signal,
            "nonzero_component_count": item.nonzero_component_count,
            "sequence_opportunity_count": item.sequence_opportunity_count,
            "sequence_unsatisfied_count": item.sequence_unsatisfied_count,
        }
        for item in ranked_students
        if item.student_id in selected
    }
    return {
        "student_local_weighted_total": state.student_local_weighted_total,
        "highest_student_pressure": state.highest_student_pressure,
        "top_k_pressure": dict(state.top_k_pressure),
        "nonzero_pressure_student_count": state.nonzero_pressure_student_count,
        "student_local_weighted_share": state.student_local_weighted_share,
        "student_pressure_components": dict(state.student_pressure_components),
        "selected_student_ids": tuple(sorted(selected)),
        "selected_student_pressure": selected_pressure,
        "utilization_raw_penalty": state.utilization_raw_penalty,
        "utilization_normalized_value": state.utilization_normalized_value,
        "utilization_weighted_value": state.utilization_weighted_value,
        "global_utilization_weighted_share": state.global_utilization_weighted_share,
        "pressured_delivery_group_count": state.pressured_delivery_group_count,
        "top_utilization_group_share": state.top_utilization_group_share,
        "top_three_utilization_group_share": state.top_three_utilization_group_share,
        "top_five_utilization_group_share": state.top_five_utilization_group_share,
        "optimistic_utilization_leverage": state.optimistic_utilization_leverage,
        "useful_utilization_student_count": state.useful_utilization_student_count,
        "utilization_ranked_student_ids": tuple(state.utilization_ranked_student_ids),
        "role_signals": dict(role_signals or {}),
    }


def _attempt_exhaustion_classification(*, status, candidate_found, candidate_validated,
                                       validation_classification, stopping_reason,
                                       adopted):
    """Use only evidence exposed by the existing bounded probe."""

    if adopted:
        return "PRODUCTIVE"
    if (
        status == "unknown"
        or validation_classification in {"validation_unknown", "validation_error"}
        or (candidate_found and not candidate_validated)
    ):
        return "OPERATOR_UNRESOLVED"
    if status == "infeasible" or stopping_reason in {
        "proven_infeasible",
        "proven_scope_exhausted",
    }:
        return "EXACT_SCOPE_EXHAUSTED"
    return "OPERATOR_NON_IMPROVING"


def _role_exhaustion_classification(role_pressure):
    """Avoid inferring role exhaustion from one failed operator."""

    signals = role_pressure.get("role_signals", {})
    if any(float(value or 0) > 0 for value in signals.values()):
        return "ROLE_REMAINS_ACTIONABLE"
    return "ROLE_EXHAUSTION_NOT_PROVEN"


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


def _weighted_quality_value(quality, result):
    """Use the supplied DTO profile when valuing an incumbent.

    A detached source-decision incumbent can be reused under another v2
    counselor profile.  In that case the result object's solver components
    may describe the profile that originally created it, so policy state must
    use the freshly reconstructed quality facts instead.
    """

    components = (
        quality.get("objective_semantics", {}).get("components", {})
        if isinstance(quality, dict)
        else {}
    )
    weighted = [
        fact.get("weighted_normalized_contribution")
        for fact in components.values()
        if isinstance(fact, dict)
        and fact.get("weighted_normalized_contribution") is not None
    ]
    if weighted:
        return float(sum(float(value or 0) for value in weighted))
    return _weighted_substantive_value(result)


def _source_decisions_from_result(result):
    return tuple(
        (result.optimization_facts or {})
        .get("stage_2", {})
        .get("final_source_decisions", ())
    )


def _compact_inner_probe_summary(
    iteration, *, operator, target_scope, actual_target_scope, selected_grade,
    candidate_complete=False,
):
    """Keep bounded inner-probe facts in the durable policy record.

    The local bootstrap already records these facts on the engine result, but
    the calibration boundary previously retained only outer operator counts.
    Preserve the fields needed to compare policy productivity without copying
    candidate schedules or quality ledgers into every attempt.
    """

    candidate_value = iteration.get("candidate_value")
    incumbent_before = iteration.get("incumbent_before")
    return {
        "operator": operator,
        "iteration": iteration.get("iteration"),
        "attempt_number_for_radius": iteration.get("attempt_number_for_radius"),
        "radius": iteration.get("radius"),
        "effective_radius": iteration.get("effective_radius"),
        "target_scope": tuple(target_scope or ()),
        "actual_target_scope": tuple(
            iteration.get("selected_student_ids") or actual_target_scope or ()
        ),
        "selected_grade": iteration.get("selected_grade", selected_grade),
        "status": iteration.get("status"),
        "candidate_found": bool(iteration.get("candidate_found", candidate_value is not None)),
        "candidate_complete": bool(
            iteration.get("candidate_complete", candidate_complete)
        ),
        "candidate_validated": bool(iteration.get("candidate_validated", False)),
        "adopted": bool(iteration.get("adopted", False)),
        "validation_classification": iteration.get("validation_classification"),
        "candidate_substantive_value": candidate_value,
        "starting_incumbent_value": incumbent_before,
        "substantive_gain": (
            float(incumbent_before) - float(candidate_value)
            if incumbent_before is not None and candidate_value is not None
            else 0.0
        ),
        "candidate_source_decision_fingerprint": iteration.get(
            "candidate_source_decision_fingerprint"
        ),
        "elapsed_seconds": iteration.get("elapsed_seconds"),
        "solver_wall_time_seconds": iteration.get("solver_wall_time_seconds"),
        "validation_elapsed_seconds": iteration.get("validation_elapsed_seconds"),
        "branches": iteration.get("branches"),
        "conflicts": iteration.get("conflicts"),
        "best_bound": iteration.get("best_bound"),
        "model_variable_count": iteration.get("model_variable_count"),
        "model_constraint_count": iteration.get("model_constraint_count"),
        "changed_source_decision_count": iteration.get(
            "changed_source_decision_count", 0
        ),
        "changed_student_count": iteration.get("changed_student_count", 0),
        "component_deltas": dict(iteration.get("component_deltas") or {}),
        "affected_student_ids": tuple(iteration.get("affected_student_ids") or ()),
        "affected_section_ids": tuple(iteration.get("affected_section_ids") or ()),
        "stopping_reason": iteration.get("stopping_reason"),
    }


def _operator_result(data, spec, *, selected_student_ids, current_source_decisions,
                    time_limit_seconds, worker_count, collect_resource_telemetry,
                    session_overrides=None,
                    hard_feasibility_time_limit_seconds=None,
                    hard_feasibility_validation_time_limit_seconds=None,
                    hard_feasibility_worker_count=None,
                    hard_feasibility_validation_worker_count=None,
                    candidate_validation_time_limit_seconds=None,
                    cp_sat_random_seed=None,
                    cp_sat_max_deterministic_time_seconds=None,
                    phase_callback=None):
    # Every policy family uses the same reusable continuous-session boundary.
    # The policy spec describes the session granularity; the outer caller still
    # caps it by the remaining shared deadline.  This keeps multi-attempt
    # execution and validation semantics identical across adaptive and static
    # controls.
    override = dict((session_overrides or {}).get(spec.name, {}))
    effective_spec = replace(
        spec,
        session_time_limit_seconds=float(
            override.get("session_time_limit_seconds", spec.session_time_limit_seconds)
        ),
        session_max_attempts=int(
            override.get("session_max_attempts", spec.session_max_attempts)
        ),
        per_attempt_cp_sat_limit_seconds=float(
            override.get(
                "per_attempt_cp_sat_limit_seconds",
                spec.per_attempt_cp_sat_limit_seconds,
            )
        ),
    )
    request = build_operator_session_request(
        effective_spec,
        remaining_seconds=time_limit_seconds,
        worker_count=worker_count,
        selected_student_ids=selected_student_ids,
        cp_sat_random_seed=cp_sat_random_seed,
        cp_sat_max_deterministic_time_seconds=(
            cp_sat_max_deterministic_time_seconds
        ),
    )
    return run_student_assignment_operator_session_diagnostic(
        data,
        operator_family=spec.name,
        initial_source_decisions=current_source_decisions,
        total_time_limit_seconds=request["allocated_time_limit_seconds"],
        max_attempts=request["max_attempts"],
        per_attempt_time_limit_seconds=min(
            request["per_attempt_time_limit_seconds"],
            request["allocated_time_limit_seconds"],
        ),
        worker_count=request["worker_count"],
        target_policy=request["target_policy"],
        selected_student_ids=request["selected_student_ids"],
        selected_grade=request["selected_grade"],
        utilization_cluster_policy="interaction_aware",
        hard_feasibility_validation_time_limit_seconds=(
            hard_feasibility_validation_time_limit_seconds
        ),
        hard_feasibility_validation_worker_count=(
            hard_feasibility_validation_worker_count
        ),
        candidate_validation_time_limit_seconds=(
            candidate_validation_time_limit_seconds
        ),
        cp_sat_random_seed=cp_sat_random_seed,
        cp_sat_max_deterministic_time_seconds=(
            cp_sat_max_deterministic_time_seconds
        ),
        collect_resource_telemetry=collect_resource_telemetry,
        capture_final_source_decisions=True,
        phase_callback=phase_callback,
    )


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
    session_overrides=None,
    hard_feasibility_time_limit_seconds=None,
    hard_feasibility_validation_time_limit_seconds=None,
    hard_feasibility_worker_count=None,
    hard_feasibility_validation_worker_count=None,
    candidate_validation_time_limit_seconds=None,
    cp_sat_random_seed=None,
    cp_sat_max_deterministic_time_seconds=None,
    session_id=None,
    selection_policy="adaptive",
    fixed_cycle=(),
    phase_callback=None,
):
    """Run a diagnostic v2 operator session inside one shared wall-clock budget.

    ``selection_policy`` controls only which already-existing operator is
    selected for each iteration. ``adaptive`` is the allocator under study;
    ``stateless_role`` and ``fixed_cycle`` are matched solver controls. All
    policies use the same CP-SAT operator boundary, complete-result checks,
    full-model validation, and strict adoption rule. This function is not
    called by ``solve_student_assignment`` and is suitable for offline
    comparison only. A failed, unknown, partial, or unvalidated candidate is
    recorded and never replaces the current incumbent.
    """

    if data.objective_semantics_version != "v2":
        raise ValueError("adaptive local search requires Objective Semantics v2")
    if selection_policy not in {"adaptive", "stateless_role", "fixed_cycle"}:
        raise ValueError(
            "selection_policy must be adaptive, stateless_role, or fixed_cycle"
        )
    if selection_policy == "fixed_cycle" and not tuple(fixed_cycle):
        raise ValueError("fixed_cycle selection requires at least one operator")
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
    initial_components = dict(current_result.objective_components or {})
    phase_timings = {}

    def _emit_phase(phase, event="completed", **facts):
        """Publish optional live breadcrumbs without affecting the search."""

        if phase_callback is None:
            return
        try:
            phase_callback(str(phase), event=str(event), **facts)
        except Exception:
            # Calibration telemetry is observational. A broken status sink
            # must never change candidate selection or solver behavior.
            return

    def _record_phase(name, phase_started):
        elapsed = monotonic() - phase_started
        phase_timings[name] = phase_timings.get(name, 0.0) + elapsed
        _emit_phase(
            name,
            "completed",
            elapsed_seconds=elapsed,
            cumulative_seconds=phase_timings[name],
        )
        return elapsed

    phase_started = monotonic()
    _emit_phase("initial_quality_evaluation", "started")
    initial_quality = _quality_report(data, current_result)
    _record_phase("initial_quality_evaluation", phase_started)
    current_value = _weighted_quality_value(initial_quality, current_result)
    iteration_limit = (
        max(1, int(max_iterations)) if max_iterations is not None else None
    )
    stopping_reason = "shared_budget_exhausted"
    policy_selection_seconds = 0.0
    operator_execution_seconds = 0.0

    while monotonic() - started < configured_budget:
        if iteration_limit is not None and len(history) >= iteration_limit:
            stopping_reason = "iteration_budget_exhausted"
            break
        elapsed = monotonic() - started
        remaining = max(0.0, configured_budget - elapsed)
        phase_started = monotonic()
        _emit_phase(
            "policy_quality_evaluation",
            "started",
            iteration=len(history) + 1,
        )
        quality = _quality_report(data, current_result)
        _record_phase("policy_quality_evaluation", phase_started)
        phase_started = monotonic()
        _emit_phase(
            "target_preparation",
            "started",
            iteration=len(history) + 1,
        )
        ranked = rank_students_by_quality_pressure(data, quality)
        state = build_adaptive_search_state(
            data,
            quality,
            elapsed_seconds=elapsed,
            remaining_seconds=remaining,
            history=tuple(history),
            source_decisions=current_source_decisions,
            recent_memory_peak_bytes=int(
                (current_result.optimization_facts or {})
                .get("operation_resource_monitor", {})
                .get("peak_working_set_bytes", 0)
                or 0
            ),
        )
        _record_phase("target_preparation", phase_started)
        selection_started = monotonic()
        _emit_phase(
            "policy_selection",
            "started",
            iteration=len(history) + 1,
        )
        if selection_policy == "adaptive":
            decision = choose_adaptive_operator(
                state,
                portfolio=portfolio,
                ranked_students=ranked,
            )
        elif selection_policy == "stateless_role":
            decision = select_stateless_role_operator(
                state,
                portfolio=portfolio,
                ranked_students=ranked,
            )
        else:
            decision = select_fixed_cycle_operator(
                state,
                fixed_cycle,
                ranked_students=ranked,
            )
        selection_elapsed = monotonic() - selection_started
        policy_selection_seconds += selection_elapsed
        phase_timings["policy_selection"] = (
            phase_timings.get("policy_selection", 0.0) + selection_elapsed
        )
        _emit_phase(
            "policy_selection",
            "completed",
            elapsed_seconds=selection_elapsed,
            cumulative_seconds=phase_timings["policy_selection"],
            iteration=len(history) + 1,
        )
        if decision is None:
            stopping_reason = "no_eligible_operator"
            break
        decision_payload = decision.to_dict()
        decision_payload["selection_policy"] = selection_policy
        selected = tuple(decision.selected_student_ids)
        operation_limit = min(per_operator, remaining)
        effective_spec = replace(
            decision.operator,
            session_time_limit_seconds=float(
                dict((session_overrides or {}).get(decision.operator.name, {})).get(
                    "session_time_limit_seconds",
                    decision.operator.session_time_limit_seconds,
                )
            ),
            session_max_attempts=int(
                dict((session_overrides or {}).get(decision.operator.name, {})).get(
                    "session_max_attempts",
                    decision.operator.session_max_attempts,
                )
            ),
            per_attempt_cp_sat_limit_seconds=float(
                dict((session_overrides or {}).get(decision.operator.name, {})).get(
                    "per_attempt_cp_sat_limit_seconds",
                    decision.operator.per_attempt_cp_sat_limit_seconds,
                )
            ),
        )
        decision_payload["session_request"] = build_operator_session_request(
            effective_spec,
            remaining_seconds=operation_limit,
            worker_count=worker_count,
            selected_student_ids=selected,
            cp_sat_random_seed=cp_sat_random_seed,
            cp_sat_max_deterministic_time_seconds=(
                cp_sat_max_deterministic_time_seconds
            ),
        )
        role_pressure_before = _role_pressure_facts(
            state,
            ranked,
            selected,
            role_signals={
                **_role_signals(state),
                **dict(decision.signal_values.get("role_signals", {}) or {}),
            },
        )
        sequence_position = decision.signal_values.get("cycle_index")
        # Preserve the factual policy explanation in the live phase stream as
        # well as in the eventual result payload. A supervised worker can be
        # stopped before its final JSON is returned, so this compact event is
        # the durable record of why a role/operator was selected in that case.
        # It is observational and cannot affect the search.
        _emit_phase(
            "policy_decision",
            "completed",
            iteration=len(history) + 1,
            selection_policy=selection_policy,
            selected_role=decision.operator.portfolio_role,
            selected_operator=decision.operator.name,
            selected_student_ids=selected,
            selected_grade=decision.operator.selected_grade,
            reasons=tuple(decision.reasons),
            signal_values=dict(decision.signal_values),
        )
        decisions.append(decision_payload)
        operation_started = monotonic()
        _emit_phase(
            "operator_execution",
            "started",
            iteration=len(history) + 1,
            operator=decision.operator.name,
        )
        validation_error = None
        try:
            result = _operator_result(
                data,
                decision.operator,
                selected_student_ids=selected,
                current_source_decisions=current_source_decisions,
                time_limit_seconds=operation_limit,
                worker_count=worker_count,
                collect_resource_telemetry=collect_resource_telemetry,
                session_overrides=session_overrides,
                hard_feasibility_time_limit_seconds=hard_feasibility_time_limit_seconds,
                hard_feasibility_validation_time_limit_seconds=(
                    hard_feasibility_validation_time_limit_seconds
                ),
                hard_feasibility_worker_count=hard_feasibility_worker_count,
                hard_feasibility_validation_worker_count=(
                    hard_feasibility_validation_worker_count
                ),
                candidate_validation_time_limit_seconds=(
                    candidate_validation_time_limit_seconds
                ),
                cp_sat_random_seed=cp_sat_random_seed,
                cp_sat_max_deterministic_time_seconds=(
                    cp_sat_max_deterministic_time_seconds
                ),
                phase_callback=phase_callback,
            )
        except ValueError as exc:
            # An operator may be given too little of the shared budget to
            # validate its supplied incumbent. Treat that as a recorded
            # validation failure, not as a policy/runtime crash; the current
            # complete incumbent remains authoritative and adoptable.
            result = current_result
            validation_error = str(exc)
        operation_elapsed = monotonic() - operation_started
        operator_execution_seconds += operation_elapsed
        phase_timings["operator_execution"] = (
            phase_timings.get("operator_execution", 0.0) + operation_elapsed
        )
        _emit_phase(
            "operator_execution",
            "completed",
            elapsed_seconds=operation_elapsed,
            cumulative_seconds=phase_timings["operator_execution"],
            iteration=len(history) + 1,
            operator=decision.operator.name,
        )
        phase_started = monotonic()
        _emit_phase(
            "candidate_processing",
            "started",
            iteration=len(history) + 1,
        )
        local = dict((result.optimization_facts or {}).get("stage_2_local_bootstrap") or {})
        if validation_error is not None:
            local.update({
                "status": "validation_error",
                "candidate_found": False,
                "candidate_validated": False,
                "validation_classification": "validation_error",
                "validation_error": validation_error,
                "stopping_reason": "candidate_validation_error",
            })
        candidate_source = _source_decisions_from_result(result)
        candidate_quality = _quality_report(data, result)
        candidate_value = _weighted_quality_value(candidate_quality, result)
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
            after_state = build_adaptive_search_state(
                data,
                candidate_quality,
                elapsed_seconds=monotonic() - started,
                remaining_seconds=max(
                    0.0,
                    configured_budget - (monotonic() - started),
                ),
                history=tuple(history),
                source_decisions=current_source_decisions,
                recent_memory_peak_bytes=int(
                    (result.optimization_facts or {})
                    .get("operation_resource_monitor", {})
                    .get("peak_working_set_bytes", 0)
                    or 0
                ),
            )
            role_pressure_after = _role_pressure_facts(
                after_state,
                rank_students_by_quality_pressure(data, candidate_quality),
                selected,
                role_signals=_role_signals(after_state),
            )
            role_pressure_after["state_changed"] = True
        else:
            role_pressure_after = dict(role_pressure_before)
            role_pressure_after["state_changed"] = False
        _record_phase("candidate_processing", phase_started)
        stage_2_facts = dict((result.optimization_facts or {}).get("stage_2") or {})
        # These facts are observational breadcrumbs for the supervised
        # calibration worker.  They allow a validated improvement to be
        # persisted immediately, even if the outer worker is later stopped
        # before the policy returns its final JSON payload.
        _emit_phase(
            "candidate_processing",
            "completed",
            iteration=len(history) + 1,
            candidate_found=candidate_found,
            candidate_validated=candidate_validated,
            candidate_complete=hard_complete,
            adopted=adopted,
            candidate_substantive_value=(
                candidate_value if candidate_found else None
            ),
            candidate_source_decisions=(
                tuple(candidate_source) if candidate_found else ()
            ),
            candidate_objective_vector=tuple(
                stage_2_facts.get("objective_values") or ()
            ) if candidate_found else (),
            candidate_components=(
                dict(result.objective_components or {}) if candidate_found else {}
            ),
            candidate_assignment_count=(
                len(result.assignments) if candidate_found else 0
            ),
            candidate_unmet_count=(
                len(result.unmet_requests) if candidate_found else 0
            ),
            candidate_special_commitment_count=(
                len(result.commitment_assignments) if candidate_found else 0
            ),
        )
        status = str(local.get("status") or result.solver_outcome or "unknown")
        validation_classification = str(
            local.get("validation_classification", "not_attempted")
        )
        exhaustion_classification = _attempt_exhaustion_classification(
            status=status,
            candidate_found=candidate_found,
            candidate_validated=candidate_validated,
            validation_classification=validation_classification,
            stopping_reason=local.get("stopping_reason"),
            adopted=adopted,
        )
        role_exhaustion_classification = _role_exhaustion_classification(
            role_pressure_before
        )
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
                unknown=(
                    status == "unknown"
                    or local.get("validation_classification") == "validation_unknown"
                ),
                infeasible=status == "infeasible",
                stopping_reason=local.get("stopping_reason"),
                role_specific_gain=float(local.get("role_specific_gain", gain) or 0),
                validation_classification=validation_classification,
                validation_solver_outcome=local.get("validation_solver_outcome"),
                validation_error=local.get("validation_error"),
                target_scope=selected,
                actual_target_scope=tuple(
                    local.get("selected_student_ids") or selected
                ),
                candidate_source_decision_fingerprint=(
                    source_decision_fingerprint(candidate_source)
                    if candidate_source
                    else None
                ),
                selected_grade=decision.operator.selected_grade,
                utilization_cluster=tuple(
                    local.get("utilization_cluster", ()) or ()
                ),
                session_attempt_count=len(tuple(local.get("iterations", ()) or ())),
                session_adopted_count=sum(
                    bool(item.get("adopted"))
                    for item in tuple(local.get("iterations", ()) or ())
                ),
                session_requested_seconds=local.get(
                    "configured_session_budget_seconds"
                ),
                session_cp_sat_seconds=local.get("solver_wall_time_seconds"),
                session_validation_seconds=local.get(
                    "validation_elapsed_seconds"
                ),
                session_external_overrun_seconds=local.get(
                    "external_overrun_seconds"
                ),
                cp_sat_random_seed=local.get("cp_sat_random_seed"),
                cp_sat_max_deterministic_time_seconds=(
                    local.get("cp_sat_max_deterministic_time_seconds")
                ),
                inner_probe_summaries=tuple(
                    _compact_inner_probe_summary(
                        item,
                        operator=decision.operator.name,
                        target_scope=selected,
                        actual_target_scope=tuple(
                            local.get("selected_student_ids") or selected
                        ),
                        selected_grade=decision.operator.selected_grade,
                        candidate_complete=hard_complete,
                    )
                    for item in tuple(local.get("iterations", ()) or ())
                ),
                role_pressure_before=role_pressure_before,
                role_pressure_after=role_pressure_after,
                exhaustion_classification=exhaustion_classification,
                role_exhaustion_classification=role_exhaustion_classification,
                sequence_position=(
                    int(sequence_position)
                    if sequence_position is not None
                    else None
                ),
            )
        )
        if monotonic() - started >= configured_budget:
            stopping_reason = "shared_budget_exhausted"

    if stopping_reason == "validated_improvement_adopted" and monotonic() - started >= configured_budget:
        stopping_reason = "shared_budget_exhausted"
    finalization_started = monotonic()
    _emit_phase("finalization", "started")
    final_components = dict(current_result.objective_components or {})
    final_quality = _quality_report(data, current_result)
    finalization_seconds = monotonic() - finalization_started
    phase_timings["finalization"] = finalization_seconds
    _emit_phase(
        "finalization",
        "completed",
        elapsed_seconds=finalization_seconds,
        cumulative_seconds=finalization_seconds,
    )
    elapsed_seconds = monotonic() - started
    phase_timings["total"] = elapsed_seconds
    _emit_phase("total", "completed", elapsed_seconds=elapsed_seconds)
    record = AdaptiveSessionRecord(
        session_id=str(session_id or uuid4()),
        policy_version=state.policy_version if "state" in locals() else "v2-local-allocator-diagnostic-2",
        input_fingerprint=semantic_student_assignment_input_fingerprint(data),
        source_seed_fingerprint=source_decision_fingerprint(initial_source_decisions or _source_decisions_from_result(initial_result)),
        objective_semantics_version=data.objective_semantics_version,
        counselor_scores=dict(data.objective_importance_scores),
        configured_budget_seconds=configured_budget,
        elapsed_seconds=elapsed_seconds,
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
        selection_policy=selection_policy,
        policy_selection_seconds=policy_selection_seconds,
        operator_execution_seconds=operator_execution_seconds,
        finalization_seconds=finalization_seconds,
        external_overrun_seconds=max(
            0.0,
            elapsed_seconds - configured_budget,
        ),
        cp_sat_random_seed=(
            int(cp_sat_random_seed) if cp_sat_random_seed is not None else None
        ),
        cp_sat_max_deterministic_time_seconds=(
            cp_sat_max_deterministic_time_seconds
        ),
        phase_timings=dict(phase_timings),
    )
    return AdaptiveSessionResult(
        record=record,
        result=current_result,
        source_decisions=current_source_decisions,
    )


__all__ = ["AdaptiveSessionResult", "run_adaptive_local_search_diagnostic"]
