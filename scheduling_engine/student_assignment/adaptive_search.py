"""Diagnostic Objective Semantics v2 adaptive local-search policy.

This module owns policy state and operator selection only. It never imports
Django, constructs CP-SAT models, validates candidates, or authorizes a
schedule. The diagnostic runner in ``adaptive_runtime.py`` executes the
selected existing operators and remains subject to CP-SAT plus full-model
validation.
"""

from __future__ import annotations

from dataclasses import MISSING, asdict, dataclass, field, replace
import hashlib
import json
import math
from types import SimpleNamespace

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
ADAPTIVE_POLICY_LADDER_VARIANTS = (
    "hierarchical_evidence",
    "hierarchical_recent",
    "component_aware",
    "horizon_aware",
)
ALL_ADAPTIVE_POLICY_VARIANTS = ADAPTIVE_POLICY_VARIANTS + ADAPTIVE_POLICY_LADDER_VARIANTS
ADAPTIVE_ROLE_BIAS_MULTIPLIER = 0.25

ADAPTIVE_SELECTOR_STATE_SCHEMA = "adaptive_selector_state_v1"
ADAPTIVE_SELECTOR_TRACE_SCHEMA = "adaptive_selector_trace_v1"
ADAPTIVE_OBJECTIVE_TRAJECTORY_SCHEMA = "adaptive_objective_trajectory_v1"
ADAPTIVE_NEW_POLICY_VERSIONS = {
    "hierarchical_evidence": "v2-local-allocator-hierarchical-1",
    "hierarchical_recent": "v2-local-allocator-hierarchical-recent-1",
    "component_aware": "v2-local-allocator-component-aware-1",
    "horizon_aware": "v2-local-allocator-horizon-aware-1",
}

# The new policies are a cumulative, diagnostic-only ladder.  The values are
# deliberately explicit so policy fingerprints can identify every selection
# semantic without changing the historical evidence-guided configuration.
HIERARCHICAL_EXACT_PSEUDOCOUNT = 2.0
HIERARCHICAL_FAMILY_PSEUDOCOUNT = 4.0
HIERARCHICAL_ROLE_PSEUDOCOUNT = 8.0
RECENT_PRODUCTIVITY_WINDOW_ATTEMPTS = 6
RECENT_PRODUCTIVITY_MAX_WEIGHT = 0.50
RECENT_PRODUCTIVITY_WEIGHT_DIVISOR = 4.0
COMPONENT_ALIGNMENT_WEIGHT = 0.10
HORIZON_EXPLORATION_MAX = 0.20

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
    # Scope order is not semantically meaningful.  This records equality after
    # both scopes have been canonicalized by the diagnostic runtime.
    scope_equal: bool | None = None
    scope_mismatch: bool = False
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
    # Search progress and validation authority are intentionally separate. A
    # complete improving candidate that could not yet be validated is useful
    # diagnostic evidence, but it is never authoritative productivity.
    candidate_discovery_gain: float = 0.0
    search_unknown: bool = False
    validation_retry_count: int = 0
    validation_retry_facts: dict = field(default_factory=dict)
    # Canonical weighted v2 deltas are captured only for an adopted,
    # full-model-validated transition.  They are policy evidence, not a
    # replacement for the solver's objective facts.
    objective_weighted_delta: dict = field(default_factory=dict)
    objective_normalized_delta: dict = field(default_factory=dict)

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
    current_objective_vector: tuple = ()

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

    if adaptive_policy_variant not in ALL_ADAPTIVE_POLICY_VARIANTS:
        raise ValueError(
            "adaptive_policy_variant must be balanced, "
            "student_pressure_biased, utilization_biased, evidence_guided, "
            "r4_anchor, hierarchical_evidence, hierarchical_recent, "
            "component_aware, or horizon_aware"
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
    selector_trace_schema: str = ADAPTIVE_SELECTOR_TRACE_SCHEMA
    objective_trajectory: dict = field(default_factory=dict)

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
        "validation_unknown", "validation_error", "scope_mismatch"
        }
    )
    return (1.0 + sum(bool(item.adopted) for item in resolved)) / (2.0 + len(resolved))


