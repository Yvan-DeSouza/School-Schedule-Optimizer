"""Detached-input validation and scope helpers for student assignment."""

from __future__ import annotations

from collections import defaultdict

from ..constants import (
    IMPORTANCE_LEVELS,
    LOCK_TYPES,
    LOCK_TYPE_STUDENT_GROUP,
    SCHEDULE_PRESERVATION_LEVELS,
)


def is_active_enrollment(enrollment):
    """Historical DTO rows are audit-only even if an older caller leaves is_active true."""

    return enrollment.is_active and not enrollment.is_historical


def scope_includes_enrollment(data, enrollment):
    """Apply the immutable resolved scope before the model sees an enrollment.

    A scoped run may identify a row directly in ``is_in_scope`` after the
    adapter resolves its three queryable scope dimensions. The explicit IDs
    remain available so a detached snapshot is sufficient to reproduce that
    decision without an ORM query.
    """

    if data.scope.scope_type == "full":
        return True
    return (
        enrollment.is_in_scope
        or enrollment.student_id in data.scope.student_ids
        or enrollment.course_id in data.scope.course_ids
        or enrollment.section_id in data.scope.section_ids
    )


def request_matches_enrollment(request, enrollment):
    return (
        request.student_id == enrollment.student_id
        and request.course_id == enrollment.course_id
        and (
            request.current_enrollment_id is None
            or request.current_enrollment_id == enrollment.enrollment_id
        )
    )


def validate_input(data):
    """Reject malformed detached facts before building a CP-SAT model."""

    section_ids = set()
    offering_sections = defaultdict(list)
    for section in data.sections:
        if section.section_id in section_ids:
            raise ValueError(f"Duplicate student-assignment section {section.section_id}.")
        if section.semester not in {1, 2} or section.timeslot_id <= 0 or section.capacity_max < 0:
            raise ValueError(f"Section {section.section_id} lacks accepted assignment context.")
        section_ids.add(section.section_id)
        for offering_id in section.member_course_offering_ids:
            offering_sections[offering_id].append(section)

    request_ids = set()
    for request in data.requests:
        if request.request_id in request_ids:
            raise ValueError(f"Duplicate effective course request {request.request_id}.")
        request_ids.add(request.request_id)

    for importance in (
        data.section_utilization_balance_importance,
        data.student_semester_balance_importance,
        data.course_sequence_preferences_importance,
        data.difficulty_balance_importance,
        data.course_category_diversity_importance,
    ):
        if importance not in IMPORTANCE_LEVELS:
            raise ValueError("Student-assignment importance values are invalid.")
    difficulty_course_ids = set()
    for difficulty in data.course_difficulties:
        if difficulty.course_id in difficulty_course_ids:
            raise ValueError(f"Duplicate course difficulty profile {difficulty.course_id}.")
        if not 0 <= difficulty.calculated_difficulty <= 100:
            raise ValueError("Calculated course difficulty must be between 0 and 100.")
        if (
            difficulty.manual_difficulty_override is not None
            and not 0 <= difficulty.manual_difficulty_override <= 100
        ):
            raise ValueError("Manual course difficulty override must be between 0 and 100.")
        if not 0 <= difficulty.effective_difficulty <= 100:
            raise ValueError("Effective course difficulty must be between 0 and 100.")
        difficulty_course_ids.add(difficulty.course_id)
    category_relationships = set()
    for relationship in data.course_category_relationships:
        pair = tuple(sorted((relationship.category_a, relationship.category_b)))
        if relationship.category_a == relationship.category_b or pair in category_relationships:
            raise ValueError("Course category relationships must be unique distinct pairs.")
        if not 0 <= relationship.similarity_score <= 100:
            raise ValueError("Course category relationship similarity must be between 0 and 100.")
        category_relationships.add(pair)
    if data.schedule_preservation_level not in SCHEDULE_PRESERVATION_LEVELS:
        raise ValueError("Student-assignment schedule preservation level is invalid.")
    if data.scope.scope_type not in {"full", "scoped"}:
        raise ValueError("Student-assignment scope_type must be full or scoped.")
    if data.scope.scope_type == "scoped" and not any(
        (data.scope.student_ids, data.scope.course_ids, data.scope.section_ids)
    ):
        raise ValueError("A scoped student-assignment input requires at least one resolved scope ID.")
    if len(set(data.priority_request_ids)) != len(data.priority_request_ids):
        raise ValueError("Priority request IDs must be unique.")
    priority_ids = set(data.priority_request_ids)
    unknown_priority_ids = priority_ids - request_ids
    if unknown_priority_ids:
        raise ValueError("Priority request IDs must identify requests in this input.")
    if data.priority_request_limit is not None:
        if data.priority_request_limit < 0:
            raise ValueError("Priority request limit cannot be negative.")
        if len(priority_ids) > data.priority_request_limit:
            raise ValueError("Priority request IDs exceed the resolved run limit.")
    if any(not request.is_primary for request in data.requests if request.request_id in priority_ids):
        raise ValueError("Only primary requests may receive student-assignment priority.")

    lock_ids = set()
    for lock in data.student_assignment_locks:
        if lock.lock_id in lock_ids:
            raise ValueError(f"Duplicate student-assignment lock {lock.lock_id}.")
        lock_ids.add(lock.lock_id)
        if lock.lock_type not in LOCK_TYPES:
            raise ValueError(f"Unrecognized student-assignment lock type {lock.lock_type!r}.")
        if lock.lock_type == LOCK_TYPE_STUDENT_GROUP and lock.is_active:
            if len(set(lock.member_student_ids)) < 2:
                raise ValueError("An active student-group lock requires at least two distinct members.")
            if lock.course_id is None:
                raise ValueError("An active student-group lock requires a course target.")
    return offering_sections
