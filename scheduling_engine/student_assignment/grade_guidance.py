"""Solver-neutral facts for diagnostic grade-bounded escape searches.

The records in this module explain the scope of a grade experiment.  They do
not choose assignments or claim that a grade can improve: CP-SAT and the
unchanged full-model validator remain the only authorities.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..constants import VALID_STUDENT_GRADE_LEVELS


@dataclass(frozen=True)
class GradeOpportunity:
    """Bounded opportunity facts for one actual student grade."""

    grade_level: int
    student_ids: tuple[int, ...]
    student_count: int
    movable_source_decision_count: int
    movable_student_count: int
    pressured_student_count: int
    local_pressure_total: int
    utilization_pressure_share: float
    pressured_delivery_group_ids: tuple[int, ...]
    ordinary_lock_count: int
    special_lock_count: int
    effective_search_available: bool


def _source_map(source_decisions):
    if isinstance(source_decisions, dict):
        return dict(source_decisions)
    return dict(tuple(source_decisions or ()))


def _grade_map(data):
    values = {}
    for student_id, grade_level in data.student_grades:
        student_id = int(student_id)
        grade_level = int(grade_level)
        if student_id in values and values[student_id] != grade_level:
            raise ValueError(f"Student {student_id} has conflicting grade facts")
        values[student_id] = grade_level
    return values


def _student_pressure(quality_report, student_ids):
    total = 0
    pressured_students = set()
    for metric_name in (
        "student_semester_load_balance",
        "difficulty_balance",
        "course_category_diversity",
    ):
        entities = quality_report.get(metric_name, {}).get("entities", {})
        for student_id in student_ids:
            value = entities.get(str(student_id), 0)
            if isinstance(value, dict):
                value = value.get(
                    "absolute_difference",
                    value.get("penalty", value.get("value", 0)),
                )
            try:
                numeric = int(value or 0)
            except (TypeError, ValueError):
                numeric = 0
            total += numeric
            if numeric > 0:
                pressured_students.add(student_id)
    return total, len(pressured_students)


def build_grade_opportunity_facts(data, source_decisions=(), quality_report=None):
    """Return deterministic grade scope facts from existing detached facts."""

    grade_by_student = _grade_map(data)
    source_map = _source_map(source_decisions)
    quality_report = quality_report or {}
    source_rows = tuple(
        (key, value)
        for key, value in source_map.items()
        if isinstance(value, tuple) and value and value[0] in grade_by_student
    )
    request_by_id = {request.request_id: request for request in data.requests}
    commitment_by_id = {
        request.request_id: request
        for request in data.schedule_commitment_requests
    }
    ordinary_locks_by_grade = {grade: 0 for grade in VALID_STUDENT_GRADE_LEVELS}
    special_locks_by_grade = {grade: 0 for grade in VALID_STUDENT_GRADE_LEVELS}
    for lock in data.student_assignment_locks:
        student_ids = set(lock.member_student_ids)
        if lock.student_id is not None:
            student_ids.add(lock.student_id)
        for grade in VALID_STUDENT_GRADE_LEVELS:
            if any(grade_by_student.get(student_id) == grade for student_id in student_ids):
                ordinary_locks_by_grade[grade] += 1
    for lock in data.special_commitment_locks:
        request = (
            request_by_id.get(lock.course_request_id)
            or commitment_by_id.get(lock.schedule_commitment_request_id)
        )
        if request is None:
            continue
        grade = grade_by_student.get(request.student_id)
        if grade in special_locks_by_grade:
            special_locks_by_grade[grade] += 1

    utilization_entities = quality_report.get(
        "section_utilization_balance", {}
    ).get("entities", {})
    group_students = {}
    for key, value in source_rows:
        if not isinstance(key, tuple) or not key or key[0] != "course":
            continue
        if len(value) < 2 or value[1] is None:
            continue
        request = request_by_id.get(key[1])
        if request is None:
            continue
        group_id = next(
            (
                section.delivery_group_id
                for section in data.sections
                if section.section_id == value[1]
            ),
            None,
        )
        if group_id is not None:
            group_students.setdefault(group_id, set()).add(value[0])
    group_pressure = {
        int(group_id): int(facts.get("pairwise_absolute_difference", 0) or 0)
        for group_id, facts in utilization_entities.items()
        if isinstance(facts, dict)
    }
    total_pressure = sum(max(0, value) for value in group_pressure.values())

    opportunities = []
    for grade in VALID_STUDENT_GRADE_LEVELS:
        student_ids = tuple(sorted(
            student_id for student_id, value in grade_by_student.items()
            if value == grade
        ))
        grade_source_rows = tuple(
            (key, value) for key, value in source_rows
            if value[0] in student_ids
        )
        pressured_groups = tuple(sorted(
            group_id for group_id, students in group_students.items()
            if students.intersection(student_ids) and group_pressure.get(group_id, 0) > 0
        ))
        grade_utilization_pressure = sum(
            group_pressure.get(group_id, 0) for group_id in pressured_groups
        )
        local_pressure_total, pressured_student_count = _student_pressure(
            quality_report, student_ids
        )
        opportunities.append(GradeOpportunity(
            grade_level=grade,
            student_ids=student_ids,
            student_count=len(student_ids),
            movable_source_decision_count=len(grade_source_rows),
            movable_student_count=len({value[0] for _key, value in grade_source_rows}),
            pressured_student_count=pressured_student_count,
            local_pressure_total=local_pressure_total,
            utilization_pressure_share=(
                grade_utilization_pressure / total_pressure
                if total_pressure else 0.0
            ),
            pressured_delivery_group_ids=pressured_groups,
            ordinary_lock_count=ordinary_locks_by_grade[grade],
            special_lock_count=special_locks_by_grade[grade],
            effective_search_available=bool(grade_source_rows),
        ))
    return tuple(opportunities)


__all__ = ["GradeOpportunity", "build_grade_opportunity_facts"]
