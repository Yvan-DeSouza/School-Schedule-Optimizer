"""Structured, solver-neutral records for student-search experiments.

This module defines the evidence boundary for diagnostic operator studies. It
does not select assignments or alter CP-SAT models. Ranking policies are
explicitly experimental controls; the solver and unchanged full-model
validator remain authoritative for every candidate.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from hashlib import sha256
import json

from .runtime import semantic_student_assignment_input_fingerprint
from .search_guidance import (
    StudentTargetPressure,
    rank_students_by_quality_pressure,
)


RANKING_POLICY_WEIGHTED = "v2_counselor_weighted_pressure"
RANKING_POLICY_RAW = "raw_local_penalty_control"
RANKING_POLICY_DETERMINISTIC = "deterministic_semantic_control"


def _ranked_with_rank(records):
    return tuple(
        replace(record, rank=index)
        for index, record in enumerate(records, start=1)
    )


def rank_students_by_raw_local_penalty(data, quality_report, *, limit=None):
    """Return a diagnostic control ranking using unweighted raw local facts.

    This deliberately does not change the v2 solver objective. It answers
    whether counselor normalization and importance scores add useful search
    targeting beyond a comparable raw-penalty heuristic.
    """

    records = rank_students_by_quality_pressure(data, quality_report)
    ordered = sorted(
        records,
        key=lambda record: (
            -sum(value for _name, value in record.component_penalties),
            -record.opportunity_signal,
            -record.nonzero_component_count,
            record.student_id,
        ),
    )
    if limit is not None:
        ordered = ordered[: max(0, int(limit))]
    return _ranked_with_rank(ordered)


def select_deterministic_control_students(data, count, *, seed=0):
    """Select a reproducible semantic-order control sample.

    Hash ordering avoids database-order dependence while keeping the control
    independent of quality pressure. The seed is part of the experiment
    record and must be held constant for matched trials.
    """

    count = max(0, int(count))
    student_ids = sorted(
        {
            request.student_id
            for request in data.requests
        }
        | {row.student_id for row in data.fixed_enrollments}
        | {row.student_id for row in data.schedule_commitment_requests}
        | {row.student_id for row in data.fixed_schedule_commitments}
    )
    ordered = sorted(
        student_ids,
        key=lambda student_id: (
            sha256(f"{seed}:{student_id}".encode()).hexdigest(),
            student_id,
        ),
    )
    return tuple(ordered[:count])


def select_interacting_second_student(data, first_student_id, ranked_students):
    """Choose a cheap deterministic S2 partner from existing static facts.

    Interaction is estimated from shared requested courses and sections that
    can serve both course sets. This is a search-selection hint only; it does
    not claim that a pair must move or that the resulting candidate is valid.
    """

    first_courses = {
        request.course_id
        for request in data.requests
        if request.student_id == first_student_id
    }
    section_course_sets = tuple(
        (section.section_id, set(section.member_course_ids))
        for section in data.sections
    )
    scored = []
    for record in ranked_students:
        if record.student_id == first_student_id:
            continue
        other_courses = {
            request.course_id
            for request in data.requests
            if request.student_id == record.student_id
        }
        shared_courses = len(first_courses & other_courses)
        shared_sections = sum(
            bool(first_courses & course_ids)
            and bool(other_courses & course_ids)
            for _section_id, course_ids in section_course_sets
        )
        scored.append(
            (
                -(shared_courses * 2 + shared_sections),
                -record.weighted_current_penalty,
                record.student_id,
                record,
            )
        )
    scored.sort(key=lambda item: item[:3])
    return scored[0][3] if scored else None


def source_decision_fingerprint(source_decisions):
    """Return the stable diagnostic hash for semantic source decisions."""

    canonical = tuple(sorted(tuple(source_decisions or ()), key=repr))
    return sha256(repr(canonical).encode()).hexdigest()


def _json_safe(value):
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_safe(item) for item in value]
    if isinstance(value, set):
        return [_json_safe(item) for item in sorted(value, key=repr)]
    return value


@dataclass(frozen=True)
class StudentSearchExperimentRecord:
    """Compact JSON-safe evidence for one matched search-operator trial."""

    experiment_id: str
    input_fingerprint: str
    source_seed_fingerprint: str | None
    objective_semantics_version: str
    counselor_profile: dict
    ranking_policy: str
    selected_student_ids: tuple
    operator: str
    neighborhood_radius: int | None
    max_changed_students: int | None
    targeted: bool
    solver_status: str | None
    candidate_found: bool
    candidate_validated: bool
    candidate_adopted: bool
    starting_objective_vector: tuple
    final_objective_vector: tuple
    starting_components: dict
    final_components: dict
    normalized_components: dict
    weighted_contributions: dict
    changed_source_decision_count: int
    changed_student_ids: tuple
    affected_section_ids: tuple
    cp_sat_wall_time_seconds: float | None
    external_solve_seconds: float | None
    model_preparation_seconds: float | None
    full_validation_seconds: float | None
    total_operation_seconds: float | None
    model_variable_count: int | None
    model_constraint_count: int | None
    branches: int | None
    conflicts: int | None
    deterministic_time_seconds: float | None
    resource: dict
    stopping_reason: str | None
    control_seed: int | None = None
    source_decision_fingerprint: str | None = None

    def to_dict(self):
        return _json_safe(asdict(self))

    def to_json(self):
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


def build_search_experiment_record(
    *,
    data,
    result,
    experiment_id,
    operator,
    ranking_policy=RANKING_POLICY_WEIGHTED,
    selected_student_ids=(),
    targeted=False,
    source_seed_decisions=(),
    external_elapsed_seconds=None,
    model_preparation_seconds=None,
    control_seed=None,
):
    """Build a record from an existing diagnostic result without re-solving."""

    facts = dict(result.optimization_facts or {})
    local = dict(facts.get("stage_2_local_bootstrap") or {})
    stage_2 = dict(facts.get("stage_2") or {})
    stage_1 = dict(facts.get("stage_1") or {})
    components = dict(result.objective_components or {})
    return StudentSearchExperimentRecord(
        experiment_id=str(experiment_id),
        input_fingerprint=semantic_student_assignment_input_fingerprint(data),
        source_seed_fingerprint=(
            source_decision_fingerprint(source_seed_decisions)
            if source_seed_decisions
            else None
        ),
        objective_semantics_version=data.objective_semantics_version,
        counselor_profile={
            "section_utilization_balance": data.objective_importance_scores.get(
                "section_utilization_balance"
            ),
            "student_semester_balance": data.objective_importance_scores.get(
                "student_semester_balance"
            ),
            "course_sequence_preferences": data.objective_importance_scores.get(
                "course_sequence_preferences"
            ),
            "difficulty_balance": data.objective_importance_scores.get(
                "difficulty_balance"
            ),
            "course_category_diversity": data.objective_importance_scores.get(
                "course_category_diversity"
            ),
        },
        ranking_policy=ranking_policy,
        selected_student_ids=tuple(selected_student_ids),
        operator=operator,
        neighborhood_radius=local.get("neighborhood_radius"),
        max_changed_students=local.get("max_changed_students"),
        targeted=bool(targeted),
        solver_status=result.solver_outcome,
        candidate_found=bool(local.get("candidate_found", result.status == "complete")),
        candidate_validated=bool(local.get("candidate_validated", False)),
        candidate_adopted=bool(local.get("improvement_adopted", False)),
        starting_objective_vector=tuple(stage_1.get("objective_vector", ())),
        final_objective_vector=tuple(stage_2.get("objective_values", ())),
        starting_components=dict(stage_1.get("substantive_components", {})),
        final_components={
            key: value
            for key, value in components.items()
            if key.endswith("_penalty")
        },
        normalized_components=dict(components.get("normalized_components", {})),
        weighted_contributions=dict(
            components.get("weighted_normalized_contributions", {})
        ),
        changed_source_decision_count=int(
            local.get("changed_source_decision_count", 0) or 0
        ),
        changed_student_ids=tuple(local.get("affected_student_ids", ())),
        affected_section_ids=tuple(local.get("affected_section_ids", ())),
        cp_sat_wall_time_seconds=local.get("solver_wall_time_seconds"),
        external_solve_seconds=local.get("probe_timings", {}).get(
            "operation_total_seconds"
        ),
        model_preparation_seconds=model_preparation_seconds,
        full_validation_seconds=local.get("validation_elapsed_seconds"),
        total_operation_seconds=(
            external_elapsed_seconds
            if external_elapsed_seconds is not None
            else local.get("deadline_elapsed_seconds")
        ),
        model_variable_count=(
            local.get("model_variable_count")
            or facts.get("full_model_variable_count")
        ),
        model_constraint_count=(
            local.get("model_constraint_count")
            or facts.get("full_model_constraint_count")
        ),
        branches=local.get("branches"),
        conflicts=local.get("conflicts"),
        deterministic_time_seconds=local.get("deterministic_time_seconds"),
        resource=dict(local.get("memory") or facts.get("operation_resource_monitor") or {}),
        stopping_reason=local.get("stopping_reason") or stage_2.get("stopping_reason"),
        control_seed=control_seed,
        source_decision_fingerprint=stage_2.get("source_decision_fingerprint"),
    )


__all__ = [
    "RANKING_POLICY_WEIGHTED",
    "RANKING_POLICY_RAW",
    "RANKING_POLICY_DETERMINISTIC",
    "StudentSearchExperimentRecord",
    "build_search_experiment_record",
    "rank_students_by_raw_local_penalty",
    "select_deterministic_control_students",
    "select_interacting_second_student",
    "source_decision_fingerprint",
]
