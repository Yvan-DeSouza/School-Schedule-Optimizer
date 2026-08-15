"""Append-only lock workflow for Study, online, Co-op, and Focus decisions."""

from django.db import transaction
from django.utils import timezone

from backend.apps.courses.constants import (
    COURSE_ALLOWED_SEMESTER_1_ONLY,
    COURSE_ALLOWED_SEMESTER_2_ONLY,
    COURSE_DELIVERY_KIND_CO_OP,
    COURSE_DURATION_FULL_SEMESTER,
)
from backend.apps.common.exceptions import DomainConflictError, DomainValidationError
from backend.apps.courses.models import CourseRequest
from backend.apps.scheduling.codes import STUDENT_SPECIAL_COMMITMENT_LOCK_INVALID_TARGET
from backend.apps.scheduling.constants import (
    STUDENT_SPECIAL_COMMITMENT_LOCK_TYPE_CO_OP_TIME,
    STUDENT_SPECIAL_COMMITMENT_LOCK_TYPE_FOCUS_SEMESTER,
)
from backend.apps.scheduling.models import StudentSpecialCommitmentLock


def _clean_reason(value, *, field_name):
    value = value.strip() if isinstance(value, str) else ""
    if not value:
        raise DomainValidationError({field_name: "A non-blank reason is required."})
    return value


def _mandatory_full_semester_request_ids(*, student_id, academic_year, semester):
    """Return requests that must occupy every local block of one semester.

    This deliberately proves only immediate contradictions.  Four mandatory
    full-semester local courses restricted to one semester must use all four
    A-D blocks, regardless of later section selection or optimization.  More
    subtle candidate/capacity conflicts remain the student solver's job.
    """

    allowed_semester = (
        COURSE_ALLOWED_SEMESTER_1_ONLY
        if semester == 1
        else COURSE_ALLOWED_SEMESTER_2_ONLY
    )
    return tuple(CourseRequest.objects.filter(
        student_id=student_id,
        academic_year=academic_year,
        is_mandatory=True,
        course__duration=COURSE_DURATION_FULL_SEMESTER,
        course__allowed_semester=allowed_semester,
    ).exclude(
        course__delivery_kind=COURSE_DELIVERY_KIND_CO_OP,
    ).order_by("id").values_list("id", flat=True))


def _active_target_locks(lock):
    """Load existing active restrictions for the same immutable source request."""

    filters = {
        "academic_year": lock.academic_year,
        "lock_type": lock.lock_type,
        "is_active": True,
    }
    if lock.schedule_commitment_request_id:
        filters["schedule_commitment_request"] = lock.schedule_commitment_request
    else:
        filters["course_request"] = lock.course_request
    return tuple(StudentSpecialCommitmentLock.objects.select_for_update().filter(**filters))


def _allowed_focus_semesters(lock, active_locks):
    locks = (*active_locks, lock)
    exact_semesters = {item.semester for item in locks if item.lock_mode == "exact"}
    if exact_semesters:
        return exact_semesters
    return {1, 2} - {item.semester for item in locks if item.lock_mode == "exclude"}


def _allowed_co_op_placements(lock, active_locks):
    all_placements = {
        (semester, block_pair)
        for semester in (1, 2)
        for block_pair in ("a_b", "c_d")
    }
    locks = (*active_locks, lock)
    exact_placements = {
        (item.semester, item.co_op_block_pair)
        for item in locks if item.lock_mode == "exact"
    }
    if exact_placements:
        return exact_placements
    excluded_placements = {
        (item.semester, item.co_op_block_pair)
        for item in locks if item.lock_mode == "exclude"
    }
    return all_placements - excluded_placements