def _group_unknown_rate(history):
    return (
        sum(bool(item.unknown) or item.validation_classification in {
            "validation_unknown", "validation_error", "scope_mismatch"
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
            "validation_unknown", "validation_error", "scope_mismatch"
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


def _stable_fingerprint(value):
    """Return a compact deterministic fingerprint for diagnostic payloads."""

    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _attempt_is_resolved(item):
    """Return whether an attempt supplies an authoritative outcome sample."""

    return not (
        bool(getattr(item, "unknown", False))
        or bool(getattr(item, "search_unknown", False))
        or getattr(item, "validation_classification", "")
        in {"validation_unknown", "validation_error", "scope_mismatch"}
    )


def _disjoint_evidence_history(history, spec, portfolio):
    """Split evidence into exact, family-peer, and role-peer observations."""

    history = tuple(history)
    family = operator_family(spec)
    exact = tuple(item for item in history if item.operator == spec.name)
    family_all = tuple(
        item for item in history if operator_family(item.operator) == family
    )
    role_all = _role_history(history, spec.portfolio_role, portfolio)
    family_peers = tuple(item for item in family_all if item.operator != spec.name)
    role_peers = tuple(
        item for item in role_all if operator_family(item.operator) != family
    )
    return exact, family_peers, role_peers, family_all, role_all


def _yield_observation(history):
    """Return bounded validated productivity and its resolved sample count."""

    resolved = tuple(item for item in history if _attempt_is_resolved(item))
    total_minutes = sum(
        max(0.0, float(item.elapsed_seconds)) for item in resolved
    ) / 60.0
    if total_minutes <= 0:
        value = 0.0
    else:
        value = sum(
            max(0.0, float(item.gain))
            for item in resolved
            if bool(item.adopted)
        ) / (total_minutes * EVIDENCE_GUIDED_GAIN_SCALE_PER_MINUTE)
    return {
        "yield": min(1.0, max(0.0, value)),
        "resolved_attempt_count": len(resolved),
        "resolved_elapsed_seconds": sum(
            max(0.0, float(item.elapsed_seconds)) for item in resolved
        ),
        "adopted_count": sum(bool(item.adopted) for item in resolved),
    }


def _recent_yield_observation(history, *, recent_history):
    recent_ids = {id(item) for item in recent_history}
    return _yield_observation(
        tuple(item for item in history if id(item) in recent_ids)
    )


def _hierarchical_yield_observation(
    history,
    strata,
    *,
    use_recent=False,
):
    """Build one bounded, non-overlapping productivity hierarchy."""

    exact, family_peers, role_peers, family_all, role_all = strata
    recent_history = tuple(history[-RECENT_PRODUCTIVITY_WINDOW_ATTEMPTS:])

    def blended(items):
        lifetime = _yield_observation(items)
        recent = _recent_yield_observation(items, recent_history=recent_history)
        recent_count = recent["resolved_attempt_count"]
        recent_weight = (
            min(
                RECENT_PRODUCTIVITY_MAX_WEIGHT,
                recent_count / RECENT_PRODUCTIVITY_WEIGHT_DIVISOR,
            )
            if use_recent
            else 0.0
        )
        value = (
            (1.0 - recent_weight) * lifetime["yield"]
            + recent_weight * recent["yield"]
        )
        return {
            "lifetime": lifetime,
            "recent": recent,
            "recent_weight": recent_weight,
            "blended_yield": min(1.0, max(0.0, value)),
        }

    role = blended(role_peers)
    family = blended(family_peers)
    exact_facts = blended(exact)

    def shrink(value, parent, count, pseudo):
        return (
            (count * value) + (pseudo * parent)
        ) / (count + pseudo) if count + pseudo else parent

    role_value = shrink(
        role["blended_yield"],
        0.0,
        role["lifetime"]["resolved_attempt_count"],
        HIERARCHICAL_ROLE_PSEUDOCOUNT,
    )
    family_value = shrink(
        family["blended_yield"],
        role_value,
        family["lifetime"]["resolved_attempt_count"],
        HIERARCHICAL_FAMILY_PSEUDOCOUNT,
    )
    exact_value = shrink(
        exact_facts["blended_yield"],
        family_value,
        exact_facts["lifetime"]["resolved_attempt_count"],
        HIERARCHICAL_EXACT_PSEUDOCOUNT,
    )

    def reliability(items):
        resolved = tuple(item for item in items if _attempt_is_resolved(item))
        return {
            "resolved_attempt_count": len(resolved),
            "adopted_count": sum(bool(item.adopted) for item in resolved),
        }

    role_rel = reliability(role_peers)
    family_rel = reliability(family_peers)
    exact_rel = reliability(exact)
    role_reliability = (
        role_rel["adopted_count"] + HIERARCHICAL_ROLE_PSEUDOCOUNT * 0.5
    ) / (role_rel["resolved_attempt_count"] + HIERARCHICAL_ROLE_PSEUDOCOUNT)
    family_reliability = (
        family_rel["adopted_count"]
        + HIERARCHICAL_FAMILY_PSEUDOCOUNT * role_reliability
    ) / (family_rel["resolved_attempt_count"] + HIERARCHICAL_FAMILY_PSEUDOCOUNT)
    exact_reliability = (
        exact_rel["adopted_count"]
        + HIERARCHICAL_EXACT_PSEUDOCOUNT * family_reliability
    ) / (exact_rel["resolved_attempt_count"] + HIERARCHICAL_EXACT_PSEUDOCOUNT)

    return {
        "exact": exact_facts,
        "family_peers": family,
        "role_peers": role,
        "inclusive_family_attempt_count": len(family_all),
        "inclusive_role_attempt_count": len(role_all),
        "exact_hierarchical_yield": exact_value,
        "family_hierarchical_yield": family_value,
        "role_hierarchical_yield": role_value,
        "hierarchical_reliability": exact_reliability,
        "centered_hierarchical_reliability": 2.0 * exact_reliability - 1.0,
        "pseudo_counts": {
            "exact": HIERARCHICAL_EXACT_PSEUDOCOUNT,
            "family": HIERARCHICAL_FAMILY_PSEUDOCOUNT,
            "role": HIERARCHICAL_ROLE_PSEUDOCOUNT,
        },
    }


def _component_alignment_observation(state, history, strata):
    """Return a sparse-safe, session-local component alignment signal."""

    weighted = {
        str(name): max(0.0, float(value or 0.0))
        for name, value in dict(state.weighted_contributions).items()
    }
    total = sum(weighted.values())
    if total <= 0:
        remaining = {}
    else:
        remaining = {name: value / total for name, value in weighted.items()}

    exact, family_peers, role_peers, _, _ = strata

    def signature(items):
        vectors = []
        for item in items:
            if not _attempt_is_resolved(item) or not item.adopted:
                continue
            delta = dict(getattr(item, "objective_weighted_delta", {}) or {})
            magnitude = sum(abs(float(value or 0.0)) for value in delta.values())
            if magnitude <= 0:
                continue
            vectors.append({
                name: float(value or 0.0) / magnitude
                for name, value in delta.items()
            })
        if not vectors:
            return {"vector": {}, "sample_count": 0}
        names = set().union(*(vector.keys() for vector in vectors))
        return {
            "vector": {
                name: sum(vector.get(name, 0.0) for vector in vectors)
                / len(vectors)
                for name in sorted(names)
            },
            "sample_count": len(vectors),
        }

    role = signature(role_peers)
    family = signature(family_peers)
    exact = signature(exact)

    def shrink_vector(value, parent, count, pseudo):
        names = set(value) | set(parent)
        denominator = count + pseudo
        if denominator <= 0:
            return dict(parent)
        return {
            name: (
                count * value.get(name, 0.0)
                + pseudo * parent.get(name, 0.0)
            ) / denominator
            for name in sorted(names)
        }

    role_vector = shrink_vector(
        role["vector"], {}, role["sample_count"], HIERARCHICAL_ROLE_PSEUDOCOUNT
    )
    family_vector = shrink_vector(
        family["vector"], role_vector, family["sample_count"],
        HIERARCHICAL_FAMILY_PSEUDOCOUNT,
    )
    exact_vector = shrink_vector(
        exact["vector"], family_vector, exact["sample_count"],
        HIERARCHICAL_EXACT_PSEUDOCOUNT,
    )
    alignment = sum(remaining.get(name, 0.0) * value for name, value in exact_vector.items())
    return {
        "remaining_share": remaining,
        "exact_signature": exact,
        "family_peer_signature": family,
        "role_peer_signature": role,
        "hierarchical_signature": exact_vector,
        "alignment": min(1.0, max(-1.0, alignment)),
    }


def _new_evidence_guided_score(spec, state, portfolio, ranked_students, *, variant):
    """Score one operator for the versioned post-study policy ladder."""

    history = tuple(state.operator_history)
    strata = _disjoint_evidence_history(history, spec, portfolio)
    use_recent = variant in {"hierarchical_recent", "component_aware", "horizon_aware"}
    hierarchy = _hierarchical_yield_observation(
        history, strata, use_recent=use_recent
    )
    component = _component_alignment_observation(state, history, strata)
    role_signal = _role_signals(state).get(spec.portfolio_role, 0.0)
    predicted = _predicted_scope(spec, state, ranked_students)
    scope_state = _scope_status(history, _scope_key(spec, predicted, state))
    all_family = strata[3]
    unresolved_rate = _group_unknown_rate(all_family)
    family_attempts = len(all_family)
    estimated_cost = _estimated_full_cost(spec, state, history)
    budget_fit = min(1.0, state.remaining_seconds / estimated_cost)
    previous = history[-1] if history else None
    productive_continuation = bool(
        previous
        and previous.adopted
        and previous.operator == spec.name
        and previous.candidate_source_decision_fingerprint
        and previous.candidate_source_decision_fingerprint
        != previous.source_fingerprint_before
        and spec.portfolio_role != "basin_escape"
    )
    duplicate_penalty = (
        EVIDENCE_GUIDED_DUPLICATE_SCOPE_PENALTY
        if scope_state == "unresolved" else 0.0
    )
    opportunity = role_signal
    if variant == "horizon_aware":
        horizon_fraction = state.remaining_seconds / max(
            0.001, state.elapsed_seconds + state.remaining_seconds
        )
        exploration = (
            HORIZON_EXPLORATION_MAX
            * horizon_fraction ** 2
            * min(1.0, max(0.0, opportunity))
            * (1.0 / math.sqrt(1.0 + family_attempts))
            * budget_fit
        )
    else:
        exploration = 0.10 / math.sqrt(1.0 + family_attempts)
    component_term = (
        COMPONENT_ALIGNMENT_WEIGHT * component["alignment"]
        if variant in {"component_aware", "horizon_aware"}
        else 0.0
    )
    continuation_term = 0.20 if productive_continuation else 0.0
    score = (
        opportunity
        + EVIDENCE_GUIDED_OPERATOR_PRIORS.get(spec.name, 0.0)
        + 0.50 * hierarchy["exact_hierarchical_yield"]
        + 0.10 * hierarchy["centered_hierarchical_reliability"]
        + exploration
        + 0.10 * budget_fit
        - 0.25 * unresolved_rate
        - duplicate_penalty
        + component_term
        + continuation_term
    )
    return {
        "score": score,
        "operator_family": operator_family(spec),
        "opportunity": opportunity,
        "role_signal": opportunity,
        "operator_prior": EVIDENCE_GUIDED_OPERATOR_PRIORS.get(spec.name, 0.0),
        "exact_yield": _yield_observation(strata[0])["yield"],
        "family_yield": _yield_observation(strata[3])["yield"],
        "role_yield": _yield_observation(strata[4])["yield"],
        "hierarchical": hierarchy,
        "recent_productivity": {
            "enabled": use_recent,
            "window_attempts": RECENT_PRODUCTIVITY_WINDOW_ATTEMPTS,
        },
        "component_alignment": component,
        "component_alignment_term": component_term,
        "exploration": exploration,
        "budget_fit": budget_fit,
        "estimated_full_cost_seconds": estimated_cost,
        "family_attempt_count": family_attempts,
        "family_unknown_rate": unresolved_rate,
        "predicted_scope": predicted,
        "scope_state": scope_state,
        "duplicate_scope_penalty": duplicate_penalty,
        "productive_continuation": productive_continuation,
        "continuation_term": continuation_term,
        "variant": variant,
    }


def _eligibility_facts(spec, state, ranked_students, history):
    """Return selector eligibility and a stable diagnostic reason code."""

    if spec.portfolio_role == "targeted_repair":
        if len(ranked_students) < spec.student_count:
            return False, (), "insufficient_targeted_students"
        selected = tuple(item.student_id for item in ranked_students[: spec.student_count])
    elif spec.portfolio_role == "utilization_repair":
        selected = tuple(state.utilization_ranked_student_ids[: spec.student_count])
        if len(selected) < spec.student_count:
            return False, selected, "insufficient_utilization_scope"
    else:
        selected = ()
    if spec.portfolio_role == "basin_escape":
        if state.consecutive_no_improvement_attempts < 2:
            return False, selected, "basin_escape_stagnation_gate"
        if not any(
            item.get("grade_level") == spec.selected_grade
            and item.get("effective_search_available")
            for item in state.grade_opportunities
        ):
            return False, selected, "no_actionable_grade_opportunity"
    scope_state = _scope_status(history, _scope_key(spec, selected, state))
    if scope_state == "exhausted":
        return False, selected, "scope_exhausted"
    if scope_state == "non_improving":
        return False, selected, "scope_non_improving"
    estimated_cost = _estimated_full_cost(spec, state, history)
    if state.remaining_seconds + 1e-9 < estimated_cost:
        return False, selected, "insufficient_remaining_budget"
    return True, selected, None


def adaptive_selector_state_snapshot(state, *, ranked_students=(), portfolio=()):
    """Serialize only the bounded state inputs consumed by selector policy."""

    max_targeted = max(
        (spec.student_count for spec in portfolio if spec.portfolio_role == "targeted_repair"),
        default=0,
    )
    max_utilization = max(
        (spec.student_count for spec in portfolio if spec.portfolio_role == "utilization_repair"),
        default=0,
    )
    utilization_prefix = tuple(state.utilization_ranked_student_ids[:max_utilization])
    ranked_prefix = tuple(item.student_id for item in ranked_students[:max_targeted])
    return {
        "schema": ADAPTIVE_SELECTOR_STATE_SCHEMA,
        "policy_version": state.policy_version,
        "objective_semantics_version": state.objective_semantics_version,
        "counselor_scores": dict(state.counselor_scores),
        "normalized_components": dict(state.normalized_components),
        "weighted_contributions": dict(state.weighted_contributions),
        "current_objective_vector": tuple(state.current_objective_vector),
        "student_local_weighted_total": state.student_local_weighted_total,
        "highest_student_pressure": state.highest_student_pressure,
        "top_k_pressure": dict(state.top_k_pressure),
        "nonzero_pressure_student_count": state.nonzero_pressure_student_count,
        "student_local_weighted_share": state.student_local_weighted_share,
        "global_utilization_weighted_share": state.global_utilization_weighted_share,
        "elapsed_seconds": state.elapsed_seconds,
        "remaining_seconds": state.remaining_seconds,
        "substantive_aggregate": state.substantive_aggregate,
        "student_pressure_components": dict(state.student_pressure_components),
        "utilization_raw_penalty": state.utilization_raw_penalty,
        "utilization_normalized_value": state.utilization_normalized_value,
        "utilization_weighted_value": state.utilization_weighted_value,
        "pressured_delivery_group_count": state.pressured_delivery_group_count,
        "top_utilization_group_share": state.top_utilization_group_share,
        "top_three_utilization_group_share": state.top_three_utilization_group_share,
        "top_five_utilization_group_share": state.top_five_utilization_group_share,
        "optimistic_utilization_leverage": state.optimistic_utilization_leverage,
        "useful_utilization_student_count": state.useful_utilization_student_count,
        "ranked_student_ids_prefix": ranked_prefix,
        "utilization_ranked_student_ids_prefix": utilization_prefix,
        "utilization_ranked_student_count": len(state.utilization_ranked_student_ids),
        "utilization_ranked_student_fingerprint": _stable_fingerprint(
            tuple(state.utilization_ranked_student_ids)
        ),
        "grade_opportunities": tuple(state.grade_opportunities),
        "recent_operation_seconds": state.recent_operation_seconds,
        "recent_memory_peak_bytes": state.recent_memory_peak_bytes,
        "consecutive_no_improvement_attempts": state.consecutive_no_improvement_attempts,
        "unknown_streak": state.unknown_streak,
        "validation_unknown_count": state.validation_unknown_count,
        "hard_invalid_count": state.hard_invalid_count,
        "last_target_scope": tuple(state.last_target_scope),
        "last_grade": state.last_grade,
        "last_utilization_cluster": tuple(state.last_utilization_cluster),
        "estimated_operator_cost_seconds": state.estimated_operator_cost_seconds,
        "current_source_fingerprint": state.current_source_fingerprint,
        "candidate_validation_time_limit_seconds": state.candidate_validation_time_limit_seconds,
    }


def _trace_candidate_row(spec, state, portfolio, ranked_students, variant):
    history = tuple(state.operator_history)
    eligible, selected, reason = _eligibility_facts(
        spec, state, ranked_students, history
    )
    facts = (
        _evidence_guided_score(spec, state, portfolio, ranked_students)
        if variant in {"evidence_guided", "r4_anchor"}
        else _new_evidence_guided_score(
            spec, state, portfolio, ranked_students, variant=variant
        )
    )
    hierarchy = facts.get("hierarchical", {})
    compact_hierarchy = dict(hierarchy)
    for stratum_name in ("exact", "family_peers", "role_peers"):
        stratum = hierarchy.get(stratum_name)
        if not isinstance(stratum, dict):
            continue
        lifetime = stratum.get("lifetime", {})
        recent = stratum.get("recent", {})
        compact_hierarchy[stratum_name] = {
            "resolved_attempt_count": lifetime.get("resolved_attempt_count", 0),
            "resolved_elapsed_seconds": lifetime.get(
                "resolved_elapsed_seconds", 0.0
            ),
            "adopted_count": lifetime.get("adopted_count", 0),
            "yield": lifetime.get("yield", 0.0),
            "recent_resolved_attempt_count": recent.get(
                "resolved_attempt_count", 0
            ),
            "recent_yield": recent.get("yield", 0.0),
            "recent_weight": stratum.get("recent_weight", 0.0),
            "blended_yield": stratum.get("blended_yield", 0.0),
        }
    scope = tuple(facts.get("predicted_scope", selected) or selected)
    return {
        "operator": spec.name,
        "family": operator_family(spec),
        "role": spec.portfolio_role,
        "eligible": bool(eligible),
        "ineligibility_reason": reason,
        "predicted_scope": scope,
        "scope_count": len(scope),
        "scope_fingerprint": _stable_fingerprint(scope),
        "scope_state": facts.get("scope_state"),
        "opportunity": facts.get("opportunity", facts.get("role_signal", 0.0)),
        "prior": facts.get("operator_prior", 0.0),
        "exact_yield": facts.get("operator_yield", facts.get("exact_yield", 0.0)),
        "family_yield": facts.get("family_yield", 0.0),
        "role_yield": facts.get("role_yield", 0.0),
        "recent_productivity": facts.get("recent_productivity", {}),
        "hierarchical_evidence": compact_hierarchy,
        "reliability": facts.get(
            "family_centered_reliability",
            facts.get("hierarchical", {}).get("centered_hierarchical_reliability", 0.0),
        ),
        "unresolved_rate": facts.get("family_unknown_rate", 0.0),
        "exploration": facts.get(
            "exploration",
            0.10 / math.sqrt(1.0 + facts.get("family_attempt_count", 0)),
        ),
        "budget_fit": facts.get("budget_fit", 0.0),
        "component_alignment": facts.get("component_alignment", {}),
        "component_alignment_term": facts.get("component_alignment_term", 0.0),
        "duplicate_scope_penalty": facts.get("duplicate_scope_penalty", 0.0),
        "continuation_term": facts.get(
            "continuation_term",
            0.20 if facts.get("productive_continuation") else 0.0,
        ),
        "estimated_full_cost_seconds": facts.get(
            "estimated_full_cost_seconds", 0.0
        ),
        "family_attempt_count": facts.get("family_attempt_count", 0),
        "score": round(float(facts.get("score", 0.0)), 9) if eligible else None,
        "tie_break": None,
        "selected_student_ids": tuple(selected),
    }


def build_adaptive_competition_trace(
    state,
    *,
    portfolio=DEFAULT_ADAPTIVE_OPERATOR_PORTFOLIO,
    ranked_students=(),
    adaptive_policy_variant="evidence_guided",
):
    """Build a bounded, solver-free competition trace for one decision."""

    rows = [
        _trace_candidate_row(
            spec,
            state,
            tuple(portfolio),
            ranked_students,
            adaptive_policy_variant,
        )
        for spec in tuple(portfolio)
    ]

    def key(row):
        return (
            float(row["score"] if row["score"] is not None else -math.inf),
            -float(row["estimated_full_cost_seconds"]),
            -float(
                next(
                    (spec.radius or 0 for spec in portfolio if spec.name == row["operator"]),
                    0,
                )
            ),
            row["operator"],
        )

    eligible = sorted(
        (row for row in rows if row["eligible"]), key=key, reverse=True
    )
    for index, row in enumerate(eligible):
        row["rank"] = index + 1
        row["tie_break"] = key(row)

    score_winner = eligible[0] if eligible else None
    no_prior_winner = (
        max(
            eligible,
            key=lambda row: (
                float(row["score"] or 0.0) - float(row["prior"] or 0.0),
                -float(row["estimated_full_cost_seconds"]),
                -float(next((spec.radius or 0 for spec in portfolio if spec.name == row["operator"]), 0)),
                row["operator"],
            ),
        ) if eligible else None
    )
    opportunity_winner = (
        max(
            eligible,
            key=lambda row: (
                float(row["opportunity"]),
                -float(row["estimated_full_cost_seconds"]),
                -float(next((spec.radius or 0 for spec in portfolio if spec.name == row["operator"]), 0)),
                row["operator"],
            ),
        ) if eligible else None
    )
    evidence_winner = (
        max(
            eligible,
            key=lambda row: (
                float(row["opportunity"])
                + float(row.get("hierarchical_evidence", {}).get("exact_hierarchical_yield", 0.0)) * 0.50
                + float(row.get("reliability", 0.0)) * 0.10
                - float(row.get("unresolved_rate", 0.0)) * 0.25,
                -float(row["estimated_full_cost_seconds"]),
                -float(next((spec.radius or 0 for spec in portfolio if spec.name == row["operator"]), 0)),
                row["operator"],
            ),
        ) if eligible else None
    )

    def compact(row):
        if row is None:
            return None
        return {
            "operator": row["operator"],
            "family": row["family"],
            "role": row["role"],
            "score": row["score"],
            "opportunity": row["opportunity"],
            "scope_fingerprint": row["scope_fingerprint"],
            "rank": row.get("rank"),
        }

    winner_margin = None
    if len(eligible) > 1:
        winner_margin = round(
            float(eligible[0]["score"]) - float(eligible[1]["score"]), 9
        )
    return {
        "schema": ADAPTIVE_SELECTOR_TRACE_SCHEMA,
        "adaptive_policy_variant": adaptive_policy_variant,
        "portfolio": tuple(asdict(spec) for spec in portfolio),
        "state": adaptive_selector_state_snapshot(
            state, ranked_students=ranked_students, portfolio=portfolio
        ),
        "history_prefix_count": len(state.operator_history),
        "history_digest": _stable_fingerprint(
            [asdict(item) for item in state.operator_history]
        ),
        "candidates": tuple(rows),
        "derived": {
            "score_winner": compact(score_winner),
            "runner_up": compact(eligible[1] if len(eligible) > 1 else None),
            "winner_margin": winner_margin,
            "top_three": tuple(compact(row) for row in eligible[:3]),
            "winner_with_prior": compact(score_winner),
            "winner_without_prior": compact(no_prior_winner),
            "opportunity_only_winner": compact(opportunity_winner),
            "opportunity_plus_evidence_winner": compact(evidence_winner),
        },
        "trace_complete": True,
    }


def _with_competition_trace(decision, trace, *, selection_override=None):
    trace = dict(trace)
    derived = dict(trace.get("derived") or {})
    executed = {
        "operator": decision.operator.name,
        "family": operator_family(decision.operator),
        "role": decision.operator.portfolio_role,
        "score": decision.score,
        "selected_student_ids": tuple(decision.selected_student_ids),
    }
    derived["executed_winner"] = executed
    score_winner = derived.get("score_winner") or {}
    derived["continuation_override"] = bool(
        "productive_validated_continuation" in decision.reasons
        and score_winner.get("operator") != decision.operator.name
    )
    trace["derived"] = derived
    signal_values = dict(decision.signal_values)
    signal_values["competition_trace"] = trace
    if selection_override is not None:
        signal_values["competition_selection_override"] = selection_override
    return replace(decision, signal_values=signal_values)


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
        decision = _new_policy_decision(
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
        return _with_competition_trace(
            decision,
            build_adaptive_competition_trace(
                state,
                portfolio=portfolio,
                ranked_students=ranked_students,
                adaptive_policy_variant=adaptive_policy_variant,
            ),
            selection_override="productive_validated_continuation",
        )

    if adaptive_policy_variant == "r4_anchor" and not history:
        anchored = next((item for item in portfolio if item.name == "targeted_r4_s2"), None)
        if anchored is not None:
            selected = _eligible_operator_selection(anchored, state, ranked_students, history)
            if selected is not None:
                facts = _evidence_guided_score(anchored, state, portfolio, ranked_students)
                decision = _new_policy_decision(
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
                return _with_competition_trace(
                    decision,
                    build_adaptive_competition_trace(
                        state,
                        portfolio=portfolio,
                        ranked_students=ranked_students,
                        adaptive_policy_variant=adaptive_policy_variant,
                    ),
                    selection_override="r4_anchor",
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
    decision = max(
        candidates,
        key=lambda decision: (
            decision.score,
            -_estimated_full_cost(decision.operator, state, history),
            -(decision.operator.radius or 0),
            decision.operator.name,
        ),
    )
    return _with_competition_trace(
        decision,
        build_adaptive_competition_trace(
            state,
            portfolio=portfolio,
            ranked_students=ranked_students,
            adaptive_policy_variant=adaptive_policy_variant,
        ),
    )


def _choose_versioned_evidence_operator(
    state,
    *,
    portfolio,
    ranked_students,
    adaptive_policy_variant,
):
    """Choose one operator using a new, immutable ladder configuration."""

    trace = build_adaptive_competition_trace(
        state,
        portfolio=portfolio,
        ranked_students=ranked_students,
        adaptive_policy_variant=adaptive_policy_variant,
    )
    continuation = _productive_continuation_operator(
        state, portfolio, ranked_students
    )
    if continuation is not None:
        spec, selected, consecutive = continuation
        facts = _new_evidence_guided_score(
            spec,
            state,
            portfolio,
            ranked_students,
            variant=adaptive_policy_variant,
        )
        decision = _new_policy_decision(
            spec,
            selected,
            facts["score"],
            ("productive_validated_continuation", "fresh_source_incumbent"),
            {
                "selected_role": spec.portfolio_role,
                "adaptive_policy_variant": adaptive_policy_variant,
                "operator_family": facts["operator_family"],
                "evidence_guided": facts,
                "continuation_count": consecutive,
            },
        )
        return _with_competition_trace(
            decision,
            trace,
            selection_override="productive_validated_continuation",
        )

    winner = trace["derived"].get("score_winner")
    if winner is None:
        return None
    row = next(
        item for item in trace["candidates"]
        if item["operator"] == winner["operator"]
    )
    spec = next(item for item in portfolio if item.name == row["operator"])
    selected = tuple(row.get("selected_student_ids") or ())
    # Candidate rows intentionally contain only compact telemetry. Rebuild
    # the full scoring facts for the selected decision without storing a
    # second copy in the trace.
    facts = _new_evidence_guided_score(
        spec,
        state,
        portfolio,
        ranked_students,
        variant=adaptive_policy_variant,
    )
    reasons = ["bounded_current_opportunity", "hierarchical_evidence_score"]
    if facts.get("operator_prior"):
        reasons.append("historical_operator_prior")
    if facts.get("family_attempt_count", 0) == 0:
        reasons.append("family_exploration_bonus")
    decision = _new_policy_decision(
        spec,
        selected,
        row["score"],
        reasons,
        {
            "selected_role": spec.portfolio_role,
            "adaptive_policy_variant": adaptive_policy_variant,
            "operator_family": row["family"],
            "evidence_guided": facts,
            "role_signals": _role_signals(state),
        },
    )
    return _with_competition_trace(decision, trace)


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
    current_objective_vector=(),
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
        current_objective_vector=tuple(current_objective_vector or ()),
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
    if adaptive_policy_variant in {
        "hierarchical_evidence",
        "hierarchical_recent",
        "component_aware",
        "horizon_aware",
    }:
        return _choose_versioned_evidence_operator(
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
    enforced_student_scope=None,
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
    if enforced_student_scope is None:
        enforced_student_scope = selected_student_ids
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
        "enforced_student_scope": tuple(enforced_student_scope),
        "cp_sat_random_seed": (
            int(cp_sat_random_seed) if cp_sat_random_seed is not None else None
        ),
        "cp_sat_max_deterministic_time_seconds": (
            float(cp_sat_max_deterministic_time_seconds)
            if cp_sat_max_deterministic_time_seconds is not None
            else None
        ),
    }


def _attempt_from_record(item):
    """Convert a JSON attempt while preserving every replay-relevant field."""

    item = dict(item or {})
    validation_classification = str(
        item.get("validation_classification", "not_attempted")
    )
    unresolved = bool(
        item.get("unknown", False)
        or item.get("search_unknown", False)
        or item.get("status") == "unknown"
        or validation_classification in {
            "validation_unknown", "validation_error", "scope_mismatch"
        }
    )
    return AdaptiveOperatorAttempt(
        operator=item["operator"],
        status=item.get("status", "unknown"),
        candidate_found=bool(item.get("candidate_found")),
        candidate_validated=bool(item.get("candidate_validated")),
        adopted=bool(item.get("candidate_adopted", item.get("adopted", False))),
        gain=float(item.get("gain", 0) or 0),
        elapsed_seconds=float(
            item.get("total_operation_seconds", item.get("elapsed_seconds", 0))
            or 0
        ),
        solver_wall_time_seconds=item.get("solver_wall_time_seconds"),
        validation_seconds=item.get("validation_seconds"),
        changed_student_count=int(item.get("changed_student_count", 0) or 0),
        changed_source_decision_count=int(
            item.get("changed_source_decision_count", 0) or 0
        ),
        unknown=unresolved,
        infeasible=bool(item.get("infeasible", item.get("status") == "infeasible")),
        stopping_reason=item.get("stopping_reason"),
        role_specific_gain=float(item.get("role_specific_gain", 0) or 0),
        validation_classification=validation_classification,
        validation_solver_outcome=item.get("validation_solver_outcome"),
        validation_error=item.get("validation_error"),
        target_scope=tuple(item.get("target_scope", ())),
        actual_target_scope=tuple(item.get("actual_target_scope", ())),
        scope_equal=item.get("scope_equal"),
        scope_mismatch=bool(item.get("scope_mismatch", False)),
        source_fingerprint_before=item.get("source_fingerprint_before"),
        candidate_source_decision_fingerprint=item.get(
            "candidate_source_decision_fingerprint"
        ),
        selected_grade=item.get("selected_grade"),
        utilization_cluster=tuple(item.get("utilization_cluster", ()) or ()),
        session_attempt_count=int(item.get("session_attempt_count", 0) or 0),
        session_adopted_count=int(item.get("session_adopted_count", 0) or 0),
        session_requested_seconds=item.get("session_requested_seconds"),
        session_cp_sat_seconds=item.get("session_cp_sat_seconds"),
        session_validation_seconds=item.get("session_validation_seconds"),
        session_external_overrun_seconds=item.get("session_external_overrun_seconds"),
        cp_sat_random_seed=item.get("cp_sat_random_seed"),
        cp_sat_max_deterministic_time_seconds=item.get(
            "cp_sat_max_deterministic_time_seconds"
        ),
        inner_probe_summaries=tuple(item.get("inner_probe_summaries", ()) or ()),
        role_pressure_before=dict(item.get("role_pressure_before", {}) or {}),
        role_pressure_after=dict(item.get("role_pressure_after", {}) or {}),
        exhaustion_classification=item.get(
            "exhaustion_classification", "OPERATOR_UNRESOLVED"
        ),
        role_exhaustion_classification=item.get(
            "role_exhaustion_classification", "ROLE_EXHAUSTION_NOT_PROVEN"
        ),
        sequence_position=item.get("sequence_position"),
        operator_family=item.get("operator_family"),
        candidate_discovery_gain=float(item.get("candidate_discovery_gain", 0) or 0),
        search_unknown=bool(item.get("search_unknown", item.get("status") == "unknown")),
        validation_retry_count=int(item.get("validation_retry_count", 0) or 0),
        validation_retry_facts=dict(item.get("validation_retry_facts", {}) or {}),
        objective_weighted_delta=dict(item.get("objective_weighted_delta", {}) or {}),
        objective_normalized_delta=dict(item.get("objective_normalized_delta", {}) or {}),
    )


def replay_adaptive_policy(records, *, portfolio=DEFAULT_ADAPTIVE_OPERATOR_PORTFOLIO):
    """Replay policy decisions from structured records without solving."""

    return tuple(_attempt_from_record(item) for item in records)


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
                    elapsed_seconds=float(
                        item.get(
                            "elapsed_seconds",
                            item.get("total_operation_seconds", 0),
                        )
                        or 0
                    ),
                    unknown=(
                        item.get("status") == "unknown"
                        or item.get("validation_classification") in {
                            "validation_unknown", "scope_mismatch"
                        }
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
                    scope_equal=item.get("scope_equal"),
                    scope_mismatch=bool(item.get("scope_mismatch", False)),
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


def _state_from_selector_snapshot(snapshot, history):
    """Rehydrate the bounded selector state used by solver-free replay."""

    snapshot = dict(snapshot or {})
    values = {}
    for name, definition in AdaptiveSearchState.__dataclass_fields__.items():
        if definition.default is not MISSING:
            values[name] = definition.default
        elif definition.default_factory is not MISSING:
            values[name] = definition.default_factory()
    values.update({
        "policy_version": snapshot.get("policy_version", ADAPTIVE_POLICY_VERSION),
        "objective_semantics_version": snapshot.get(
            "objective_semantics_version", "v2"
        ),
        "counselor_scores": dict(snapshot.get("counselor_scores") or {}),
        "normalized_components": dict(snapshot.get("normalized_components") or {}),
        "weighted_contributions": dict(snapshot.get("weighted_contributions") or {}),
        "student_local_weighted_total": float(
            snapshot.get("student_local_weighted_total", 0.0) or 0.0
        ),
        "highest_student_pressure": float(
            snapshot.get("highest_student_pressure", 0.0) or 0.0
        ),
        "top_k_pressure": dict(snapshot.get("top_k_pressure") or {}),
        "nonzero_pressure_student_count": int(
            snapshot.get("nonzero_pressure_student_count", 0) or 0
        ),
        "student_local_weighted_share": float(
            snapshot.get("student_local_weighted_share", 0.0) or 0.0
        ),
        "global_utilization_weighted_share": float(
            snapshot.get("global_utilization_weighted_share", 0.0) or 0.0
        ),
        "elapsed_seconds": float(snapshot.get("elapsed_seconds", 0.0) or 0.0),
        "remaining_seconds": float(snapshot.get("remaining_seconds", 0.0) or 0.0),
        "student_pressure_components": dict(
            snapshot.get("student_pressure_components") or {}
        ),
        "grade_opportunities": tuple(snapshot.get("grade_opportunities") or ()),
        "utilization_ranked_student_ids": tuple(
            snapshot.get("utilization_ranked_student_ids_prefix") or ()
        ),
        "last_target_scope": tuple(snapshot.get("last_target_scope") or ()),
        "last_utilization_cluster": tuple(
            snapshot.get("last_utilization_cluster") or ()
        ),
        "current_objective_vector": tuple(
            snapshot.get("current_objective_vector") or ()
        ),
        "operator_history": tuple(history),
    })
    # The snapshot stores the ranked prefix separately because it is a
    # selector input rather than a field on AdaptiveSearchState.  The replay
    # caller supplies it as SimpleNamespace rows.
    return AdaptiveSearchState(**values)


def replay_selector_decision(trace, attempts=(), *, policy_variant=None):
    """Replay one complete selector trace without CP-SAT or validation."""

    trace = dict(trace or {})
    snapshot = dict(trace.get("state") or {})
    expected_count = int(trace.get("history_prefix_count", 0) or 0)
    attempt_prefix = tuple(attempts)[:expected_count]
    history = (
        tuple(attempts)
        if attempt_prefix and isinstance(attempt_prefix[0], AdaptiveOperatorAttempt)
        else replay_adaptive_policy(attempt_prefix)
    )
    missing = []
    if trace.get("schema") != ADAPTIVE_SELECTOR_TRACE_SCHEMA:
        missing.append("trace.schema")
    if not snapshot:
        missing.append("trace.state")
    if not trace.get("portfolio"):
        missing.append("trace.portfolio")
    if len(attempt_prefix) != expected_count:
        missing.append("history_prefix")
    expected_digest = trace.get("history_digest")
    actual_digest = _stable_fingerprint([asdict(item) for item in history])
    if expected_digest and expected_digest != actual_digest:
        missing.append("history_digest")
    if missing:
        return {
            "schema": "adaptive_selector_replay_v1",
            "replay_classification": "unavailable",
            "missing_fields": tuple(missing),
            "schedule_outcome_inferred": False,
        }
    state = _state_from_selector_snapshot(snapshot, history)
    ranked = tuple(
        SimpleNamespace(student_id=student_id)
        for student_id in snapshot.get("ranked_student_ids_prefix", ())
    )
    portfolio = tuple(
        AdaptiveOperatorSpec(**dict(spec)) for spec in trace.get("portfolio", ())
    )
    variant = policy_variant or trace.get("adaptive_policy_variant", "evidence_guided")
    decision = choose_adaptive_operator(
        state,
        portfolio=portfolio,
        ranked_students=ranked,
        adaptive_policy_variant=variant,
    )
    original = (trace.get("derived") or {}).get("score_winner")
    return {
        "schema": "adaptive_selector_replay_v1",
        "replay_classification": "exact",
        "missing_fields": (),
        "original_score_winner": original,
        "replay_selected_operator": decision.operator.name if decision else None,
        "replay_selected_student_ids": (
            tuple(decision.selected_student_ids) if decision else ()
        ),
        "replay_score": decision.score if decision else None,
        "selection_matches_original_score_winner": bool(
            decision and original and decision.operator.name == original["operator"]
        ),
        "competition_trace": (
            decision.signal_values.get("competition_trace") if decision else None
        ),
        "schedule_outcome_inferred": False,
    }


def replay_selector_artifact(artifact, *, policy_variants=(), decision_indices=None):
    """Replay prospective traces or classify legacy artifacts as partial."""

    artifact = dict(artifact or {})
    decisions = tuple(artifact.get("decisions") or ())
    attempts = tuple(artifact.get("attempts") or ())
    requested = tuple(policy_variants or ())
    indices = (
        tuple(range(len(decisions)))
        if decision_indices is None
        else tuple(int(index) for index in decision_indices)
    )
    output = []
    for index in indices:
        if index < 0 or index >= len(decisions):
            output.append({
                "decision_index": index,
                "replay_classification": "unavailable",
                "missing_fields": ("decision_index",),
                "schedule_outcome_inferred": False,
            })
            continue
        decision = dict(decisions[index] or {})
        trace = dict(
            (decision.get("signal_values") or {}).get("competition_trace") or {}
        )
        if not trace:
            output.append({
                "decision_index": index,
                "replay_classification": "partial",
                "missing_fields": ("competition_trace", "state_snapshot"),
                "original_selected_operator": (
                    (decision.get("operator") or {}).get("name")
                ),
                "schedule_outcome_inferred": False,
            })
            continue
        variants = requested or (trace.get("adaptive_policy_variant", "evidence_guided"),)
        for variant in variants:
            row = replay_selector_decision(
                trace,
                attempts,
                policy_variant=variant,
            )
            row["decision_index"] = index
            row["policy_variant"] = variant
            row["original_selected_operator"] = (
                (decision.get("operator") or {}).get("name")
            )
            output.append(row)
    return {
        "schema": "adaptive_selector_replay_v1",
        "schedule_outcome_inferred": False,
        "results": tuple(output),
    }


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
    "ADAPTIVE_NEW_POLICY_VERSIONS",
    "ADAPTIVE_POLICY_VARIANTS",
    "ADAPTIVE_POLICY_LADDER_VARIANTS",
    "ALL_ADAPTIVE_POLICY_VARIANTS",
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
    "adaptive_selector_state_snapshot",
    "build_adaptive_competition_trace",
    "replay_selector_decision",
    "replay_selector_artifact",
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
