"""Diagnostic Objective Semantics v2 adaptive local-search policy.

This module owns policy state and operator selection only. It never imports
Django, constructs CP-SAT models, validates candidates, or authorizes a
schedule. The diagnostic runner in ``adaptive_runtime.py`` executes the
selected existing operators and remains subject to CP-SAT plus full-model
validation.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json

from .grade_guidance import build_grade_opportunity_facts
from .search_guidance import rank_students_by_quality_pressure
from .utilization_guidance import build_utilization_cluster_guidance


ADAPTIVE_POLICY_VERSION = "v2-local-allocator-diagnostic-2"


@dataclass(frozen=True)
class AdaptiveOperatorSpec:
    """One existing local operator exposed to diagnostic policy selection."""

    name: str
    radius: int | None
    max_changed_students: int | None
    targeted: bool
    student_count: int
    portfolio_role: str
    session_time_limit_seconds: float = 60.0
    session_max_attempts: int = 4
    per_attempt_cp_sat_limit_seconds: float = 20.0
    target_policy: str = "dynamic"
    selected_grade: int | None = None


DEFAULT_ADAPTIVE_OPERATOR_PORTFOLIO = (
    AdaptiveOperatorSpec("r2", 2, None, False, 0, "local_descent"),
    AdaptiveOperatorSpec("targeted_r4_s1", 4, 1, True, 1, "targeted_repair"),
    AdaptiveOperatorSpec("targeted_r8_s1", 8, 1, True, 1, "targeted_repair"),
    AdaptiveOperatorSpec("targeted_r4_s2", 4, 2, True, 2, "targeted_repair"),
    AdaptiveOperatorSpec("targeted_r8_s2", 8, 2, True, 2, "targeted_repair"),
    AdaptiveOperatorSpec("targeted_utilization_r16_s2", 16, 2, True, 2, "utilization_repair"),
    AdaptiveOperatorSpec("targeted_utilization_r16_s4", 16, 4, True, 4, "utilization_repair"),
    AdaptiveOperatorSpec("targeted_utilization_r32_s4", 32, 4, True, 4, "utilization_repair"),
    AdaptiveOperatorSpec("targeted_utilization_r32_s6", 32, 6, True, 6, "utilization_repair"),
    AdaptiveOperatorSpec("targeted_utilization_r64_s6", 64, 6, True, 6, "utilization_repair"),
    AdaptiveOperatorSpec("targeted_utilization_r64_s8", 64, 8, True, 8, "utilization_repair"),
    AdaptiveOperatorSpec("targeted_utilization_r64_s10", 64, 10, True, 10, "utilization_repair"),
    AdaptiveOperatorSpec("grade_bounded_g9", None, None, False, 0, "basin_escape", target_policy="fixed", selected_grade=9),
    AdaptiveOperatorSpec("grade_bounded_g10", None, None, False, 0, "basin_escape", target_policy="fixed", selected_grade=10),
    AdaptiveOperatorSpec("grade_bounded_g11", None, None, False, 0, "basin_escape", target_policy="fixed", selected_grade=11),
    AdaptiveOperatorSpec("grade_bounded_g12", None, None, False, 0, "basin_escape", target_policy="fixed", selected_grade=12),
)


@dataclass(frozen=True)
class AdaptiveOperatorAttempt:
    """Immutable evidence from one already-completed operator attempt."""

    operator: str
    status: str
    candidate_found: bool
    candidate_validated: bool
    adopted: bool
    gain: float
    elapsed_seconds: float
    solver_wall_time_seconds: float | None = None
    validation_seconds: float | None = None
    changed_student_count: int = 0
    changed_source_decision_count: int = 0
    unknown: bool = False
    infeasible: bool = False
    stopping_reason: str | None = None
    role_specific_gain: float = 0.0
    validation_classification: str = "not_attempted"
    validation_solver_outcome: str | None = None
    validation_error: str | None = None
    target_scope: tuple = ()
    selected_grade: int | None = None
    utilization_cluster: tuple = ()
    session_attempt_count: int = 0
    session_adopted_count: int = 0
    session_requested_seconds: float | None = None
    session_cp_sat_seconds: float | None = None
    session_validation_seconds: float | None = None
    session_external_overrun_seconds: float | None = None
    cp_sat_random_seed: int | None = None
    cp_sat_max_deterministic_time_seconds: float | None = None

    @property
    def gain_per_minute(self):
        if self.elapsed_seconds <= 0 or self.gain <= 0:
            return 0.0
        return self.gain / (self.elapsed_seconds / 60.0)


@dataclass(frozen=True)
class AdaptiveSearchState:
    """Explicit diagnostic state consumed by the policy."""

    policy_version: str
    objective_semantics_version: str
    counselor_scores: dict
    normalized_components: dict
    weighted_contributions: dict
    student_local_weighted_total: float
    highest_student_pressure: float
    top_k_pressure: dict
    nonzero_pressure_student_count: int
    student_local_weighted_share: float
    global_utilization_weighted_share: float
    elapsed_seconds: float
    remaining_seconds: float
    operator_history: tuple[AdaptiveOperatorAttempt, ...] = ()
    substantive_aggregate: float = 0.0
    student_pressure_components: dict = field(default_factory=dict)
    utilization_raw_penalty: float = 0.0
    utilization_normalized_value: float = 0.0
    utilization_weighted_value: float = 0.0
    pressured_delivery_group_count: int = 0
    top_utilization_group_share: float = 0.0
    top_three_utilization_group_share: float = 0.0
    top_five_utilization_group_share: float = 0.0
    optimistic_utilization_leverage: float = 0.0
    useful_utilization_student_count: int = 0
    utilization_ranked_student_ids: tuple = ()
    grade_opportunities: tuple = ()
    recent_operation_seconds: float = 0.0
    recent_memory_peak_bytes: int = 0
    consecutive_no_improvement_attempts: int = 0
    unknown_streak: int = 0
    validation_unknown_count: int = 0
    hard_invalid_count: int = 0
    last_target_scope: tuple = ()
    last_grade: int | None = None
    last_utilization_cluster: tuple = ()
    estimated_operator_cost_seconds: float = 0.0

    def to_dict(self):
        payload = asdict(self)
        payload["operator_history"] = [asdict(item) for item in self.operator_history]
        return payload


@dataclass(frozen=True)
class AdaptivePolicyDecision:
    """Explainable policy output; it is not a solver or approval decision."""

    operator: AdaptiveOperatorSpec
    selected_student_ids: tuple
    score: float
    reasons: tuple[str, ...]
    signal_values: dict

    def to_dict(self):
        return {
            "operator": asdict(self.operator),
            "selected_student_ids": list(self.selected_student_ids),
            "score": self.score,
            "reasons": list(self.reasons),
            "signal_values": dict(self.signal_values),
            "session_request": build_operator_session_request(
                self,
                remaining_seconds=self.signal_values.get("remaining_seconds", 0),
            ),
        }


def _role_signals(state):
    """Return transparent, current-state opportunity signals by role."""

    local_intent = sum(
        state.counselor_scores.get(key, 0) or 0
        for key in (
            "student_semester_balance",
            "difficulty_balance",
            "course_category_diversity",
            "course_sequence_preferences",
        )
    ) / 40.0
    utilization_intent = (
        state.counselor_scores.get("section_utilization_balance", 0) or 0
    ) / 10.0
    # Per-student normalized values are rounded independently from the
    # aggregate objective, so their sum can exceed the aggregate denominator.
    # Keep this policy-only share bounded; the authoritative objective facts
    # are never modified.
    bounded_local_share = min(
        1.0, max(0.0, float(state.student_local_weighted_share))
    )
    bounded_local_intent = min(1.0, max(0.0, float(local_intent)))
    bounded_utilization_intent = min(1.0, max(0.0, float(utilization_intent)))
    student_signal = bounded_local_share * bounded_local_intent
    utilization_signal = min(
        1.0,
        (
            state.global_utilization_weighted_share
            + state.top_utilization_group_share
            + min(1.0, state.optimistic_utilization_leverage / 1000.0)
        ) * bounded_utilization_intent,
    )
    grade_signal = 0.0
    if state.consecutive_no_improvement_attempts >= 2:
        grade_signal = max(
            (
                (
                    item.get("local_pressure_total", 0)
                    / max(1, state.student_local_weighted_total)
                )
                + item.get("utilization_pressure_share", 0)
            ) * max(local_intent, utilization_intent)
            for item in state.grade_opportunities
            if item.get("effective_search_available")
        ) if state.grade_opportunities else 0.0
    return {
        "local_descent": max(0.0, student_signal * 0.5),
        "targeted_repair": max(0.0, student_signal),
        "utilization_repair": max(0.0, utilization_signal),
        "basin_escape": max(0.0, grade_signal),
    }


def select_adaptive_role(state):
    """Select the opportunity role before choosing a concrete operator."""

    signals = _role_signals(state)
    return max(
        signals,
        key=lambda role: (signals[role], {
            "basin_escape": 3,
            "utilization_repair": 2,
            "targeted_repair": 1,
            "local_descent": 0,
        }[role]),
    )


@dataclass(frozen=True)
class AdaptiveSessionRecord:
    """JSON-safe diagnostic record for one adaptive session."""

    session_id: str
    policy_version: str
    input_fingerprint: str
    source_seed_fingerprint: str | None
    objective_semantics_version: str
    counselor_scores: dict
    configured_budget_seconds: float
    elapsed_seconds: float
    initial_components: dict
    final_components: dict
    initial_objective_vector: tuple
    final_objective_vector: tuple
    attempts: tuple[dict, ...]
    decisions: tuple[dict, ...]
    stopping_reason: str
    final_assignment_count: int
    final_unmet_count: int
    final_special_commitment_count: int
    resource: dict = field(default_factory=dict)
    selection_policy: str = "adaptive"
    policy_selection_seconds: float = 0.0
    operator_execution_seconds: float = 0.0
    finalization_seconds: float = 0.0
    external_overrun_seconds: float = 0.0
    cp_sat_random_seed: int | None = None
    cp_sat_max_deterministic_time_seconds: float | None = None
    # Optional diagnostic facts.  Keeping this field defaulted preserves
    # compatibility with existing in-memory records and historical JSON while
    # allowing supervised trials to localize setup before CP-SAT is reached.
    phase_timings: dict = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)

    def to_json(self):
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


def _history_for(history, operator):
    return tuple(item for item in history if item.operator == operator)


def build_adaptive_search_state(
    data,
    quality_report,
    *,
    weighted_contributions=None,
    elapsed_seconds=0.0,
    remaining_seconds=0.0,
    history=(),
    source_decisions=(),
    recent_memory_peak_bytes=0,
):
    """Build policy state from current quality facts and immutable history."""

    ranked = rank_students_by_quality_pressure(data, quality_report)
    local_values = [item.weighted_current_penalty for item in ranked]
    local_total = float(sum(local_values))
    top_k = {
        str(k): (
            sum(local_values[:k]) / local_total if local_total else 0.0
        )
        for k in (1, 2, 5, 10)
    }
    objective_facts = quality_report.get("objective_semantics", {})
    weighted = dict(
        weighted_contributions
        or objective_facts.get("weighted_normalized_contributions", {})
    )
    if not weighted:
        # Compact quality reports store the same values one level lower than
        # solver objective components. Derive the policy signal from those
        # facts without inventing a second quality calculation.
        weighted = {
            f"{name}_penalty": facts.get("weighted_normalized_contribution", 0)
            for name, facts in objective_facts.get("components", {}).items()
            if facts.get("weighted_normalized_contribution") is not None
        }
    weighted_total = float(sum(float(value or 0) for value in weighted.values()))
    utilization = float(
        weighted.get("section_utilization_balance_penalty", 0) or 0
    )
    component_facts = objective_facts.get("components", {})
    utilization_facts = component_facts.get("section_utilization_balance", {})
    utilization_raw = float(
        utilization_facts.get("raw_value", utilization_facts.get("value", 0)) or 0
    )
    utilization_normalized = float(
        utilization_facts.get("normalized_value", utilization_facts.get("normalized", 0)) or 0
    )
    utilization_guidance = build_utilization_cluster_guidance(
        data,
        quality_report,
        source_decisions,
        target_scope_size=10,
        policy="interaction_aware",
    ) if source_decisions else None
    leverage = utilization_guidance.leverage_facts if utilization_guidance else ()
    group_facts = utilization_guidance.pressure_facts if utilization_guidance else ()
    total_group_pressure = float(sum(item.pairwise_penalty for item in group_facts))
    grade_facts = build_grade_opportunity_facts(
        data, source_decisions, quality_report
    ) if getattr(data, "student_grades", ()) else ()
    no_improvement = 0
    unknown_streak = 0
    for item in reversed(tuple(history)):
        if item.adopted:
            break
        no_improvement += 1
        if item.unknown:
            unknown_streak += 1
        else:
            unknown_streak = 0
    last = history[-1] if history else None
    local_component_totals = {
        name: sum(dict(item.component_weighted_penalties).get(name, 0) for item in ranked)
        for name in (
            "student_semester_load_balance",
            "difficulty_balance",
            "course_category_diversity",
            "course_sequence_preferences",
        )
    }
    return AdaptiveSearchState(
        policy_version=ADAPTIVE_POLICY_VERSION,
        objective_semantics_version=data.objective_semantics_version,
        counselor_scores=dict(data.objective_importance_scores),
        normalized_components=dict(
            quality_report.get("objective_semantics", {}).get("normalized_components", {})
        ),
        weighted_contributions=weighted,
        student_local_weighted_total=local_total,
        highest_student_pressure=float(local_values[0] if local_values else 0),
        top_k_pressure=top_k,
        nonzero_pressure_student_count=sum(value > 0 for value in local_values),
        student_local_weighted_share=(
            local_total / weighted_total if weighted_total else 0.0
        ),
        global_utilization_weighted_share=(
            utilization / weighted_total if weighted_total else 0.0
        ),
        elapsed_seconds=float(elapsed_seconds),
        remaining_seconds=max(0.0, float(remaining_seconds)),
        operator_history=tuple(history),
        substantive_aggregate=weighted_total,
        student_pressure_components=local_component_totals,
        utilization_raw_penalty=utilization_raw,
        utilization_normalized_value=utilization_normalized,
        utilization_weighted_value=utilization,
        pressured_delivery_group_count=sum(item.pairwise_penalty > 0 for item in group_facts),
        top_utilization_group_share=(
            group_facts[0].pairwise_penalty / total_group_pressure
            if group_facts and total_group_pressure else 0.0
        ),
        top_three_utilization_group_share=(
            sum(item.pairwise_penalty for item in group_facts[:3]) / total_group_pressure
            if total_group_pressure else 0.0
        ),
        top_five_utilization_group_share=(
            sum(item.pairwise_penalty for item in group_facts[:5]) / total_group_pressure
            if total_group_pressure else 0.0
        ),
        optimistic_utilization_leverage=float(
            sum(item.total_positive_leverage for item in leverage)
        ),
        useful_utilization_student_count=sum(
            item.total_positive_leverage > 0 for item in leverage
        ),
        utilization_ranked_student_ids=tuple(item.student_id for item in leverage),
        grade_opportunities=tuple(item.__dict__ for item in grade_facts),
        recent_operation_seconds=float(last.elapsed_seconds if last else 0.0),
        recent_memory_peak_bytes=int(recent_memory_peak_bytes or 0),
        consecutive_no_improvement_attempts=no_improvement,
        unknown_streak=unknown_streak,
        validation_unknown_count=sum(
            item.validation_classification == "validation_unknown" for item in history
        ),
        hard_invalid_count=sum(
            item.validation_classification == "hard_invalid" for item in history
        ),
        last_target_scope=tuple(last.target_scope if last else ()),
        last_grade=(
            last.selected_grade
            if last and last.selected_grade is not None
            else (
                int(last.operator.rsplit("g", 1)[1])
                if last and last.operator.startswith("grade_bounded_g") else None
            )
        ),
        last_utilization_cluster=tuple(last.utilization_cluster if last else ()),
        estimated_operator_cost_seconds=float(
            sum(item.elapsed_seconds for item in history) / len(history)
            if history else 0.0
        ),
    )


def _operator_score(spec, state):
    """Return a transparent exploratory score from bounded evidence signals.

    Every signal is in approximately [0, 1]. Equal additive treatment is an
    intentionally conservative diagnostic starting point, not a production
    policy calibration. Promotion requires measured comparisons against static
    policies and does not happen in this module.
    """

    history = _history_for(state.operator_history, spec.name)
    attempts = len(history)
    successes = sum(item.adopted for item in history)
    success_rate = successes / attempts if attempts else 0.5
    gains_per_minute = [item.gain_per_minute for item in history if item.gain > 0]
    gain_signal = min(1.0, max(gains_per_minute, default=0.0) / 10.0)
    unknown_rate = (
        sum(item.unknown for item in history) / attempts if attempts else 0.0
    )
    unused_bonus = 0.25 if attempts == 0 else 0.0

    local_intent = sum(
        state.counselor_scores.get(key, 0) or 0
        for key in (
            "student_semester_balance",
            "difficulty_balance",
            "course_category_diversity",
            "course_sequence_preferences",
        )
    ) / 40.0
    utilization_signal = min(
        1.0,
        state.global_utilization_weighted_share
        + state.top_utilization_group_share
        + min(1.0, state.optimistic_utilization_leverage / 1000.0),
    )
    utilization_intent = (state.counselor_scores.get("section_utilization_balance", 0) or 0) / 10.0
    grade_signal = 0.0
    if state.grade_opportunities:
        grade_rows = tuple(
            item for item in state.grade_opportunities
            if item.get("grade_level") == spec.selected_grade
        ) if spec.selected_grade is not None else tuple(state.grade_opportunities)
        grade_signal = max(
            (
                (item.get("local_pressure_total", 0) / max(1, state.student_local_weighted_total))
                + item.get("utilization_pressure_share", 0)
            )
            for item in grade_rows
        ) if grade_rows else 0.0
    if spec.portfolio_role == "targeted_repair":
        concentration = state.top_k_pressure.get(str(spec.student_count), 0.0)
        scope_signal = state.student_local_weighted_share
        role_signal = (concentration + scope_signal + local_intent) / 3.0
    elif spec.portfolio_role == "utilization_repair":
        role_signal = utilization_signal * utilization_intent
    elif spec.portfolio_role == "basin_escape":
        role_signal = (
            grade_signal * max(local_intent, utilization_intent)
            if state.consecutive_no_improvement_attempts >= 2 else 0.0
        )
    else:
        role_signal = max(0.0, state.student_local_weighted_share)

    if state.remaining_seconds < spec.per_attempt_cp_sat_limit_seconds:
        budget_signal = 0.0
    else:
        budget_signal = 0.25

    return (
        role_signal
        + success_rate
        + gain_signal
        + unused_bonus
        + budget_signal
        - (0.5 * unknown_rate)
        - (0.05 * attempts)
    )


def choose_adaptive_operator(
    state,
    *,
    portfolio=DEFAULT_ADAPTIVE_OPERATOR_PORTFOLIO,
    ranked_students=(),
):
    """Choose one operator deterministically and explain the policy signals."""

    if state.remaining_seconds <= 0 or not portfolio:
        return None
    selected_role = select_adaptive_role(state)
    return_to_local = bool(
        state.operator_history
        and state.operator_history[-1].adopted
        and state.operator_history[-1].operator.startswith("grade_bounded_g")
    )
    if return_to_local:
        # A grade escape is a basin-change experiment.  The next action must
        # test the new basin with the cheap local operator before another
        # expensive escape is considered.  This changes allocation only; CP-SAT
        # and full validation still determine every candidate.
        selected_role = "local_descent"
    if selected_role == "utilization_repair" and len(state.utilization_ranked_student_ids) < 2:
        selected_role = "targeted_repair" if ranked_students else "local_descent"
    if selected_role == "basin_escape" and not any(
        item.get("effective_search_available") for item in state.grade_opportunities
    ):
        selected_role = "targeted_repair" if ranked_students else "local_descent"
    candidates = []
    for spec in portfolio:
        if spec.portfolio_role == "targeted_repair" and len(ranked_students) < spec.student_count:
            # A bounded target operator with no legal target is not an
            # operator failure; do not spend an iteration discovering that
            # the policy input cannot supply its requested scope.
            continue
        operator_history = _history_for(state.operator_history, spec.name)
        if operator_history and operator_history[-1].stopping_reason == "proven_scope_exhausted":
            continue
        score = _operator_score(spec, state)
        history = _history_for(state.operator_history, spec.name)
        attempts = len(history)
        success_rate = sum(item.adopted for item in history) / attempts if attempts else 0.5
        gains_per_minute = [item.gain_per_minute for item in history if item.gain > 0]
        gain_signal = min(1.0, max(gains_per_minute, default=0.0) / 10.0)
        unknown_rate = (
            sum(item.unknown for item in history) / attempts if attempts else 0.0
        )
        reasons = []
        if spec.portfolio_role == "targeted_repair":
            reasons.append("student_local_pressure_signal")
            reasons.append("student_pressure_concentration_signal")
        elif spec.portfolio_role == "utilization_repair":
            reasons.append("global_utilization_pressure_signal")
            reasons.append("utilization_leverage_signal")
        elif spec.portfolio_role == "basin_escape":
            reasons.append("grade_opportunity_signal")
            if state.consecutive_no_improvement_attempts >= 2:
                reasons.append("specialized_search_stagnation")
        else:
            reasons.append("local_descent_signal")
            if return_to_local:
                reasons.append("return_to_local_after_escape")
        if not history:
            reasons.append("untried_operator_bonus")
        elif history[-1].adopted:
            reasons.append("recent_validated_success")
        if state.remaining_seconds > 0:
            reasons.append("shared_budget_available")
        if spec.portfolio_role == "targeted_repair":
            selected = tuple(item.student_id for item in ranked_students[: spec.student_count])
        elif spec.portfolio_role == "utilization_repair":
            selected = tuple(state.utilization_ranked_student_ids[: spec.student_count])
        else:
            selected = ()
        if spec.portfolio_role == "utilization_repair" and len(selected) < spec.student_count:
            continue
        if spec.portfolio_role == "basin_escape":
            # A grade operator is eligible only when that exact grade has a
            # current actionable opportunity.  Checking merely that some
            # other grade is actionable could cause the policy to select an
            # unavailable grade because of its generic history/budget score.
            if not any(
                item.get("grade_level") == spec.selected_grade
                and item.get("effective_search_available")
                for item in state.grade_opportunities
            ):
                continue
        candidates.append(
            AdaptivePolicyDecision(
                operator=spec,
                selected_student_ids=selected,
                score=round(score, 9),
                reasons=tuple(reasons),
                signal_values={
                    "student_local_weighted_share": state.student_local_weighted_share,
                    "global_utilization_weighted_share": state.global_utilization_weighted_share,
                    "top_k_pressure": state.top_k_pressure.get(str(spec.student_count), 0.0),
                    "remaining_seconds": state.remaining_seconds,
                    "attempt_count": len(history),
                    "selected_role": selected_role,
                    "role_signals": _role_signals(state),
                    "portfolio_role": spec.portfolio_role,
                    "student_pressure_components": dict(state.student_pressure_components),
                    "utilization_weighted_value": state.utilization_weighted_value,
                    "top_utilization_group_share": state.top_utilization_group_share,
                    "optimistic_utilization_leverage": state.optimistic_utilization_leverage,
                    "grade_opportunities": tuple(state.grade_opportunities),
                    "consecutive_no_improvement_attempts": state.consecutive_no_improvement_attempts,
                    "unknown_streak": state.unknown_streak,
                    "history_effect": {
                        "attempt_count": attempts,
                        "success_rate": success_rate,
                        "gain_signal": gain_signal,
                        "unknown_rate": unknown_rate,
                        "exact_scope_exhausted": bool(
                            history
                            and history[-1].stopping_reason
                            == "proven_scope_exhausted"
                        ),
                    },
                    "budget_effect": {
                        "remaining_seconds": state.remaining_seconds,
                        "estimated_operator_cost_seconds": (
                            sum(item.elapsed_seconds for item in history) / len(history)
                            if history else spec.session_time_limit_seconds
                        ),
                        "minimum_meaningful_attempt_seconds": (
                            spec.per_attempt_cp_sat_limit_seconds
                        ),
                        "enough_for_configured_attempt": (
                            state.remaining_seconds
                            >= spec.per_attempt_cp_sat_limit_seconds
                        ),
                    },
                    "resource_facts": {
                        "recent_memory_peak_bytes": state.recent_memory_peak_bytes,
                        "recent_operation_seconds": state.recent_operation_seconds,
                    },
                    "selection_tie_break": "score, smaller radius, natural operator name",
                    "estimated_operator_cost_seconds": (
                        sum(item.elapsed_seconds for item in history) / len(history)
                        if history else spec.session_time_limit_seconds
                    ),
                },
            )
        )
    role_candidates = [
        decision for decision in candidates
        if decision.operator.portfolio_role == selected_role
    ]
    if role_candidates:
        candidates = role_candidates
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda decision: (
            decision.score,
            -(decision.operator.radius or 0),
            decision.operator.name,
        ),
    )


def build_operator_session_request(
    decision,
    *,
    remaining_seconds,
    worker_count=8,
    selected_student_ids=None,
    cp_sat_random_seed=None,
    cp_sat_max_deterministic_time_seconds=None,
):
    """Translate a policy decision into a future session execution request.

    This is an interface description for offline calibration. It does not run
    an operator, create a model, or authorize a candidate. The session runner
    remains responsible for CP-SAT, full validation, and strict adoption.
    """

    spec = decision.operator if isinstance(decision, AdaptivePolicyDecision) else decision
    if selected_student_ids is None:
        selected_student_ids = (
            decision.selected_student_ids
            if isinstance(decision, AdaptivePolicyDecision)
            else ()
        )
    return {
        "operator_family": spec.name,
        "allocated_time_limit_seconds": min(
            max(0.0, float(remaining_seconds)),
            float(spec.session_time_limit_seconds),
        ),
        "max_attempts": int(spec.session_max_attempts),
        "per_attempt_time_limit_seconds": float(spec.per_attempt_cp_sat_limit_seconds),
        "worker_count": int(worker_count),
        "target_policy": spec.target_policy,
        "selected_grade": spec.selected_grade,
        "selected_student_ids": tuple(selected_student_ids),
        "cp_sat_random_seed": (
            int(cp_sat_random_seed) if cp_sat_random_seed is not None else None
        ),
        "cp_sat_max_deterministic_time_seconds": (
            float(cp_sat_max_deterministic_time_seconds)
            if cp_sat_max_deterministic_time_seconds is not None
            else None
        ),
    }


def replay_adaptive_policy(records, *, portfolio=DEFAULT_ADAPTIVE_OPERATOR_PORTFOLIO):
    """Replay policy decisions from structured records without solving."""

    history = tuple(
        AdaptiveOperatorAttempt(
            operator=item["operator"],
            status=item.get("status", "unknown"),
            candidate_found=bool(item.get("candidate_found")),
            candidate_validated=bool(item.get("candidate_validated")),
            adopted=bool(item.get("candidate_adopted", item.get("adopted", False))),
            gain=float(item.get("gain", 0) or 0),
            elapsed_seconds=float(item.get("total_operation_seconds", 0) or 0),
            unknown=(
                item.get("status") == "unknown"
                or item.get("validation_classification") == "validation_unknown"
            ),
            infeasible=item.get("status") == "infeasible",
            role_specific_gain=float(item.get("role_specific_gain", 0) or 0),
            validation_classification=str(
                item.get("validation_classification", "not_attempted")
            ),
            validation_solver_outcome=item.get("validation_solver_outcome"),
            validation_error=item.get("validation_error"),
            target_scope=tuple(item.get("target_scope", ())),
            selected_grade=item.get("selected_grade"),
            utilization_cluster=tuple(item.get("utilization_cluster", ())),
        )
        for item in records
    )
    return history


def simulate_adaptive_policy(
    state,
    attempts,
    *,
    portfolio=DEFAULT_ADAPTIVE_OPERATOR_PORTFOLIO,
    ranked_students=(),
):
    """Replay operator allocation from recorded outcomes without solving.

    This is intentionally an evidence simulator, not a predictor: recorded
    outcomes are fed back into the transparent policy so researchers can
    compare allocation rules without creating CP-SAT models or authorizing a
    schedule.
    """

    history = list(state.operator_history)
    decisions = []
    for item in attempts:
        current = AdaptiveSearchState(
            **{
                **state.__dict__,
                "operator_history": tuple(history),
            }
        )
        decision = choose_adaptive_operator(
            current,
            portfolio=portfolio,
            ranked_students=ranked_students,
        )
        if decision is None:
            break
        decisions.append(decision)
        if isinstance(item, AdaptiveOperatorAttempt):
            history.append(item)
        else:
            history.append(
                AdaptiveOperatorAttempt(
                    operator=item["operator"],
                    status=item.get("status", "unknown"),
                    candidate_found=bool(item.get("candidate_found")),
                    candidate_validated=bool(item.get("candidate_validated")),
                    adopted=bool(item.get("adopted", item.get("candidate_adopted", False))),
                    gain=float(item.get("gain", 0) or 0),
                    elapsed_seconds=float(item.get("elapsed_seconds", item.get("total_operation_seconds", 0)) or 0),
                    unknown=(
                        item.get("status") == "unknown"
                        or item.get("validation_classification") == "validation_unknown"
                    ),
                    infeasible=item.get("status") == "infeasible",
                    role_specific_gain=float(item.get("role_specific_gain", 0) or 0),
                    validation_classification=str(
                        item.get("validation_classification", "not_attempted")
                    ),
                    validation_solver_outcome=item.get("validation_solver_outcome"),
                    validation_error=item.get("validation_error"),
                    target_scope=tuple(item.get("target_scope", ())),
                    selected_grade=item.get("selected_grade"),
                    utilization_cluster=tuple(item.get("utilization_cluster", ())),
                )
            )
    return tuple(decisions)


def select_stateless_role_operator(
    state,
    *,
    portfolio=DEFAULT_ADAPTIVE_OPERATOR_PORTFOLIO,
    ranked_students=(),
):
    """Select with current signals but without history-dependent learning.

    This is a solver-free control policy for matched offline experiments.  It
    deliberately retains the same role and scope rules as the adaptive policy
    while removing prior-attempt success, unknown, and exhaustion effects.
    """

    stateless = AdaptiveSearchState(
        **{
            **state.__dict__,
            "operator_history": (),
            "consecutive_no_improvement_attempts": 0,
            "unknown_streak": 0,
            "validation_unknown_count": 0,
            "hard_invalid_count": 0,
            "last_target_scope": (),
            "last_grade": None,
            "last_utilization_cluster": (),
            "recent_operation_seconds": 0.0,
            "estimated_operator_cost_seconds": 0.0,
        }
    )
    return choose_adaptive_operator(
        stateless,
        portfolio=portfolio,
        ranked_students=ranked_students,
    )


def select_fixed_cycle_operator(
    state,
    cycle,
    *,
    ranked_students=(),
):
    """Select the next operator from a deterministic offline control cycle."""

    cycle = tuple(cycle)
    if not cycle or state.remaining_seconds <= 0:
        return None
    index = len(state.operator_history) % len(cycle)
    spec = cycle[index]
    if spec.portfolio_role == "targeted_repair":
        if len(ranked_students) < spec.student_count:
            return None
        selected = tuple(
            item.student_id for item in ranked_students[: spec.student_count]
        )
    elif spec.portfolio_role == "utilization_repair":
        selected = tuple(
            state.utilization_ranked_student_ids[: spec.student_count]
        )
        if len(selected) < spec.student_count:
            return None
    else:
        selected = ()
    return AdaptivePolicyDecision(
        operator=spec,
        selected_student_ids=selected,
        score=0.0,
        reasons=("fixed_cycle_control",),
        signal_values={
            "selected_role": spec.portfolio_role,
            "remaining_seconds": state.remaining_seconds,
            "cycle_index": index,
            "selection_tie_break": "configured fixed cycle order",
        },
    )


__all__ = [
    "ADAPTIVE_POLICY_VERSION",
    "AdaptiveOperatorAttempt",
    "AdaptiveOperatorSpec",
    "AdaptivePolicyDecision",
    "AdaptiveSearchState",
    "AdaptiveSessionRecord",
    "DEFAULT_ADAPTIVE_OPERATOR_PORTFOLIO",
    "build_adaptive_search_state",
    "select_adaptive_role",
    "choose_adaptive_operator",
    "select_stateless_role_operator",
    "select_fixed_cycle_operator",
    "build_operator_session_request",
    "replay_adaptive_policy",
    "simulate_adaptive_policy",
]
