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
import math

from .grade_guidance import build_grade_opportunity_facts
from .search_guidance import rank_students_by_quality_pressure
from .utilization_guidance import build_utilization_cluster_guidance


ADAPTIVE_POLICY_VERSION = "v2-local-allocator-diagnostic-3"

# These variants alter diagnostic operator allocation only.  They do not
# change Objective Semantics v2, counselor scores, candidate authority, or the
# set of operators that may be executed.
ADAPTIVE_POLICY_VARIANTS = (
    "balanced",
    "student_pressure_biased",
    "utilization_biased",
    "evidence_guided",
    "r4_anchor",
)
ADAPTIVE_ROLE_BIAS_MULTIPLIER = 0.25

# Evidence-guided policy constants are diagnostic allocation controls only.
# They do not enter Objective Semantics v2 or any solver model.
EVIDENCE_GUIDED_GAIN_SCALE_PER_MINUTE = 10.0
EVIDENCE_GUIDED_CONTINUATION_LIMIT = 2
EVIDENCE_GUIDED_DUPLICATE_SCOPE_PENALTY = 0.25
EVIDENCE_GUIDED_OPERATOR_PRIORS = {
    "targeted_r4_s2": 0.20,
    "r2": 0.05,
    "targeted_utilization_r16_s4": 0.025,
    "targeted_utilization_r64_s8": 0.025,
}


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
    # The policy-selected scope and the scope actually passed to the probe can
    # differ when an operator uses dynamic targeting. Keep both facts so
    # diagnostic comparisons do not mistake policy selection for execution.
    actual_target_scope: tuple = ()
    # Diagnostic identity of the candidate source decisions returned by the
    # operator. This is deliberately metadata only; candidate authority still
    # comes from the existing full-model validation boundary.
    source_fingerprint_before: str | None = None
    candidate_source_decision_fingerprint: str | None = None
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
    # Compact facts from the inner CP-SAT probes run by a reusable operator
    # session.  This is diagnostic telemetry only; the outer attempt remains
    # the policy-level record and candidate authority is unchanged.
    inner_probe_summaries: tuple[dict, ...] = ()
    # Existing solver-neutral policy state captured around this attempt. These
    # fields explain opportunity/exhaustion; they never affect selection or
    # candidate authority.
    role_pressure_before: dict = field(default_factory=dict)
    role_pressure_after: dict = field(default_factory=dict)
    exhaustion_classification: str = "OPERATOR_UNRESOLVED"
    role_exhaustion_classification: str = "ROLE_EXHAUSTION_NOT_PROVEN"
    sequence_position: int | None = None
    operator_family: str | None = None

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
    current_source_fingerprint: str | None = None
    candidate_validation_time_limit_seconds: float | None = None

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


def _role_selection_facts(state, adaptive_policy_variant="balanced"):
    """Return raw and variant-adjusted role signals for one policy decision.

    The biased variants deliberately adjust only the role gate.  Concrete
    operator scoring, eligibility, history effects, and tie-breaking remain
    the existing behavior in ``choose_adaptive_operator``.
    """

    if adaptive_policy_variant not in ADAPTIVE_POLICY_VARIANTS:
        raise ValueError(
            "adaptive_policy_variant must be balanced, "
            "student_pressure_biased, utilization_biased, evidence_guided, "
            "or r4_anchor"
        )
    raw_signals = _role_signals(state)
    adjusted_signals = dict(raw_signals)
    role_biases = {role: 0.0 for role in raw_signals}
    if adaptive_policy_variant == "student_pressure_biased":
        role_biases["targeted_repair"] = (
            raw_signals["targeted_repair"] * ADAPTIVE_ROLE_BIAS_MULTIPLIER
        )
        adjusted_signals["targeted_repair"] += role_biases[
            "targeted_repair"
        ]
    elif adaptive_policy_variant == "utilization_biased":
        role_biases["utilization_repair"] = (
            raw_signals["utilization_repair"] * ADAPTIVE_ROLE_BIAS_MULTIPLIER
        )
        adjusted_signals["utilization_repair"] += role_biases[
            "utilization_repair"
        ]
    selected_role = max(
        adjusted_signals,
        key=lambda role: (adjusted_signals[role], {
            "basin_escape": 3,
            "utilization_repair": 2,
            "targeted_repair": 1,
            "local_descent": 0,
        }[role]),
    )
    return {
        "raw_signals": raw_signals,
        "adjusted_signals": adjusted_signals,
        "role_biases": role_biases,
        "selected_role": selected_role,
    }


