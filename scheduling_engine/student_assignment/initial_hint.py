"""Deterministic, CP-SAT-validated starting guidance for student assignment."""

from __future__ import annotations

from collections import Counter, defaultdict

from .occupancy import occupied_half_segments


def build_initial_assignment_hints(
    *, data, request_candidates, fixed_by_section, fixed_slots, group_locks,
):
    """Construct deterministic enrollment guidance for the first CP-SAT pass.

    This is intentionally conservative. It only proposes ordinary independent
    requests and declines to guide inputs with group or same-year prerequisite
    relationships, whose coupled decisions deserve the full model's search.
    The later preparatory CP-SAT solve validates every proposed value, so a
    construction mistake can never weaken or bypass a hard constraint.
    """

    if group_locks or data.hard_prerequisites:
        return {}

    requests_by_student = defaultdict(list)
    for request in data.requests:
        if request_candidates[request.request_id]:
            requests_by_student[request.student_id].append(request)

    remaining_capacity = {
        section.section_id: section.capacity_max - len(fixed_by_section[section.section_id])
        for section in data.sections
    }
    request_count_by_offering = Counter(
        request.course_offering_id
        for request in data.requests
        if request_candidates[request.request_id]
    )
    capacity_by_offering = {}
    for request in data.requests:
        if request.course_offering_id in capacity_by_offering:
            continue
        capacity_by_offering[request.course_offering_id] = sum(
            remaining_capacity[section.section_id]
            for section, _variable in request_candidates[request.request_id]
        )
    slack_by_offering = {
        offering_id: capacity_by_offering[offering_id] - request_count
        for offering_id, request_count in request_count_by_offering.items()
    }
    assigned_section_by_request = {}

    def assign_student(requests, used_timeslots):
        """Backtrack within one student's small request set, not across students."""

        if not requests:
            return True
        request = requests[0]
        candidates = sorted(
            request_candidates[request.request_id],
            key=lambda item: (
                # Filling the least-used compatible physical section first
                # creates a balanced, capacity-safe seed for CP-SAT to improve.
                -remaining_capacity[item[0].section_id],
                item[0].section_id,
            ),
        )
        for section, _variable in candidates:
            if (
                remaining_capacity[section.section_id] <= 0
                or any(
                    (section.timeslot_id, segment) in used_timeslots
                    for segment in occupied_half_segments(section.half_semester_segment)
                )
            ):
                continue
            remaining_capacity[section.section_id] -= 1
            assigned_section_by_request[request.request_id] = section.section_id
            if assign_student(
                requests[1:],
                used_timeslots | {
                    (section.timeslot_id, segment)
                    for segment in occupied_half_segments(section.half_semester_segment)
                },
            ):
                return True
            assigned_section_by_request.pop(request.request_id)
            remaining_capacity[section.section_id] += 1
        return False

    for student_id in sorted(requests_by_student):
        student_requests = sorted(
            requests_by_student[student_id],
            key=lambda request: (
                # The seed should protect a one-seat low-demand offering
                # before a high-demand course with many spare seats. This is
                # only search guidance; CP-SAT remains responsible for every
                # fulfillment tier and hard scheduling rule.
                slack_by_offering[request.course_offering_id],
                not request.is_mandatory,
                not request.is_primary,
                len(request_candidates[request.request_id]),
                request.request_id,
            ),
        )
        before_student = set(assigned_section_by_request)
        if assign_student(student_requests, set(fixed_slots[student_id])):
            continue
        # Do not leave a partial individual schedule in a failed hint. The
        # model still receives the request unchanged and can solve it normally.
        for request_id in set(assigned_section_by_request) - before_student:
            section_id = assigned_section_by_request.pop(request_id)
            remaining_capacity[section_id] += 1

    return {
        (request_id, section_id): True
        for request_id, section_id in assigned_section_by_request.items()
    }
