"""Pure-engine evidence records for student-search characterization.

This module does not build a second solver model, choose production targets,
or authorize a schedule.  Its optional trial helper delegates to the existing
diagnostic operator-session wrapper; CP-SAT and the unchanged full-model
validator remain the candidate authority.  Keeping the record and aggregation
logic here makes capability cards and adaptive-readiness comparisons
reproducible without introducing backend persistence or policy.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from statistics import median

from .search_guidance import rank_students_by_quality_pressure


CHARACTERIZATION_SCHEMA = "student_assignment_operator_characterization_v1"

OPERATOR_ROLES = {
    "r2": "local_descent",
    "targeted_r4_s1": "student_pressure_repair",
    "targeted_r8_s1": "student_pressure_repair",
    "targeted_r4_s2": "student_pressure_repair",
    "targeted_r8_s2": "student_pressure_repair",
    "targeted_utilization_r16_s2": "section_utilization_repair",
    "targeted_utilization_r16_s4": "section_utilization_repair",
    "targeted_utilization_r32_s4": "section_utilization_repair",
    "targeted_utilization_r32_s6": "section_utilization_repair",
    "targeted_utilization_r64_s6": "section_utilization_repair",
    "targeted_utilization_r64_s8": "section_utilization_repair",
    "targeted_utilization_r64_s10": "section_utilization_repair",
    "grade_bounded_g9": "basin_escape",
    "grade_bounded_g10": "basin_escape",
    "grade_bounded_g11": "basin_escape",
    "grade_bounded_g12": "basin_escape",
}

_COMPONENT_KEYS = (
    "section_utilization_balance_penalty",
    "student_semester_balance_penalty",
    "difficulty_balance_penalty",
    "course_category_diversity_penalty",
    "course_sequence_preferences_penalty",
)


def _json_safe(value):
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_safe(item) for item in value]
    if isinstance(value, set):
        return [_json_safe(item) for item in sorted(value, key=repr)]
    return value


def _number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _integer(value, default=0):
    return int(round(_number(value, default)))


def _objective_components(quality_report):
    return quality_report.get("objective_semantics", {}).get("components", {})


def _weighted_substantive_value(quality_report):
    components = _objective_components(quality_report)
    substantive_names = {
        "section_utilization_balance",
        "student_semester_load_balance",
        "difficulty_balance",
        "course_category_diversity",
        "course_sequence_preferences",
    }
    return sum(
        _number(facts.get("weighted_normalized_contribution"))
        for name, facts in components.items()
        if name in substantive_names and isinstance(facts, dict)
    )


def _component_values(quality_report):
    components = _objective_components(quality_report)
    values = {}
    for component_name, facts in components.items():
        if not isinstance(facts, dict):
            continue
        raw_name = component_name
        if component_name == "student_semester_load_balance":
            raw_name = "student_semester_balance"
        values[f"{raw_name}_penalty"] = _integer(facts.get("raw_penalty"))
    for key in _COMPONENT_KEYS:
        values.setdefault(key, 0)
    return values


def _component_facts(quality_report):
    """Return raw, normalized, and weighted facts for every v2 component."""

    facts = {}
    for name, value in _objective_components(quality_report).items():
        if not isinstance(value, dict):
            continue
        facts[name] = {
            "raw_penalty": _integer(value.get("raw_penalty")),
            "normalized_penalty": _integer(value.get("normalized_penalty")),
            "weighted_normalized_contribution": _integer(
                value.get("weighted_normalized_contribution")
            ),
        }
    return facts


def _student_pressure_facts(data, quality_report):
    ranked = rank_students_by_quality_pressure(data, quality_report)
    total = sum(item.weighted_current_penalty for item in ranked)
    positive = tuple(item for item in ranked if item.weighted_current_penalty > 0)
    return {
        "total_weighted_pressure": total,
        "nonzero_pressure_student_count": len(positive),
        "meaningful_opportunity_student_count": sum(
            item.opportunity_signal > 0 for item in ranked
        ),
        "top_1_share": (
            ranked[0].weighted_current_penalty / total
            if ranked and total else 0.0
        ),
        "top_2_share": (
            sum(item.weighted_current_penalty for item in ranked[:2]) / total
            if total else 0.0
        ),
        "top_5_share": (
            sum(item.weighted_current_penalty for item in ranked[:5]) / total
            if total else 0.0
        ),
        "top_10_share": (
            sum(item.weighted_current_penalty for item in ranked[:10]) / total
            if total else 0.0
        ),
        "ranked_student_ids": tuple(item.student_id for item in ranked[:10]),
    }


def _role_value(data, quality_report, role):
    if role == "student_pressure_repair":
        return _student_pressure_facts(data, quality_report)["total_weighted_pressure"]
    if role == "section_utilization_repair":
        return _component_values(quality_report)["section_utilization_balance_penalty"]
    if role == "basin_escape":
        return _weighted_substantive_value(quality_report)
    return _weighted_substantive_value(quality_report)


def _role_facts(data, quality_report, role):
    if role == "student_pressure_repair":
        return {"student_local": _student_pressure_facts(data, quality_report)}
    if role == "section_utilization_repair":
        return {
            "section_utilization": _component_facts(quality_report).get(
                "section_utilization_balance", {}
            )
        }
    return {"substantive": _component_facts(quality_report)}


def _first_improvement_seconds(attempts):
    for attempt in attempts:
        if attempt.get("adopted") or attempt.get("candidate_adopted"):
            return attempt.get(
                "cumulative_session_elapsed_seconds",
                attempt.get("elapsed_seconds", attempt.get("total_operation_seconds")),
            )
    return None


def _last_observed_attempt_fact(local, attempts, key):
    """Read a session-level fact, falling back to its final attempt.

    Continuous sessions keep model/solver facts at attempt scope because each
    probe is independently measurable.  Older summaries did not duplicate
    those facts at the session root, so characterization must not turn a
    present measurement into ``None`` merely because the summary was compact.
    """

    value = local.get(key)
    if value is not None:
        return value
    for attempt in reversed(attempts):
        if attempt.get(key) is not None:
            return attempt.get(key)
    return None


def summarize_stagnation(attempts):
    """Classify observed progress without claiming mathematical optimality."""

    attempts = tuple(attempts or ())
    adopted = tuple(
        bool(item.get("adopted", item.get("candidate_adopted", False)))
        for item in attempts
    )
    unknown_count = sum(str(item.get("status")) == "unknown" for item in attempts)
    no_gain_streak = 0
    longest_no_gain_streak = 0
    for value in adopted:
        if value:
            no_gain_streak = 0
        else:
            no_gain_streak += 1
            longest_no_gain_streak = max(longest_no_gain_streak, no_gain_streak)
    if not attempts:
        classification = "unobserved"
    elif unknown_count and not any(adopted):
        classification = "unresolved"
    elif any(adopted) and longest_no_gain_streak <= 1:
        classification = "productive"
    elif any(adopted):
        classification = "diminishing_or_stagnant"
    else:
        classification = "stagnant_or_unresolved"
    return {
        "attempt_count": len(attempts),
        "adopted_count": sum(adopted),
        "unknown_count": unknown_count,
        "longest_no_gain_streak": longest_no_gain_streak,
        "classification": classification,
        "mathematical_optimality_claim": False,
    }


def estimate_attempts_per_time_window(records, windows=(60, 300, 600, 1800)):
    """Estimate attempts from observed median operation cost.

    The result is explicitly an estimate.  It is not used to change a solver
    budget or to imply that future attempts have identical cost.
    """

    costs = tuple(
        _number(record.get("total_operation_seconds"))
        for record in records
        if _number(record.get("total_operation_seconds")) > 0
    )
    if not costs:
        return {str(window): None for window in windows}
    typical = median(costs)
    return {
        str(window): max(0, int(float(window) // typical))
        for window in windows
    }


@dataclass(frozen=True)
class OperatorCharacterizationRecord:
    """One JSON-safe trial record enriched with role-specific evidence."""

    schema: str
    benchmark_name: str
    input_fingerprint: str
    source_seed_fingerprint: str | None
    objective_semantics_version: str
    operator: str
    role: str
    counselor_profile: dict
    ranking_policy: str
    target_policy: str
    utilization_cluster_policy: str
    selected_student_ids: tuple
    selected_grade: int | None
    radius: int | None
    max_changed_students: int | None
    starting_value: float
    final_value: float
    total_gain: float
    starting_components: dict
    final_components: dict
    component_deltas: dict
    starting_role_value: float
    final_role_value: float
    role_specific_gain: float
    starting_role_facts: dict
    final_role_facts: dict
    starting_pressure: dict
    final_pressure: dict
    candidate_found: bool
    candidate_validated: bool
    candidate_adopted: bool
    solver_status: str
    first_improvement_seconds: float | None
    total_operation_seconds: float | None
    cp_sat_wall_time_seconds: float | None
    full_validation_seconds: float | None
    model_variable_count: int | None
    model_constraint_count: int | None
    branches: int | None
    conflicts: int | None
    validation_classification: str = "not_attempted"
    validation_solver_outcome: str | None = None
    validation_error: str | None = None
    resource: dict = field(default_factory=dict)
    attempts: tuple[dict, ...] = ()
    stagnation: dict = field(default_factory=dict)
    downstream: dict = field(default_factory=dict)
    target_history: tuple = ()

    def to_dict(self):
        return _json_safe(asdict(self))

    def to_json(self):
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


def build_operator_characterization_record(
    *,
    data,
    initial_quality,
    final_quality,
    result,
    benchmark_name,
    operator,
    input_fingerprint,
    source_seed_fingerprint=None,
    ranking_policy="v2_counselor_weighted_pressure",
    target_policy=None,
    selected_student_ids=(),
    selected_grade=None,
    external_elapsed_seconds=None,
    downstream=None,
):
    """Build one characterization row from existing result/quality facts."""

    role = OPERATOR_ROLES.get(operator, "unknown")
    local = dict((result.optimization_facts or {}).get("stage_2_local_bootstrap") or {})
    attempts = tuple(local.get("iterations") or ())
    target_history = tuple(local.get("session_target_history") or ())
    observed_targets = tuple(
        sorted({student_id for target in target_history for student_id in target})
    )
    starting_components = _component_values(initial_quality)
    final_components = _component_values(final_quality)
    component_deltas = {
        key: final_components.get(key, 0) - starting_components.get(key, 0)
        for key in sorted(set(starting_components) | set(final_components))
    }
    starting_pressure = _student_pressure_facts(data, initial_quality)
    final_pressure = _student_pressure_facts(data, final_quality)
    starting_role_value = _role_value(data, initial_quality, role)
    final_role_value = _role_value(data, final_quality, role)
    starting_role_facts = _role_facts(data, initial_quality, role)
    final_role_facts = _role_facts(data, final_quality, role)
    final_attempt = attempts[-1] if attempts else {}
    start_value = _weighted_substantive_value(initial_quality)
    final_value = _weighted_substantive_value(final_quality)
    return OperatorCharacterizationRecord(
        schema=CHARACTERIZATION_SCHEMA,
        benchmark_name=benchmark_name,
        input_fingerprint=input_fingerprint,
        source_seed_fingerprint=source_seed_fingerprint,
        objective_semantics_version=data.objective_semantics_version,
        operator=operator,
        role=role,
        counselor_profile=dict(data.objective_importance_scores),
        ranking_policy=ranking_policy,
        target_policy=str(target_policy or local.get("target_policy") or "dynamic"),
        utilization_cluster_policy=str(
            local.get("utilization_cluster_policy") or "not_applicable"
        ),
        selected_student_ids=tuple(selected_student_ids) or observed_targets,
        selected_grade=selected_grade,
        radius=local.get("neighborhood_radius"),
        max_changed_students=local.get("max_changed_students"),
        starting_value=start_value,
        final_value=final_value,
        total_gain=start_value - final_value,
        starting_components=starting_components,
        final_components=final_components,
        component_deltas=component_deltas,
        starting_role_value=starting_role_value,
        final_role_value=final_role_value,
        role_specific_gain=starting_role_value - final_role_value,
        starting_role_facts=starting_role_facts,
        final_role_facts=final_role_facts,
        starting_pressure=starting_pressure,
        final_pressure=final_pressure,
        candidate_found=bool(local.get("candidate_found", False)),
        candidate_validated=bool(local.get("candidate_validated", False)),
        candidate_adopted=bool(local.get("improvement_adopted", False)),
        solver_status=str(local.get("status") or result.solver_outcome or "unknown"),
        first_improvement_seconds=_first_improvement_seconds(attempts),
        total_operation_seconds=(
            external_elapsed_seconds
            if external_elapsed_seconds is not None
            else local.get("deadline_elapsed_seconds")
        ),
        cp_sat_wall_time_seconds=local.get("solver_wall_time_seconds"),
        full_validation_seconds=local.get("validation_elapsed_seconds"),
        model_variable_count=_last_observed_attempt_fact(
            local, attempts, "model_variable_count"
        ),
        model_constraint_count=_last_observed_attempt_fact(
            local, attempts, "model_constraint_count"
        ),
        branches=_last_observed_attempt_fact(local, attempts, "branches"),
        conflicts=_last_observed_attempt_fact(local, attempts, "conflicts"),
        validation_classification=str(
            final_attempt.get("validation_classification")
            or local.get("validation_classification")
            or "not_attempted"
        ),
        validation_solver_outcome=(
            final_attempt.get("validation_solver_outcome")
            or local.get("validation_solver_outcome")
        ),
        validation_error=(
            final_attempt.get("validation_error")
            or local.get("validation_error")
        ),
        resource=dict(local.get("memory") or {}),
        attempts=attempts,
        stagnation=summarize_stagnation(attempts),
        downstream=dict(downstream or {}),
        target_history=target_history,
    )


def run_operator_characterization_trial(
    data,
    *,
    initial_result,
    initial_source_decisions,
    benchmark_name,
    operator,
    input_fingerprint=None,
    source_seed_fingerprint=None,
    ranking_policy="v2_counselor_weighted_pressure",
    selected_student_ids=(),
    selected_grade=None,
    utilization_cluster_policy="interaction_aware",
    total_time_limit_seconds=60.0,
    max_attempts=1,
    per_attempt_time_limit_seconds=30.0,
    worker_count=8,
    target_policy="dynamic",
    collect_resource_telemetry=False,
    hard_feasibility_validation_time_limit_seconds=None,
    hard_feasibility_validation_worker_count=None,
    downstream=None,
):
    """Run one existing diagnostic operator and build its evidence record.

    This is deliberately an offline characterization boundary.  It requires
    an already-complete incumbent, invokes the existing session wrapper, and
    never persists or authorizes the returned candidate.  No production
    adaptive policy calls this function.
    """

    from time import monotonic

    from .core import run_student_assignment_operator_session_diagnostic
    from .quality import evaluate_student_assignment_quality
    from .runtime import semantic_student_assignment_input_fingerprint

    initial_quality = evaluate_student_assignment_quality(
        data,
        assignments=initial_result.assignments,
        commitment_assignments=initial_result.commitment_assignments,
        solver_objective_components=initial_result.objective_components,
    )
    started = monotonic()
    result = run_student_assignment_operator_session_diagnostic(
        data,
        operator_family=operator,
        initial_source_decisions=initial_source_decisions,
        selected_student_ids=selected_student_ids,
        target_policy=target_policy,
        selected_grade=selected_grade,
        utilization_cluster_policy=utilization_cluster_policy,
        total_time_limit_seconds=total_time_limit_seconds,
        max_attempts=max_attempts,
        per_attempt_time_limit_seconds=per_attempt_time_limit_seconds,
        worker_count=worker_count,
        collect_resource_telemetry=collect_resource_telemetry,
        hard_feasibility_validation_time_limit_seconds=(
            hard_feasibility_validation_time_limit_seconds
        ),
        hard_feasibility_validation_worker_count=(
            hard_feasibility_validation_worker_count
        ),
    )
    final_quality = evaluate_student_assignment_quality(
        data,
        assignments=result.assignments,
        commitment_assignments=result.commitment_assignments,
        solver_objective_components=result.objective_components,
    )
    return build_operator_characterization_record(
        data=data,
        initial_quality=initial_quality,
        final_quality=final_quality,
        result=result,
        benchmark_name=benchmark_name,
        operator=operator,
        input_fingerprint=(
            input_fingerprint
            or semantic_student_assignment_input_fingerprint(data)
        ),
        source_seed_fingerprint=source_seed_fingerprint,
        ranking_policy=ranking_policy,
        selected_student_ids=selected_student_ids,
        selected_grade=selected_grade,
        external_elapsed_seconds=monotonic() - started,
        downstream=downstream,
    )


def aggregate_operator_characterization(records):
    """Aggregate matched trial rows into a bounded operator scorecard."""

    rows = tuple(records)
    if not rows:
        return {}
    operators = sorted({row.operator for row in rows})
    scorecard = {}
    for operator in operators:
        selected = tuple(row for row in rows if row.operator == operator)
        successful = tuple(
            row for row in selected
            if row.candidate_found and row.candidate_validated and row.candidate_adopted
        )
        times = tuple(
            _number(row.total_operation_seconds)
            for row in selected
            if row.total_operation_seconds is not None
        )
        first_times = tuple(
            _number(row.first_improvement_seconds)
            for row in selected
            if row.first_improvement_seconds is not None
        )
        gains = tuple(_number(row.total_gain) for row in selected)
        role_gains = tuple(_number(row.role_specific_gain) for row in selected)
        scorecard[operator] = {
            "role": selected[0].role,
            "trial_count": len(selected),
            "validated_adoption_count": len(successful),
            "success_rate": len(successful) / len(selected),
            "unknown_rate": sum(row.solver_status == "unknown" for row in selected) / len(selected),
            "median_total_gain": median(gains) if gains else 0,
            "best_total_gain": max(gains, default=0),
            "median_role_specific_gain": median(role_gains) if role_gains else 0,
            "best_role_specific_gain": max(role_gains, default=0),
            "median_total_operation_seconds": median(times) if times else None,
            "median_first_improvement_seconds": median(first_times) if first_times else None,
            "total_gain_per_minute": (
                sum(gains) / (sum(times) / 60.0) if sum(times) else 0.0
            ),
            "role_gain_per_minute": (
                sum(role_gains) / (sum(times) / 60.0) if sum(times) else 0.0
            ),
            "stagnation": summarize_stagnation(
                [attempt for row in selected for attempt in row.attempts]
            ),
            "attempts_per_time_window": estimate_attempts_per_time_window(
                [row.to_dict() for row in selected]
            ),
        }
    return scorecard


def build_capability_card(operator, scorecard):
    """Create a cautious human-readable summary from aggregate evidence."""

    facts = dict(scorecard.get(operator, {}))
    return {
        "operator": operator,
        "role": facts.get("role", OPERATOR_ROLES.get(operator, "unknown")),
        "trial_count": facts.get("trial_count", 0),
        "intended_use": facts.get("role", "unknown"),
        "success_rate": facts.get("success_rate"),
        "median_total_gain": facts.get("median_total_gain"),
        "median_role_specific_gain": facts.get("median_role_specific_gain"),
        "median_total_operation_seconds": facts.get("median_total_operation_seconds"),
        "median_first_improvement_seconds": facts.get("median_first_improvement_seconds"),
        "total_gain_per_minute": facts.get("total_gain_per_minute"),
        "role_gain_per_minute": facts.get("role_gain_per_minute"),
        "unknown_rate": facts.get("unknown_rate"),
        "stagnation": facts.get("stagnation", {}),
        "evidence_limit": "descriptive trial evidence; not a universal guarantee",
    }


def build_adaptive_readiness_matrix(scorecard):
    """Return an evidence-only matrix grouped by intended operator role."""

    matrix = {}
    for operator, facts in sorted(scorecard.items()):
        role = facts.get("role", OPERATOR_ROLES.get(operator, "unknown"))
        matrix.setdefault(role, []).append({
            "operator": operator,
            "success_rate": facts.get("success_rate"),
            "role_gain_per_minute": facts.get("role_gain_per_minute"),
            "median_first_improvement_seconds": facts.get("median_first_improvement_seconds"),
            "unknown_rate": facts.get("unknown_rate"),
            "stagnation": facts.get("stagnation", {}).get("classification"),
        })
    return matrix


__all__ = [
    "CHARACTERIZATION_SCHEMA",
    "OPERATOR_ROLES",
    "OperatorCharacterizationRecord",
    "aggregate_operator_characterization",
    "build_adaptive_readiness_matrix",
    "build_capability_card",
    "build_operator_characterization_record",
    "run_operator_characterization_trial",
    "estimate_attempts_per_time_window",
    "summarize_stagnation",
]