def select_adaptive_role(state, *, adaptive_policy_variant="balanced"):
    """Select the opportunity role before choosing a concrete operator."""

    return _role_selection_facts(
        state,
        adaptive_policy_variant=adaptive_policy_variant,
    )["selected_role"]


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
    adaptive_policy_variant: str = "balanced"
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


def operator_family(spec_or_name):
    """Return a stable family identity for grouped policy evidence."""

    name = spec_or_name.name if isinstance(spec_or_name, AdaptiveOperatorSpec) else str(spec_or_name)
    if name == "r2":
        return "local_r2"
    if name.startswith("targeted_r4_"):
        return "targeted_r4"
    if name.startswith("targeted_r8_"):
        return "targeted_r8"
    if name.startswith("targeted_utilization_r16_"):
        return "utilization_r16"
    if name.startswith("targeted_utilization_r32_"):
        return "utilization_r32"
    if name.startswith("targeted_utilization_r64_"):
        return "utilization_r64"
    if name.startswith("grade_bounded_g"):
        return name
    return name


def _family_history(history, family):
    return tuple(item for item in history if operator_family(item.operator) == family)


def _role_history(history, role, portfolio):
    names = {spec.name for spec in portfolio if spec.portfolio_role == role}
    return tuple(item for item in history if item.operator in names)


def _group_yield(history):
    if not history:
        return 0.0
    total_minutes = sum(max(0.0, float(item.elapsed_seconds)) for item in history) / 60.0
    if total_minutes <= 0:
        return 0.0
    total_gain = sum(max(0.0, float(item.gain)) for item in history if item.adopted)
    return min(1.0, total_gain / (total_minutes * EVIDENCE_GUIDED_GAIN_SCALE_PER_MINUTE))


def _group_reliability(history):
    resolved = tuple(
        item for item in history
        if not item.unknown and item.validation_classification not in {
            "validation_unknown", "validation_error"
        }
    )
    return (1.0 + sum(bool(item.adopted) for item in resolved)) / (2.0 + len(resolved))


def _group_unknown_rate(history):
    return (
        sum(bool(item.unknown) or item.validation_classification in {
            "validation_unknown", "validation_error"
        } for item in history) / len(history)
        if history else 0.0
    )


def _scope_key(spec, selected_student_ids, state):
    return (
        spec.name,
        state.current_source_fingerprint,
        tuple(selected_student_ids),
        spec.selected_grade,
    )


def _scope_status(history, key):
    for item in reversed(history):
        item_key = (
            item.operator,
            item.source_fingerprint_before,
            tuple(item.actual_target_scope or item.target_scope),
            item.selected_grade,
        )
        if item_key != key:
            continue
        if item.unknown or item.validation_classification in {
            "validation_unknown", "validation_error"
        }:
            return "unresolved"
        if item.exhaustion_classification == "EXACT_SCOPE_EXHAUSTED":
            return "exhausted"
        return "non_improving"
    return None


def _predicted_scope(spec, state, ranked_students):
    if spec.portfolio_role == "targeted_repair":
        return tuple(item.student_id for item in ranked_students[: spec.student_count])
    if spec.portfolio_role == "utilization_repair":
        return tuple(state.utilization_ranked_student_ids[: spec.student_count])
    return ()


