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
    ALL_ADAPTIVE_POLICY_VARIANTS,
    ADAPTIVE_NEW_POLICY_VERSIONS,
    AdaptiveOperatorAttempt,
    AdaptiveSessionRecord,
    DEFAULT_ADAPTIVE_OPERATOR_PORTFOLIO,
    build_operator_session_request,
    build_adaptive_search_state,
    choose_adaptive_operator,
    select_fixed_cycle_operator,
    select_stateless_role_operator,
    _role_signals,
    operator_family,
)
from .core import (
    run_student_assignment_operator_session_diagnostic,
    run_student_assignment_source_decision_validation_diagnostic,
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


def _canonical_student_scope(student_ids):
    """Return the stable semantic representation of a student scope.

    Operator sessions treat targeted IDs as a set and normalize fixed scopes
    with ``sorted(set(...), key=repr)``.  Policy records used to retain the
    selector's ranking order, which made an unchanged scope look different in
    telemetry even though the executed set was identical.  Scope order has no
    scheduling meaning, so canonicalize it at the diagnostic boundary.
    """

    return tuple(sorted(set(tuple(student_ids or ())), key=repr))


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
        or validation_classification in {
            "validation_unknown", "validation_error", "scope_mismatch"
        }
        or (candidate_found and not candidate_validated)
    ):
        return "OPERATOR_UNRESOLVED"
    if status == "infeasible" or stopping_reason in {
        "proven_infeasible",
        "proven_scope_exhausted",
    }:
        return "EXACT_SCOPE_EXHAUSTED"
    return "OPERATOR_NON_IMPROVING"


def _role_exhaustion_classification(role_pressure, role=None):
    """Avoid inferring role exhaustion from one failed operator."""

    signals = role_pressure.get("role_signals", {})
    if role is not None and float(signals.get(role, 0) or 0) > 0:
        return "ROLE_REMAINS_ACTIONABLE"
    if role is None and any(float(value or 0) > 0 for value in signals.values()):
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


