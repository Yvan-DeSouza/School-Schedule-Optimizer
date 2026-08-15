"""Bounded, non-narrative candidate evidence for student-assignment review."""

from __future__ import annotations

from .occupancy import request_occupied_half_segments

# A full target-year run can contain more than ten thousand requests. Keeping
# the selected option plus six stable rejected alternatives gives counselors a
# useful elimination trail while bounding immutable result storage. Omitted
# counts remain explicit so the truncated list is never presented as exhaustive.
CANDIDATE_LEDGER_MAX_REJECTED_ALTERNATIVES = 6


def candidate_rejection(
    *, code, phase, blocking_lock_id=None, blocking_section_id=None,
    blocking_student_id=None, blocking_request_id=None, detail=None,
):
    """Build one stable, non-narrative candidate-elimination fact."""

    return {
        "code": code,
        "phase": phase,
        **({"blocking_lock_id": blocking_lock_id} if blocking_lock_id is not None else {}),
        **({"blocking_section_id": blocking_section_id} if blocking_section_id is not None else {}),
        **({"blocking_student_id": blocking_student_id} if blocking_student_id is not None else {}),
        **({"blocking_request_id": blocking_request_id} if blocking_request_id is not None else {}),
        **({"detail": detail} if detail else {}),
    }


def append_candidate_rejection(candidate, rejection, *, static):
    """Record a distinct rejection without altering the CP-SAT candidate domain."""

    key = (
        rejection["code"],
        rejection.get("blocking_lock_id"),
        rejection.get("blocking_section_id"),
        rejection.get("blocking_student_id"),
        rejection.get("blocking_request_id"),
    )
    collection_key = "static_rejections" if static else "final_rejections"
    if any(
        (
            item["code"],
            item.get("blocking_lock_id"),
            item.get("blocking_section_id"),
            item.get("blocking_student_id"),
            item.get("blocking_request_id"),
        ) == key
        for item in candidate[collection_key]
    ):
        return
    candidate[collection_key].append(rejection)
    if static:
        candidate["is_statically_eligible"] = False


def new_candidate_ledger_entry(
    *, source_key, student_id, request_kind, course_id=None,
    course_offering_id=None, assignment_basis=None, delivery_kind=None,
    duration=None, half_semester_segment=None, paired_half_course_id=None,
):
    """Start mutable internal evidence for one immutable result-ledger row."""

    return {
        "source_key": source_key,
        "request_id": source_key[1],
        "student_id": student_id,
        "request_kind": request_kind,
        "course_id": course_id,
        "course_offering_id": course_offering_id,
        "assignment_basis": assignment_basis,
        "delivery_kind": delivery_kind,
        "duration": duration,
        "half_semester_segment": half_semester_segment,
        "paired_half_course_id": paired_half_course_id,
        "candidates": [],
    }


def new_section_candidate_evidence(*, request, section, timeslots_by_id):
    """Represent a normal section or generic online-supervision seat honestly."""

    timeslot = timeslots_by_id.get(section.timeslot_id)
    is_online_supervision = request.delivery_kind == "online" and section.section_id < 0
    return {
        "candidate_kind": (
            "online_supervision_session" if is_online_supervision else "section"
        ),
        "section_id": None if is_online_supervision else section.section_id,
        "online_supervision_session_id": (
            -section.section_id if is_online_supervision else None
        ),
        "semester": section.semester,
        "timeslot_id": section.timeslot_id,
        "block": timeslot.block if timeslot is not None else None,
        "half_semester_segment": (
            request.half_semester_segment
            if request.delivery_kind == "online"
            else section.half_semester_segment
        ),
        "engine_section_id": section.section_id,
        "occupancy": tuple(
            (section.timeslot_id, segment)
            for segment in request_occupied_half_segments(request, section)
        ),
        "is_statically_eligible": True,
        "static_rejections": [],
        "final_rejections": [],
        "is_selected": False,
    }


def new_commitment_candidate_evidence(
    *, candidate_kind, semester, occupancy, timeslots_by_id, co_op_block_pair=None,
):
    """Represent a Study, Focus, or Co-op time choice without a fake section."""

    return {
        "candidate_kind": candidate_kind,
        "section_id": None,
        "online_supervision_session_id": None,
        "semester": semester,
        "timeslot_id": occupancy[0][0] if occupancy else None,
        "block": (
            timeslots_by_id[occupancy[0][0]].block
            if occupancy and occupancy[0][0] in timeslots_by_id else None
        ),
        "half_semester_segment": None,
        "co_op_block_pair": co_op_block_pair,
        "engine_section_id": None,
        "occupancy": tuple(occupancy),
        "is_statically_eligible": True,
        "static_rejections": [],
        "final_rejections": [],
        "is_selected": False,
    }


def public_candidate_evidence(candidate):
    """Drop engine-only occupancy identities before immutable result storage."""

    return {
        key: value
        for key, value in candidate.items()
        if key not in {"engine_section_id", "occupancy"}
    }