def _estimated_full_cost(spec, state, history):
    operator_history = _history_for(history, spec.name)
    if operator_history:
        return max(0.001, sum(item.elapsed_seconds for item in operator_history) / len(operator_history))
    validation = float(state.candidate_validation_time_limit_seconds or 0.0)
    return max(0.001, float(spec.session_time_limit_seconds) + validation)


def _evidence_guided_score(spec, state, portfolio, ranked_students):
    history = tuple(state.operator_history)
    exact_history = _history_for(history, spec.name)
    family_history = _family_history(history, operator_family(spec))
    role_history = _role_history(history, spec.portfolio_role, portfolio)
    role_signals = _role_signals(state)
    predicted = _predicted_scope(spec, state, ranked_students)
    scope_state = _scope_status(history, _scope_key(spec, predicted, state))
    operator_yield = _group_yield(exact_history)
    family_yield = _group_yield(family_history)
    role_yield = _group_yield(role_history)
    family_centered_reliability = 2.0 * _group_reliability(family_history) - 1.0
    family_unknown_rate = _group_unknown_rate(family_history)
    family_attempts = len(family_history)
    estimated_cost = _estimated_full_cost(spec, state, history)
    budget_fit = min(1.0, state.remaining_seconds / estimated_cost)
    previous = history[-1] if history else None
    productive_continuation = bool(
        previous
        and previous.adopted
        and previous.operator == spec.name
        and previous.candidate_source_decision_fingerprint
        and previous.candidate_source_decision_fingerprint != previous.source_fingerprint_before
    )
    duplicate_penalty = (
        EVIDENCE_GUIDED_DUPLICATE_SCOPE_PENALTY
        if scope_state == "unresolved" else 0.0
    )
    score = (
        role_signals.get(spec.portfolio_role, 0.0)
        + EVIDENCE_GUIDED_OPERATOR_PRIORS.get(spec.name, 0.0)
        + 0.25 * operator_yield
        + 0.15 * family_yield
        + 0.10 * role_yield
        + 0.10 * family_centered_reliability
        + 0.10 / math.sqrt(1.0 + family_attempts)
        + 0.10 * budget_fit
        - 0.25 * family_unknown_rate
        - duplicate_penalty
    )
    if productive_continuation:
        score += 0.20
    return {
        "score": score,
        "operator_family": operator_family(spec),
        "role_signal": role_signals.get(spec.portfolio_role, 0.0),
        "operator_prior": EVIDENCE_GUIDED_OPERATOR_PRIORS.get(spec.name, 0.0),
        "operator_yield": operator_yield,
        "family_yield": family_yield,
        "role_yield": role_yield,
        "family_centered_reliability": family_centered_reliability,
        "family_unknown_rate": family_unknown_rate,
        "family_attempt_count": family_attempts,
        "estimated_full_cost_seconds": estimated_cost,
        "budget_fit": budget_fit,
        "predicted_scope": predicted,
        "scope_state": scope_state,
        "duplicate_scope_penalty": duplicate_penalty,
        "productive_continuation": productive_continuation,
    }


def _new_policy_decision(spec, selected, score, reasons, signal_values):
    return AdaptivePolicyDecision(
        operator=spec,
        selected_student_ids=tuple(selected),
        score=round(float(score), 9),
        reasons=tuple(reasons),
        signal_values=dict(signal_values),
    )


def _eligible_operator_selection(spec, state, ranked_students, history):
    """Return the predicted scope or ``None`` when the existing scope rules reject it."""

    if spec.portfolio_role == "targeted_repair":
        if len(ranked_students) < spec.student_count:
            return None
        selected = tuple(item.student_id for item in ranked_students[: spec.student_count])
    elif spec.portfolio_role == "utilization_repair":
        selected = tuple(state.utilization_ranked_student_ids[: spec.student_count])
        if len(selected) < spec.student_count:
            return None
    else:
        selected = ()
    if spec.portfolio_role == "basin_escape":
        # Grade-bounded search is deliberately a stagnation escape.  Keeping
        # this gate here is important because the evidence-guided variants
        # score all roles together; otherwise a grade operator could win on
        # exploration/budget evidence before the existing two-failed-attempt
        # safeguard has made a basin escape appropriate.
        if state.consecutive_no_improvement_attempts < 2:
            return None
        if not any(
            item.get("grade_level") == spec.selected_grade
            and item.get("effective_search_available")
            for item in state.grade_opportunities
        ):
            return None
    scope_state = _scope_status(history, _scope_key(spec, selected, state))
    if scope_state in {"exhausted", "non_improving"}:
        return None
    return selected


