"""Student-time occupancy facts shared by the pure assignment helpers."""

from __future__ import annotations

from collections import defaultdict

from ..constants import HALF_SEMESTER_SEGMENTS
from ..diagnostics import STUDENT_ASSIGNMENT_UNALLOCATED_SCHOOL_TIME
from ..dto import StudentAssignmentReviewItemDTO


def occupied_half_segments(half_semester_segment):
    """Return the physical halves occupied by one recurring schedule choice.

    Full-semester teaching and online supervision reserve both halves. A
    trimestre section reserves only its declared half, which is why two paired
    half courses can lawfully share one A-D block without becoming a collision.
    """

    return (
        (half_semester_segment,)
        if half_semester_segment in HALF_SEMESTER_SEGMENTS
        else HALF_SEMESTER_SEGMENTS
    )


def request_occupied_half_segments(request, section):
    """Return the student-time halves occupied by one proposed assignment.

    Online supervision is a full-semester physical placement, but a
    half-semester online *course* is academic only in its catalog-paired half.
    The solver therefore reserves the whole supervision block for the student
    while the request's segment is retained separately for academic balance
    and review. Normal course occupancy always comes from its instructional
    section, whose paired first/second-half identity is operational fact.
    """

    if request.delivery_kind == "online":
        return HALF_SEMESTER_SEGMENTS
    return occupied_half_segments(section.half_semester_segment)


def fixed_enrollment_occupied_half_segments(enrollment):
    """Preserve full-term supervision occupancy for an active online history row."""

    if enrollment.delivery_kind == "online":
        return HALF_SEMESTER_SEGMENTS
    return occupied_half_segments(enrollment.half_semester_segment)


def append_unallocated_school_time_review_items(
    *, data, sections, requests_by_id, fixed_slots, fixed_rows,
    assignments, commitment_assignments, unpaired_half_occupancies, review_items,
):
    """Report factual unallocated student time without creating an implicit Study.

    The solver's assignment choices are already complete when this runs. The
    calculation therefore reports only observable occupancy from accepted fixed
    context, the candidate assignments, and explicit special commitments. It
    is a counselor review aid, not an additional optimization objective.
    """

    timeslots = [slot for slot in data.timeslots if slot.is_available]
    if not timeslots:
        return
    occupied_by_student = defaultdict(set)
    for student_id, occupancy in fixed_slots.items():
        occupied_by_student[student_id].update(occupancy)
    for assignment in assignments:
        request = requests_by_id[assignment.request_id]
        section_id = (
            assignment.section_id
            if assignment.section_id is not None
            else -assignment.online_supervision_session_id
        )
        section = sections[section_id]
        occupied_by_student[assignment.student_id].update(
            (assignment.timeslot_id, segment)
            for segment in request_occupied_half_segments(request, section)
        )
    for commitment in commitment_assignments:
        occupied_by_student[commitment.student_id].update(commitment.occupancy)

    student_ids = {
        request.student_id for request in data.requests
    } | {
        row.student_id for row in fixed_rows
    } | {
        request.student_id for request in data.schedule_commitment_requests
    } | {
        commitment.student_id
        for commitment in data.fixed_schedule_commitments
        if commitment.is_active and not commitment.is_historical
    }
    students_with_study_requests = {
        request.student_id
        for request in data.schedule_commitment_requests
        if request.commitment_type == "study"
    }
    students_with_alternates = set(data.student_ids_with_alternate_requests)

    for student_id in sorted(student_ids):
        # An alternate is already an explicit counselor-recorded response to
        # unmet demand. It is not an automatic Study, but it prevents this
        # separate empty-time review from duplicating the request review.
        if student_id in students_with_alternates:
            continue
        occupied = occupied_by_student[student_id]
        for timeslot in timeslots:
            unallocated_segments = tuple(
                segment
                for segment in HALF_SEMESTER_SEGMENTS
                if (timeslot.id, segment) not in occupied
                and (student_id, timeslot.id, segment) not in unpaired_half_occupancies
            )
            if not unallocated_segments:
                continue
            review_items.append(StudentAssignmentReviewItemDTO(
                code=STUDENT_ASSIGNMENT_UNALLOCATED_SCHOOL_TIME,
                student_id=student_id,
                detail={
                    "semester": timeslot.semester,
                    "block": timeslot.block,
                    "timeslot_id": timeslot.id,
                    "unallocated_half_segments": unallocated_segments,
                    "has_requested_study": student_id in students_with_study_requests,
                    "has_alternate_request": False,
                    "recognized_commitment": False,
                    "reason": "no_requested_study_or_recognized_commitment",
                },
            ))