def _validate_immediate_occupancy_conflict(lock):
    """Reject a lock whose only allowed targets contradict known demand.

    A special lock is a counselor's hard decision, not an optimization hint.
    Once current mandatory course demand proves every target is impossible, the
    service fails closed before persisting an immutable invalid decision.  This
    check intentionally does not attempt to replicate student assignment.
    """

    if lock.lock_type not in {
        STUDENT_SPECIAL_COMMITMENT_LOCK_TYPE_FOCUS_SEMESTER,
        STUDENT_SPECIAL_COMMITMENT_LOCK_TYPE_CO_OP_TIME,
    }:
        return
    active_locks = _active_target_locks(lock)
    if lock.lock_type == STUDENT_SPECIAL_COMMITMENT_LOCK_TYPE_FOCUS_SEMESTER:
        allowed_semesters = _allowed_focus_semesters(lock, active_locks)
        student_id = lock.schedule_commitment_request.student_id
        blocked_request_ids = {
            semester: _mandatory_full_semester_request_ids(
                student_id=student_id,
                academic_year=lock.academic_year,
                semester=semester,
            )
            for semester in allowed_semesters
        }
        valid_semesters = {
            semester for semester, request_ids in blocked_request_ids.items()
            if len(request_ids) < 4
        }
        target = {"allowed_semesters": sorted(allowed_semesters)}
    else:
        allowed_placements = _allowed_co_op_placements(lock, active_locks)
        student_id = lock.course_request.student_id
        blocked_request_ids = {
            placement: _mandatory_full_semester_request_ids(
                student_id=student_id,
                academic_year=lock.academic_year,
                semester=placement[0],
            )
            for placement in allowed_placements
        }
        valid_semesters = {
            placement for placement, request_ids in blocked_request_ids.items()
            if len(request_ids) < 4
        }
        target = {
            "allowed_placements": [
                {"semester": semester, "co_op_block_pair": block_pair}
                for semester, block_pair in sorted(allowed_placements)
            ],
        }
    if valid_semesters:
        return
    conflicting_request_ids = sorted({
        request_id
        for request_ids in blocked_request_ids.values()
        for request_id in request_ids
    })
    raise DomainConflictError({
        "code": STUDENT_SPECIAL_COMMITMENT_LOCK_INVALID_TARGET,
        "detail": "The special commitment lock conflicts with mandatory full-semester course occupancy.",
        "student_id": student_id,
        "conflicting_mandatory_course_request_ids": conflicting_request_ids,
        **target,
    })


@transaction.atomic
def create_student_special_commitment_lock(*, academic_year, created_by, reason, **targets):
    """Persist one counselor restriction after enforcing its narrow target shape.

    Locks are immutable because a later student-assignment review must be able
    to prove exactly which counselor decision constrained its recommendation.
    """

    lock = StudentSpecialCommitmentLock(
        academic_year=academic_year,
        created_by=created_by,
        reason=_clean_reason(reason, field_name="reason"),
        **targets,
    )
    try:
        lock.full_clean()
    except Exception as error:  # Django collects field-specific ValidationError detail.
        from django.core.exceptions import ValidationError

        if isinstance(error, ValidationError):
            raise DomainValidationError(error.message_dict) from error
        raise
    _validate_immediate_occupancy_conflict(lock)
    if lock.lock_mode == "exact":
        # Two different exact locations for one request are not a helpful
        # preference; they are an ambiguous hard constraint. Exclusions can
        # accumulate, but exact locks need a single reviewed identity.
        duplicate = StudentSpecialCommitmentLock.objects.select_for_update().filter(
            academic_year=academic_year,
            lock_type=lock.lock_type,
            lock_mode="exact",
            schedule_commitment_request=lock.schedule_commitment_request,
            course_request=lock.course_request,
            is_active=True,
        )
        if duplicate.exists():
            raise DomainConflictError({
                "detail": "Release the current exact special-commitment lock before creating another one."
            })
    lock.save()
    return lock


@transaction.atomic
def release_student_special_commitment_lock(lock, *, released_by, release_reason):
    """Release rather than delete a counselor decision, preserving its audit trail."""

    lock = StudentSpecialCommitmentLock.objects.select_for_update().get(pk=lock.pk)
    if not lock.is_active:
        raise DomainConflictError({"detail": "This special commitment lock is already released."})
    lock.is_active = False
    lock.released_at = timezone.now()
    lock.released_by = released_by
    lock.release_reason = _clean_reason(release_reason, field_name="release_reason")
    try:
        lock.full_clean()
    except Exception as error:
        from django.core.exceptions import ValidationError

        if isinstance(error, ValidationError):
            raise DomainValidationError(error.message_dict) from error
        raise
    lock.save(update_fields=["is_active", "released_at", "released_by", "release_reason"])
    return lock