def _productive_continuation_operator(state, portfolio, ranked_students):
    """Return a successful operator's next fresh scope, at most twice in a row."""

    history = tuple(state.operator_history)
    if not history or not history[-1].adopted:
        return None
    previous = history[-1]
    consecutive = 0
    for item in reversed(history):
        if item.operator != previous.operator or not item.adopted:
            break
        consecutive += 1
    if consecutive > EVIDENCE_GUIDED_CONTINUATION_LIMIT:
        return None
    spec = next((item for item in portfolio if item.name == previous.operator), None)
    if spec is None:
        return None
    selected = _eligible_operator_selection(spec, state, ranked_students, history)
    if selected is None:
        return None
    if not previous.candidate_source_decision_fingerprint:
        return None
    if (
        state.current_source_fingerprint
        and state.current_source_fingerprint
        == previous.source_fingerprint_before
    ):
        return None
    return spec, selected, consecutive


def _choose_evidence_guided_operator(
    state,
    *,
    portfolio,
    ranked_students,
    adaptive_policy_variant,
):
    """Choose across eligible roles using current opportunity plus live evidence."""

    history = tuple(state.operator_history)
    continuation = _productive_continuation_operator(state, portfolio, ranked_students)
    if continuation is not None:
        spec, selected, consecutive = continuation
        facts = _evidence_guided_score(spec, state, portfolio, ranked_students)
        return _new_policy_decision(
            spec,
            selected,
            facts["score"],
            (
                "productive_validated_continuation",
                "fresh_source_incumbent",
            ),
            {
                "selected_role": spec.portfolio_role,
                "adaptive_policy_variant": adaptive_policy_variant,
                "operator_family": facts["operator_family"],
                "evidence_guided": facts,
                "continuation_count": consecutive,
            },
        )

    if adaptive_policy_variant == "r4_anchor" and not history:
        anchored = next((item for item in portfolio if item.name == "targeted_r4_s2"), None)
        if anchored is not None:
            selected = _eligible_operator_selection(anchored, state, ranked_students, history)
            if selected is not None:
                facts = _evidence_guided_score(anchored, state, portfolio, ranked_students)
                return _new_policy_decision(
                    anchored,
                    selected,
                    facts["score"],
                    ("r4_s2_evidence_anchor",),
                    {
                        "selected_role": anchored.portfolio_role,
                        "adaptive_policy_variant": adaptive_policy_variant,
                        "operator_family": facts["operator_family"],
                        "evidence_guided": facts,
                        "anchor_fallback": False,
                    },
                )

    anchor_fallback_reason = None
    if adaptive_policy_variant == "r4_anchor" and not history:
        anchor_fallback_reason = (
            "targeted_r4_s2_unavailable_or_scope_ineligible"
        )

    candidates = []
    for spec in portfolio:
        selected = _eligible_operator_selection(spec, state, ranked_students, history)
        if selected is None:
            continue
        facts = _evidence_guided_score(spec, state, portfolio, ranked_students)
        reasons = ["bounded_current_opportunity", "evidence_guided_score"]
        if anchor_fallback_reason is not None:
            reasons.append("r4_anchor_fallback")
        if facts["operator_prior"]:
            reasons.append("historical_operator_prior")
        if facts["family_attempt_count"] == 0:
            reasons.append("family_exploration_bonus")
        candidates.append(
            _new_policy_decision(
                spec,
                selected,
                facts["score"],
                reasons,
                {
                    "selected_role": spec.portfolio_role,
                    "adaptive_policy_variant": adaptive_policy_variant,
                    "operator_family": facts["operator_family"],
                    "evidence_guided": facts,
                    "role_signals": _role_signals(state),
                    "anchor_fallback": bool(anchor_fallback_reason),
                    "anchor_fallback_reason": anchor_fallback_reason,
                },
            )
        )
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda decision: (
            decision.score,
            -_estimated_full_cost(decision.operator, state, history),
            -(decision.operator.radius or 0),
            decision.operator.name,
        ),
    )


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
    current_source_fingerprint=None,
    candidate_validation_time_limit_seconds=None,
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
        current_source_fingerprint=current_source_fingerprint,
        candidate_validation_time_limit_seconds=(
            float(candidate_validation_time_limit_seconds)
            if candidate_validation_time_limit_seconds is not None
            else None
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
    adaptive_policy_variant="balanced",
):
    """Choose one operator deterministically and explain the policy signals."""

    if state.remaining_seconds <= 0 or not portfolio:
        return None
    if adaptive_policy_variant in {"evidence_guided", "r4_anchor"}:
        return _choose_evidence_guided_operator(
            state,
            portfolio=portfolio,
            ranked_students=ranked_students,
            adaptive_policy_variant=adaptive_policy_variant,
        )
    role_selection = _role_selection_facts(
        state,
        adaptive_policy_variant=adaptive_policy_variant,
    )
    selected_role = role_selection["selected_role"]
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
                    "adaptive_policy_variant": adaptive_policy_variant,
                    "role_signals": role_selection["raw_signals"],
                    "adjusted_role_signals": role_selection[
                        "adjusted_signals"
                    ],
                    "role_biases": role_selection["role_biases"],
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
            actual_target_scope=tuple(item.get("actual_target_scope", ())),
            source_fingerprint_before=item.get("source_fingerprint_before"),
            candidate_source_decision_fingerprint=item.get(
                "candidate_source_decision_fingerprint"
            ),
            selected_grade=item.get("selected_grade"),
            utilization_cluster=tuple(item.get("utilization_cluster", ())),
            exhaustion_classification=item.get(
                "exhaustion_classification", "OPERATOR_UNRESOLVED"
            ),
            operator_family=item.get("operator_family"),
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
    adaptive_policy_variant="balanced",
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
            adaptive_policy_variant=adaptive_policy_variant,
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
                    actual_target_scope=tuple(item.get("actual_target_scope", ())),
                    source_fingerprint_before=item.get("source_fingerprint_before"),
                    candidate_source_decision_fingerprint=item.get(
                        "candidate_source_decision_fingerprint"
                    ),
                    selected_grade=item.get("selected_grade"),
                    utilization_cluster=tuple(item.get("utilization_cluster", ())),
                    exhaustion_classification=item.get(
                        "exhaustion_classification", "OPERATOR_UNRESOLVED"
                    ),
                    operator_family=item.get("operator_family"),
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
    "ADAPTIVE_POLICY_VARIANTS",
    "ADAPTIVE_ROLE_BIAS_MULTIPLIER",
    "EVIDENCE_GUIDED_CONTINUATION_LIMIT",
    "EVIDENCE_GUIDED_DUPLICATE_SCOPE_PENALTY",
    "EVIDENCE_GUIDED_GAIN_SCALE_PER_MINUTE",
    "EVIDENCE_GUIDED_OPERATOR_PRIORS",
    "AdaptiveOperatorAttempt",
    "AdaptiveOperatorSpec",
    "AdaptivePolicyDecision",
    "AdaptiveSearchState",
    "AdaptiveSessionRecord",
    "DEFAULT_ADAPTIVE_OPERATOR_PORTFOLIO",
    "operator_family",
    "build_adaptive_search_state",
    "select_adaptive_role",
    "choose_adaptive_operator",
    "select_stateless_role_operator",
    "select_fixed_cycle_operator",
    "build_operator_session_request",
    "replay_adaptive_policy",
    "simulate_adaptive_policy",
]
