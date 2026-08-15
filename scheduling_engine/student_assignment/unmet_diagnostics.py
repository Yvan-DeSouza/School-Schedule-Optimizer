"""Stable unmet-request diagnosis from an already-solved assignment state."""

from __future__ import annotations

from .occupancy import occupied_half_segments
from ..diagnostics import (
    STUDENT_ASSIGNMENT_HARD_PREREQUISITE_SEQUENCE_UNAVAILABLE,
    STUDENT_ASSIGNMENT_LOCKED_ENROLLMENT_BLOCKS_REQUEST,
    STUDENT_ASSIGNMENT_NO_ACTIVE_PLACED_SECTION,
    STUDENT_ASSIGNMENT_NO_ELIGIBLE_SECTION,
    STUDENT_ASSIGNMENT_REQUIRES_ADDITIONAL_CAPACITY,
    STUDENT_ASSIGNMENT_REQUIRES_LOCK_RELEASE,
    STUDENT_ASSIGNMENT_REQUIRES_PLACED_SECTION,
    STUDENT_ASSIGNMENT_REQUIRES_PREREQUISITE_SEQUENCE_CHANGE,
    STUDENT_ASSIGNMENT_REQUIRES_TIMESLOT_CHANGE,
    STUDENT_ASSIGNMENT_SECTION_CAPACITY_EXHAUSTED,
    STUDENT_ASSIGNMENT_TIMESLOT_COLLISION,
)


def diagnostic_for_unmet_request(
    *, request, offering_sections, candidates, fixed_slots, fixed_slot_rows,
    request_lock_blockers, direct_protected_requests, hard_sequence_impossible,
    selected_by_section, fixed_by_section, sections,
):
    """Return a stable reason plus the most specific available blocking IDs."""

    potential_sections = tuple(offering_sections.get(request.course_offering_id, ()))
    lock_ids = sorted(request_lock_blockers.get(request.request_id, ()))
    has_direct_protection = request.request_id in direct_protected_requests
    direct_protection = direct_protected_requests.get(request.request_id)
    if not potential_sections:
        return (
            STUDENT_ASSIGNMENT_NO_ACTIVE_PLACED_SECTION,
            None,
            None,
            None,
            (STUDENT_ASSIGNMENT_REQUIRES_PLACED_SECTION,),
        )
    if has_direct_protection or (lock_ids and not candidates):
        return (
            STUDENT_ASSIGNMENT_LOCKED_ENROLLMENT_BLOCKS_REQUEST,
            direct_protection or lock_ids[0],
            None,
            None,
            (STUDENT_ASSIGNMENT_REQUIRES_LOCK_RELEASE,),
        )
    if any(
        student_id == request.student_id
        and request.course_id in {prerequisite_id, course_id}
        for student_id, prerequisite_id, course_id in hard_sequence_impossible
    ):
        return (
            STUDENT_ASSIGNMENT_HARD_PREREQUISITE_SEQUENCE_UNAVAILABLE,
            None,
            None,
            None,
            (STUDENT_ASSIGNMENT_REQUIRES_PREREQUISITE_SEQUENCE_CHANGE,),
        )
    collided_rows = [
        row
        for section in potential_sections
        for segment in occupied_half_segments(section.half_semester_segment)
        if (section.timeslot_id, segment) in fixed_slots[request.student_id]
        for row in fixed_slot_rows[request.student_id, section.timeslot_id, segment]
    ]
    if collided_rows and not candidates:
        lock_id = next((lock_id for row in collided_rows for lock_id in row.lock_ids), None)
        return (
            STUDENT_ASSIGNMENT_TIMESLOT_COLLISION,
            lock_id,
            collided_rows[0].section_id,
            collided_rows[0].student_id,
            (STUDENT_ASSIGNMENT_REQUIRES_TIMESLOT_CHANGE,),
        )
    if candidates:
        full_sections = []
        for section, _variable in candidates:
            assigned = selected_by_section.get(section.section_id, ())
            occupied = len(fixed_by_section[section.section_id]) + len(assigned)
            if occupied >= sections[section.section_id].capacity_max:
                blocking_student_id = assigned[0].student_id if assigned else (
                    fixed_by_section[section.section_id][0].student_id
                    if fixed_by_section[section.section_id] else None
                )
                full_sections.append((section.section_id, blocking_student_id))
        if full_sections:
            section_id, student_id = sorted(full_sections)[0]
            return (
                STUDENT_ASSIGNMENT_SECTION_CAPACITY_EXHAUSTED,
                None,
                section_id,
                student_id,
                (STUDENT_ASSIGNMENT_REQUIRES_ADDITIONAL_CAPACITY,),
            )
    return STUDENT_ASSIGNMENT_NO_ELIGIBLE_SECTION, None, None, None, ()