def _effective_operator_spec(spec, session_overrides=None):
    """Apply diagnostic session limits before policy costing or execution."""

    override = dict((session_overrides or {}).get(spec.name, {}))
    return replace(
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


def _selector_portfolio_with_effective_limits(portfolio, session_overrides=None):
    """Give adaptive selection the same limits the runtime will execute."""

    return tuple(
        _effective_operator_spec(spec, session_overrides)
        for spec in tuple(portfolio)
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


def _compact_objective_snapshot(quality, result, *, source_fingerprint=None):
    """Capture bounded authoritative v2 quality facts for a trajectory."""

    objective = dict((quality or {}).get("objective_semantics") or {})
    components = {}
    for name, facts in dict(objective.get("components") or {}).items():
        if not isinstance(facts, dict):
            continue
        components[name] = {
            key: facts.get(key)
            for key in (
                "raw_penalty",
                "denominator",
                "normalized_penalty",
                "importance_score",
                "weighted_normalized_contribution",
            )
        }
    fulfillment = dict((quality or {}).get("request_fulfillment") or {})
    compact_fulfillment = {
        key: dict(fulfillment.get(key) or {})
        for key in (
            "solver_aligned_counts",
            "eligible_counts",
            "unmet_counts",
        )
    }
    special = dict(fulfillment.get("special_commitments") or {})
    compact_fulfillment["special_commitments"] = {
        key: special.get(key)
        for key in ("requested_count", "fulfilled_count", "unmet_count")
    }
    return {
        "schema": "adaptive_objective_snapshot_v1",
        "source_fingerprint": source_fingerprint,
        "objective_semantics_version": objective.get("version"),
        "components": components,
        "fulfillment": compact_fulfillment,
        "assignment_count": len(tuple(getattr(result, "assignments", ()) or ())),
        "unmet_request_count": len(tuple(getattr(result, "unmet_requests", ()) or ())),
        "special_commitment_count": len(
            tuple(getattr(result, "commitment_assignments", ()) or ())
        ),
        "weighted_substantive_value": _weighted_quality_value(quality, result),
    }


def _objective_delta(before, after, field):
    names = set(before.get("components", {})) | set(after.get("components", {}))
    return {
        name: float(
            (after.get("components", {}).get(name, {}).get(field) or 0)
            - (before.get("components", {}).get(name, {}).get(field) or 0)
        )
        for name in sorted(names)
    }


def _source_decisions_from_result(result):
    return tuple(
        (result.optimization_facts or {})
        .get("stage_2", {})
        .get("final_source_decisions", ())
    )


def _candidate_source_decisions_from_local(local):
    """Recover a non-authoritative probe candidate for validation retry."""

    for iteration in reversed(tuple(local.get("iterations", ()) or ())):
        candidate = tuple(iteration.get("candidate_source_decisions") or ())
        if candidate:
            return candidate
    return ()


def _retry_candidate_validation(
    data,
    *,
    candidate_source_decisions,
    time_limit_seconds,
    worker_count,
    random_seed=None,
):
    """Retry only an inconclusive validation, never candidate generation.

    The returned result is usable as an incumbent only when the existing full
    source-decision validation boundary reports a complete matching solution.
    """

    started = monotonic()
    result = run_student_assignment_source_decision_validation_diagnostic(
        data,
        source_decisions=tuple(candidate_source_decisions),
        time_limit_seconds=float(time_limit_seconds),
        worker_count=int(worker_count),
        capture_final_source_decisions=True,
        collect_resource_telemetry=False,
    )
    stage_2 = dict((result.optimization_facts or {}).get("stage_2") or {})
    validated_source = tuple(stage_2.get("final_source_decisions") or ())
    validated = bool(
        result.status == "complete"
        and not result.unmet_requests
        and stage_2.get("alternate_seed_validated")
        and validated_source == tuple(candidate_source_decisions)
    )
    return {
        "result": result,
        "validated": validated,
        "classification": "validated" if validated else (
            "validation_unknown"
            if str(result.solver_outcome).lower() == "unknown"
            else "validation_error"
        ),
        "solver_outcome": result.solver_outcome,
        "elapsed_seconds": monotonic() - started,
        "source_decision_identity_matches": (
            validated_source == tuple(candidate_source_decisions)
            if validated_source
            else False
        ),
        "source_decision_count": len(validated_source),
        "requested_time_limit_seconds": float(time_limit_seconds),
        "cp_sat_random_seed": random_seed,
    }


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
    canonical_target_scope = _canonical_student_scope(target_scope)
    canonical_actual_target_scope = _canonical_student_scope(
        iteration.get("probe_invocation_student_ids")
        if iteration.get("probe_invocation_student_ids") is not None
        else iteration.get("selected_student_ids") or actual_target_scope or ()
    )
    return {
        "operator": operator,
        "iteration": iteration.get("iteration"),
        "attempt_number_for_radius": iteration.get("attempt_number_for_radius"),
        "radius": iteration.get("radius"),
        "effective_radius": iteration.get("effective_radius"),
        "target_scope": canonical_target_scope,
        "actual_target_scope": canonical_actual_target_scope,
        "scope_equal": canonical_target_scope == canonical_actual_target_scope,
        "enforced_student_scope": _canonical_student_scope(
            iteration.get("enforced_student_scope") or ()
        ),
        "probe_selected_student_ids": _canonical_student_scope(
            iteration.get("probe_selected_student_ids") or ()
        ),
        "probe_invocation_student_ids": _canonical_student_scope(
            iteration.get("probe_invocation_student_ids") or ()
        ),
        "probe_result_student_ids": _canonical_student_scope(
            iteration.get("probe_result_student_ids") or ()
        ),
        "probe_result_scope_equal": iteration.get("probe_result_scope_equal"),
        "scope_contract_expected_student_ids": _canonical_student_scope(
            iteration.get("scope_contract_expected_student_ids") or ()
        ),
        "scope_boundary_trace": tuple(
            dict(item) for item in (iteration.get("scope_boundary_trace") or ())
        ),
        "scope_source": iteration.get("scope_source"),
        "scope_mismatch": bool(iteration.get("scope_mismatch", False)),
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
        "candidate_components": dict(
            iteration.get("component_values")
            or iteration.get("candidate_components")
            or {}
        ),
        "elapsed_seconds": iteration.get("elapsed_seconds"),
        "solver_wall_time_seconds": iteration.get("solver_wall_time_seconds"),
        "validation_elapsed_seconds": iteration.get("validation_elapsed_seconds"),
        "validation_requested_time_limit_seconds": iteration.get(
            "validation_requested_time_limit_seconds"
        ),
        "validation_effective_time_limit_seconds": iteration.get(
            "validation_effective_time_limit_seconds"
        ),
        "validation_truncation_reason": iteration.get(
            "validation_truncation_reason"
        ),
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
                    diagnostic_parent_hard_wall_deadline_monotonic=None,
                    phase_callback=None,
                    trusted_branch_context=None,
                    validated_branch_context_callback=None):
    # Every policy family uses the same reusable continuous-session boundary.
    # The policy spec describes the session granularity; the outer caller still
    # caps it by the remaining shared deadline.  This keeps multi-attempt
    # execution and validation semantics identical across adaptive and static
    # controls.
    effective_spec = _effective_operator_spec(spec, session_overrides)
    # Keep one immutable contract value for the complete handoff.  The
    # selector owns this scope; no later request-building step may derive it
    # again from mutable policy state or operator guidance.
    enforced_scope = _canonical_student_scope(selected_student_ids)
    # The outer adaptive selector is authoritative for the diagnostic target
    # scope.  Dynamic target selection remains available to callers that do
    # not provide an explicit scope, but must not silently replace a scope
    # that was already selected and recorded by the policy.
    if enforced_scope and effective_spec.target_policy == "dynamic":
        effective_spec = replace(effective_spec, target_policy="fixed")
    request = build_operator_session_request(
        effective_spec,
        remaining_seconds=time_limit_seconds,
        worker_count=worker_count,
        selected_student_ids=enforced_scope,
        enforced_student_scope=enforced_scope,
        cp_sat_random_seed=cp_sat_random_seed,
        cp_sat_max_deterministic_time_seconds=(
            cp_sat_max_deterministic_time_seconds
        ),
    )
    if enforced_scope and (
        _canonical_student_scope(request["selected_student_ids"])
        != enforced_scope
        or _canonical_student_scope(request["enforced_student_scope"])
        != enforced_scope
    ):
        raise ValueError(
            "operator session request changed the selector-owned scope"
        )
    if phase_callback is not None:
        phase_callback(
            "scope_contract",
            event="completed",
            operator=spec.name,
            selected_student_ids=enforced_scope,
            request_selected_student_ids=_canonical_student_scope(
                request["selected_student_ids"]
            ),
            request_enforced_student_scope=_canonical_student_scope(
                request["enforced_student_scope"]
            ),
            target_policy=request["target_policy"],
            contract_match=True,
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
        selected_student_ids=enforced_scope,
        enforced_student_scope=enforced_scope,
        scope_contract_expected_student_ids=enforced_scope,
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
        diagnostic_parent_hard_wall_deadline_monotonic=(
            diagnostic_parent_hard_wall_deadline_monotonic
        ),
        collect_resource_telemetry=collect_resource_telemetry,
        capture_final_source_decisions=True,
        phase_callback=phase_callback,
        _trusted_branch_context=trusted_branch_context,
        _validated_branch_context_callback=validated_branch_context_callback,
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
    adaptive_policy_variant="balanced",
    phase_callback=None,
    use_trusted_branch_context=False,
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
    if selection_policy == "adaptive" and adaptive_policy_variant not in ALL_ADAPTIVE_POLICY_VARIANTS:
        raise ValueError(
            "adaptive_policy_variant must be balanced, "
            "student_pressure_biased, utilization_biased, evidence_guided, "
            "r4_anchor, hierarchical_evidence, hierarchical_recent, "
            "component_aware, or horizon_aware"
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
    initial_source_fingerprint = source_decision_fingerprint(
        current_source_decisions
    )
    initial_objective_snapshot = _compact_objective_snapshot(
        initial_quality,
        current_result,
        source_fingerprint=initial_source_fingerprint,
    )
    objective_transitions = []
    iteration_limit = (
        max(1, int(max_iterations)) if max_iterations is not None else None
    )
    stopping_reason = "shared_budget_exhausted"
    policy_selection_seconds = 0.0
    operator_execution_seconds = 0.0
    trusted_branch_context = None
    # The core diagnostic operator already publishes exception-safe phase
    # callbacks. Aggregate those observations here so research artifacts can
    # separate model/solve/validation work from the enclosing operator wall
    # time without changing the operator request or authority boundary.
    engine_phase_timings = {}
    engine_phase_starts = {}

    def _observe_engine_phase(phase, event="completed", **facts):
        phase = str(phase)
        event = str(event)
        if event == "started":
            engine_phase_starts.setdefault(phase, []).append(monotonic())
        elif event == "completed":
            elapsed = facts.get("elapsed_seconds")
            if elapsed is None:
                starts = engine_phase_starts.get(phase) or []
                elapsed = monotonic() - starts.pop() if starts else 0.0
            else:
                starts = engine_phase_starts.get(phase) or []
                if starts:
                    starts.pop()
            try:
                elapsed = max(0.0, float(elapsed))
            except (TypeError, ValueError):
                elapsed = 0.0
            engine_phase_timings[phase] = (
                engine_phase_timings.get(phase, 0.0) + elapsed
            )
        # This is telemetry only. A broken downstream status sink must not
        # affect the solver, validation, or policy selection.
        if phase_callback is not None:
            try:
                phase_callback(phase, event=event, **facts)
            except Exception:
                pass

    def _operator_phase_callback(phase, event="completed", **facts):
        _observe_engine_phase(phase, event=event, **facts)

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
            current_source_fingerprint=source_decision_fingerprint(
                current_source_decisions
            ),
            candidate_validation_time_limit_seconds=(
                candidate_validation_time_limit_seconds
            ),
            current_objective_vector=tuple(
                (current_result.optimization_facts or {})
                .get("stage_2", {})
                .get("objective_values", ())
                or ()
            ),
        )
        _record_phase("target_preparation", phase_started)
        selection_started = monotonic()
        _emit_phase(
            "policy_selection",
            "started",
            iteration=len(history) + 1,
        )
        selection_portfolio = _selector_portfolio_with_effective_limits(
            portfolio,
            session_overrides,
        )
        if selection_policy == "adaptive":
            decision = choose_adaptive_operator(
                state,
                portfolio=selection_portfolio,
                ranked_students=ranked,
                adaptive_policy_variant=adaptive_policy_variant,
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
        decision_payload["adaptive_policy_variant"] = (
            adaptive_policy_variant if selection_policy == "adaptive" else "balanced"
        )
        # Student target scopes are semantic sets.  Canonicalize the selector
        # output before passing it to the operator so the request and the
        # operator-session telemetry use the same representation.
        selected = _canonical_student_scope(decision.selected_student_ids)
        operation_limit = min(per_operator, remaining)
        effective_spec = _effective_operator_spec(
            decision.operator,
            session_overrides,
        )
        if selected and effective_spec.target_policy == "dynamic":
            effective_spec = replace(effective_spec, target_policy="fixed")
        decision_payload["session_request"] = build_operator_session_request(
            effective_spec,
            remaining_seconds=operation_limit,
            worker_count=worker_count,
            selected_student_ids=selected,
            enforced_student_scope=selected,
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
            adaptive_policy_variant=(
                adaptive_policy_variant if selection_policy == "adaptive" else "balanced"
            ),
            selected_role=decision.operator.portfolio_role,
            selected_operator=decision.operator.name,
            selected_student_ids=selected,
            selected_grade=decision.operator.selected_grade,
            reasons=tuple(decision.reasons),
            signal_values=dict(decision.signal_values),
        )
        decisions.append(decision_payload)
        context_holder = {"context": trusted_branch_context}

        def _capture_trusted_branch_context(context):
            context_holder["context"] = context

        operation_started = monotonic()
        source_fingerprint_before = source_decision_fingerprint(
            current_source_decisions
        )
        _emit_phase(
            "operator_execution",
            "started",
            iteration=len(history) + 1,
            operator=decision.operator.name,
        )
        validation_error = None
        validation_error_facts = {}
        operator_execution_error = None
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
                diagnostic_parent_hard_wall_deadline_monotonic=(
                    started + configured_budget
                ),
                phase_callback=_operator_phase_callback,
                trusted_branch_context=(
                    trusted_branch_context
                    if use_trusted_branch_context
                    else None
                ),
                validated_branch_context_callback=(
                    _capture_trusted_branch_context
                    if use_trusted_branch_context
                    else None
                ),
            )
        except ValueError as exc:
            # An operator may be given too little of the shared budget to
            # validate its supplied incumbent. Treat that as a recorded
            # validation failure, not as a policy/runtime crash; the current
            # complete incumbent remains authoritative and adoptable.
            result = current_result
            operator_execution_error = str(exc)
            validation_error = operator_execution_error
        if use_trusted_branch_context:
            trusted_branch_context = context_holder["context"]
            if operator_execution_error is not None:
                validation_error_facts = dict(
                    getattr(exc, "validation_facts", {}) or {}
                )
                validation_error_facts["hierarchical_phase_timing_v1"] = dict(
                    getattr(exc, "hierarchical_timing", {}) or {}
                )
                validation_error_facts["exception_type"] = type(exc).__name__
                validation_error_facts["exception_message"] = str(exc)
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
        # If the operator failed before returning a new session result, do not
        # inspect the retained incumbent's previous local-bootstrap facts as
        # though they were produced by this attempt.  Doing so can report a
        # stale prior scope as the current attempt's executed scope.
        local = (
            {}
            if operator_execution_error is not None
            else dict(
                (result.optimization_facts or {}).get(
                    "stage_2_local_bootstrap"
                )
                or {}
            )
        )
        if validation_error is not None:
            local.update({
                "status": "validation_error",
                "candidate_found": False,
                "candidate_validated": False,
                "validation_classification": "validation_error",
                "validation_error": validation_error,
                "stopping_reason": "candidate_validation_error",
                "validation_error_facts": dict(
                    locals().get("validation_error_facts", {}) or {}
                ),
            })
        candidate_found = bool(local.get("candidate_found", False))
        candidate_validated = bool(local.get("candidate_validated", False))
        probe_iterations = tuple(local.get("iterations", ()) or ())
        probe_candidate_source = _candidate_source_decisions_from_local(local)
        candidate_source = (
            probe_candidate_source
            if candidate_found and probe_candidate_source
            else _source_decisions_from_result(result)
        )
        execution_scope_facts = tuple(
            (
                _canonical_student_scope(
                    item.get("probe_invocation_student_ids")
                )
                if item.get("probe_invocation_student_ids") is not None
                else None,
                _canonical_student_scope(item.get("selected_student_ids")),
            )
            for item in probe_iterations
        )
        executed_scopes = tuple(
            invocation_scope
            for invocation_scope, _ in execution_scope_facts
            if invocation_scope is not None
        )
        missing_execution_scope = bool(
            selected
            and (
                not probe_iterations
                or any(invocation_scope is None for invocation_scope, _ in execution_scope_facts)
            )
        )
        actual_target_scope = (
            executed_scopes[-1]
            if executed_scopes
            else None
        )
        scope_equal = bool(
            not selected
            or (
                not missing_execution_scope
                and bool(executed_scopes)
                and all(scope == selected for scope in executed_scopes)
                and all(
                    item.get("probe_result_scope_equal") is not False
                    for item in probe_iterations
                )
            )
        )
        scope_mismatch = bool(
            selected
            and operator_execution_error is None
            and (missing_execution_scope or not scope_equal)
        )
        if scope_mismatch:
            # This is a defense-in-depth authority check.  The core session
            # also guards selector-owned scopes, but the outer allocator must
            # remain fail-closed if a legacy or mocked operator returns a
            # different executed scope.
            local["scope_mismatch"] = True
            local["validation_classification"] = "scope_mismatch"
            local["validation_error"] = (
                "selector-owned scope execution evidence was missing or did not match"
            )
            candidate_validated = False
        probe_candidate_value = next(
            (
                item.get("candidate_value")
                for item in reversed(probe_iterations)
                if item.get("candidate_value") is not None
            ),
            None,
        )
        validation_retry = None
        retry_remaining_seconds = max(
            0.0, configured_budget - (monotonic() - started)
        )
        retry_eligible = bool(
            candidate_found
            and not candidate_validated
            and candidate_source
            and local.get("validation_classification") == "validation_unknown"
            and candidate_validation_time_limit_seconds is not None
            and float(candidate_validation_time_limit_seconds) > 0
            and retry_remaining_seconds > 0.001
        )
        validation_retry_facts = {
            "eligible": retry_eligible,
            "count": 0,
            "requested_limit_seconds": (
                float(candidate_validation_time_limit_seconds)
                if candidate_validation_time_limit_seconds is not None
                else None
            ),
            "effective_limit_seconds": None,
            "outcome": "not_attempted",
            "ordinary_rescore_fallback": False,
        }
        if retry_eligible:
            # A complete candidate that ran out of validation time is retained
            # as pending evidence only.  Retry the existing authoritative
            # source-decision validation boundary; never use this candidate as
            # an incumbent until that retry accepts the same source decisions.
            validation_retry_limit = min(
                float(candidate_validation_time_limit_seconds),
                retry_remaining_seconds,
            )
            validation_retry = _retry_candidate_validation(
                data,
                candidate_source_decisions=candidate_source,
                time_limit_seconds=validation_retry_limit,
                worker_count=(
                    hard_feasibility_validation_worker_count
                    if hard_feasibility_validation_worker_count is not None
                    else 1
                ),
                random_seed=cp_sat_random_seed,
            )
            validation_retry_facts.update({
                "count": 1,
                "effective_limit_seconds": validation_retry_limit,
                "outcome": validation_retry.get("classification"),
            })
            local["validation_retry"] = {
                key: value
                for key, value in validation_retry.items()
                if key != "result"
            }
            local["validation_elapsed_seconds"] = float(
                local.get("validation_elapsed_seconds", 0.0) or 0.0
            ) + float(validation_retry["elapsed_seconds"])
            local["validation_classification"] = validation_retry[
                "classification"
            ]
            local["validation_solver_outcome"] = validation_retry[
                "solver_outcome"
            ]
            local["validation_error"] = None
            candidate_validated = bool(validation_retry["validated"])
            if candidate_validated:
                result = validation_retry["result"]
        candidate_quality = _quality_report(data, result)
        candidate_value = (
            float(probe_candidate_value)
            if candidate_found and probe_candidate_value is not None
            else _weighted_quality_value(candidate_quality, result)
        )
        candidate_discovery_gain = (
            max(0.0, current_value - candidate_value)
            if candidate_found and candidate_value is not None
            else 0.0
        )
        hard_complete = result.status == "complete" and not result.unmet_requests
        adopted = bool(
            hard_complete
            and candidate_found
            and candidate_validated
            and candidate_source
            and candidate_value < current_value
        )
        gain = max(0.0, current_value - candidate_value) if adopted else 0.0
        validation_classification = str(
            local.get("validation_classification", "not_attempted")
        )
        objective_weighted_delta = {}
        objective_normalized_delta = {}
        objective_improvement_weighted_delta = {}
        if adopted:
            before_objective_snapshot = _compact_objective_snapshot(
                quality,
                current_result,
                source_fingerprint=source_fingerprint_before,
            )
            after_source_fingerprint = source_decision_fingerprint(candidate_source)
            after_objective_snapshot = _compact_objective_snapshot(
                candidate_quality,
                result,
                source_fingerprint=after_source_fingerprint,
            )
            objective_weighted_delta = _objective_delta(
                before_objective_snapshot,
                after_objective_snapshot,
                "weighted_normalized_contribution",
            )
            objective_normalized_delta = _objective_delta(
                before_objective_snapshot,
                after_objective_snapshot,
                "normalized_penalty",
            )
            objective_improvement_weighted_delta = {
                name: -value for name, value in objective_weighted_delta.items()
            }
            objective_transitions.append({
                "schema": "adaptive_objective_transition_v1",
                "attempt_index": len(history) + 1,
                "decision_index": len(decisions),
                "before": before_objective_snapshot,
                "after": after_objective_snapshot,
                "raw_delta": _objective_delta(
                    before_objective_snapshot, after_objective_snapshot, "raw_penalty"
                ),
                "normalized_delta": dict(objective_normalized_delta),
                "weighted_delta": dict(objective_weighted_delta),
                "improvement_delta": {
                    name: value
                    for name, value in objective_improvement_weighted_delta.items()
                },
                "validated_gain": gain,
                "validation_classification": validation_classification,
            })
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
                current_source_fingerprint=source_decision_fingerprint(
                    current_source_decisions
                ),
                candidate_validation_time_limit_seconds=(
                    candidate_validation_time_limit_seconds
                ),
                current_objective_vector=tuple(
                    (current_result.optimization_facts or {})
                    .get("stage_2", {})
                    .get("objective_values", ())
                    or ()
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
            role_pressure_before,
            decision.operator.portfolio_role,
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
                    or local.get("validation_classification") in {
                        "validation_unknown", "scope_mismatch"
                    }
                ),
                infeasible=status == "infeasible",
                stopping_reason=local.get("stopping_reason"),
                role_specific_gain=float(local.get("role_specific_gain", gain) or 0),
                validation_classification=validation_classification,
                validation_solver_outcome=local.get("validation_solver_outcome"),
                validation_error=local.get("validation_error"),
                target_scope=selected,
                actual_target_scope=actual_target_scope,
                scope_equal=scope_equal,
                scope_mismatch=scope_mismatch,
                source_fingerprint_before=source_fingerprint_before,
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
                        actual_target_scope=actual_target_scope,
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
                operator_family=operator_family(decision.operator),
                candidate_discovery_gain=candidate_discovery_gain,
                search_unknown=(status == "unknown"),
                validation_retry_count=(1 if validation_retry is not None else 0),
                validation_retry_facts=dict(validation_retry_facts),
                objective_weighted_delta=dict(objective_weighted_delta),
                objective_normalized_delta=dict(objective_normalized_delta),
                objective_improvement_weighted_delta=dict(
                    objective_improvement_weighted_delta
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
    final_objective_snapshot = _compact_objective_snapshot(
        final_quality,
        current_result,
        source_fingerprint=source_decision_fingerprint(current_source_decisions),
    )
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
    phase_timings["engine"] = dict(engine_phase_timings)
    phase_timings["engine_incomplete_phases"] = tuple(
        sorted(name for name, starts in engine_phase_starts.items() if starts)
    )
    _emit_phase("total", "completed", elapsed_seconds=elapsed_seconds)
    record = AdaptiveSessionRecord(
        session_id=str(session_id or uuid4()),
        policy_version=(
            ADAPTIVE_NEW_POLICY_VERSIONS.get(
                adaptive_policy_variant,
                state.policy_version if "state" in locals()
                else "v2-local-allocator-diagnostic-3",
            )
            if selection_policy == "adaptive"
            else (state.policy_version if "state" in locals() else "v2-local-allocator-diagnostic-3")
        ),
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
        adaptive_policy_variant=(
            adaptive_policy_variant if selection_policy == "adaptive" else "balanced"
        ),
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
        objective_trajectory={
            "schema": "adaptive_objective_trajectory_v1",
            "initial": initial_objective_snapshot,
            "adopted_transitions": tuple(objective_transitions),
            "final": final_objective_snapshot,
        },
    )
    return AdaptiveSessionResult(
        record=record,
        result=current_result,
        source_decisions=current_source_decisions,
    )


__all__ = ["AdaptiveSessionResult", "run_adaptive_local_search_diagnostic"]
