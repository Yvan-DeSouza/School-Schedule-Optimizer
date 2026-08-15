"""Pure lock-selection helpers for the student-assignment model."""

from __future__ import annotations


def active_locks(data):
    """Return active ordinary locks in their stable snapshot order."""

    return tuple(sorted(
        (lock for lock in data.student_assignment_locks if lock.is_active),
        key=lambda lock: lock.lock_id,
    ))


def special_lock_candidates(locks, *, lock_type, request_id=None):
    """Return active special locks for one source request and commitment kind."""

    return tuple(
        lock
        for lock in locks
        if lock.is_active
        and lock.lock_type == lock_type
        and (
            lock.schedule_commitment_request_id == request_id
            or lock.course_request_id == request_id
        )
    )


def special_lock_allows_candidate(
    locks, *, timeslot_id=None, semester=None, co_op_block_pair=None,
):
    """Apply exact and exclusion choices without silently favoring either one."""

    exact = [lock for lock in locks if lock.lock_mode == "exact"]
    excluded = [lock for lock in locks if lock.lock_mode == "exclude"]
    if any(
        (lock.timeslot_id is not None and lock.timeslot_id != timeslot_id)
        or (lock.semester is not None and lock.semester != semester)
        or (lock.co_op_block_pair is not None and lock.co_op_block_pair != co_op_block_pair)
        for lock in exact
    ):
        return False
    if any(
        (lock.timeslot_id is None or lock.timeslot_id == timeslot_id)
        and (lock.semester is None or lock.semester == semester)
        and (lock.co_op_block_pair is None or lock.co_op_block_pair == co_op_block_pair)
        for lock in excluded
    ):
        return False
    return True
