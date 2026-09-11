"""Provisional, solver-free semester-credit research support.

This module is not an Objective Semantics version and is not imported by the
production solver, API adapter, or quality payload.  Its lower-bound helper is
retained only for a future post-fixture static audit.
"""

from __future__ import annotations

from collections import defaultdict

from .objective_semantics import minimum_indivisible_credit_imbalance


def focus_student_ids(data):
    """Return the students deliberately excluded from term-balance metrics."""

    return {
        request.student_id
        for request in data.schedule_commitment_requests
        if request.commitment_type == "focus"
    } | {
        commitment.student_id
        for commitment in data.fixed_schedule_commitments
        if commitment.is_active
        and not commitment.is_historical
        and commitment.commitment_kind == "focus"
    }


def credit_quantum_floor_by_student(data):
    """Return a provisional input-derived indivisible-credit lower bound.

    It does not establish a capacity-feasible whole-school outcome and must
    not be treated as an objective, score, or decision threshold.
    """

    excluded = focus_student_ids(data)
    student_ids = {
        request.student_id for request in data.requests
    } | {
        row.student_id for row in data.fixed_enrollments
        if row.is_active and not row.is_historical
    } | {
        commitment.student_id for commitment in data.fixed_schedule_commitments
        if commitment.is_active and not commitment.is_historical
    }
    fixed_loads = defaultdict(lambda: [0, 0])
    flexible = defaultdict(list)
    semester_by_timeslot = {slot.id: slot.semester for slot in data.timeslots}

    for row in data.fixed_enrollments:
        if not row.is_active or row.is_historical or row.student_id in excluded:
            continue
        fixed_loads[row.student_id][row.semester - 1] += round(row.credit_value * 2)

    fixed_co_op_request_ids = set()
    for commitment in data.fixed_schedule_commitments:
        if not commitment.is_active or commitment.is_historical:
            continue
        if commitment.commitment_kind != "co_op":
            continue
        if commitment.course_request_id is not None:
            fixed_co_op_request_ids.add(commitment.course_request_id)
        amount = round(commitment.credit_value * 2)
        for semester in {
            semester_by_timeslot.get(timeslot_id)
            for timeslot_id, _segment in commitment.occupancy
        }:
            if semester in (1, 2) and commitment.student_id not in excluded:
                fixed_loads[commitment.student_id][semester - 1] += amount

    paired = defaultdict(list)
    for request in data.requests:
        if request.student_id in excluded:
            continue
        if request.delivery_kind == "co_op":
            if request.request_id not in fixed_co_op_request_ids:
                # The v2 CP-SAT expression defines a movable Co-op as four
                # half-credit units, independent of the DTO's display value.
                flexible[request.student_id].append(4)
            continue
        if request.duration == "half_semester" and request.paired_half_course_id:
            key = (request.student_id, tuple(sorted((
                request.course_id, request.paired_half_course_id,
            ))))
            paired[key].append(request)
        else:
            flexible[request.student_id].append(round(request.credit_value * 2))

    for (student_id, _pair_key), requests in paired.items():
        if len(requests) == 2:
            flexible[student_id].append(sum(
                round(request.credit_value * 2) for request in requests
            ))
        else:
            flexible[student_id].extend(
                round(request.credit_value * 2) for request in requests
            )

    return {
        student_id: minimum_indivisible_credit_imbalance(
            fixed_loads[student_id][0],
            fixed_loads[student_id][1],
            flexible[student_id],
        )
        for student_id in sorted(student_ids - excluded)
    }
