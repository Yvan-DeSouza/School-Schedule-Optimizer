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

from .search_guidance import rank_students_by_quality_pressure


ADAPTIVE_POLICY_VERSION = "v2-local-allocator-diagnostic-1"


@dataclass(frozen=True)
class AdaptiveOperatorSpec:
    """One existing local operator exposed to diagnostic policy selection."""

    name: str
    radius: int
    max_changed_students: int | None
    targeted: bool
    student_count: int
    portfolio_role: str


DEFAULT_ADAPTIVE_OPERATOR_PORTFOLIO = (
    AdaptiveOperatorSpec("r2", 2, None, False, 0, "local_descent"),
    AdaptiveOperatorSpec("targeted_r8_s1", 8, 1, True, 1, "targeted_repair"),
    AdaptiveOperatorSpec("targeted_r8_s2", 8, 2, True, 2, "targeted_repair"),
    AdaptiveOperatorSpec("targeted_r4_s2", 4, 2, True, 2, "targeted_repair"),
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
        }


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

    if spec.targeted:
        concentration = state.top_k_pressure.get(str(spec.student_count), 0.0)
        scope_signal = state.student_local_weighted_share
        intent_signal = sum(
            state.counselor_scores.get(key, 0) or 0
            for key in (
                "student_semester_balance",
                "difficulty_balance",
                "course_category_diversity",
                "course_sequence_preferences",
            )
        ) / 40.0
        role_signal = (concentration + scope_signal + intent_signal) / 3.0
    else:
        role_signal = state.global_utilization_weighted_share

    return (
        role_signal
        + success_rate
        + gain_signal
        + unused_bonus
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
    candidates = []
    for spec in portfolio:
        if spec.targeted and len(ranked_students) < spec.student_count:
            # A bounded target operator with no legal target is not an
            # operator failure; do not spend an iteration discovering that
            # the policy input cannot supply its requested scope.
            continue
        score = _operator_score(spec, state)
        history = _history_for(state.operator_history, spec.name)
        reasons = []
        if spec.targeted:
            reasons.append("student_local_pressure_signal")
            reasons.append("student_pressure_concentration_signal")
        else:
            reasons.append("global_utilization_pressure_signal")
        if not history:
            reasons.append("untried_operator_bonus")
        elif history[-1].adopted:
            reasons.append("recent_validated_success")
        if state.remaining_seconds > 0:
            reasons.append("shared_budget_available")
        selected = tuple(
            item.student_id for item in ranked_students[: spec.student_count]
        ) if spec.targeted else ()
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
                },
            )
        )
    return max(
        candidates,
        key=lambda decision: (
            decision.score,
            -decision.operator.radius,
            decision.operator.name,
        ),
    )


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
            unknown=item.get("status") == "unknown",
            infeasible=item.get("status") == "infeasible",
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
                    unknown=item.get("status") == "unknown",
                    infeasible=item.get("status") == "infeasible",
                )
            )
    return tuple(decisions)


__all__ = [
    "ADAPTIVE_POLICY_VERSION",
    "AdaptiveOperatorAttempt",
    "AdaptiveOperatorSpec",
    "AdaptivePolicyDecision",
    "AdaptiveSearchState",
    "AdaptiveSessionRecord",
    "DEFAULT_ADAPTIVE_OPERATOR_PORTFOLIO",
    "build_adaptive_search_state",
    "choose_adaptive_operator",
    "replay_adaptive_policy",
    "simulate_adaptive_policy",
]
