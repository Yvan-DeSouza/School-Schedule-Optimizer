"""Audited student-assignment lock state transitions and validation."""

from django.db import transaction
from django.utils import timezone

from backend.apps.common.exceptions import DomainConflictError, DomainValidationError
from backend.apps.scheduling.codes import (
    STUDENT_ASSIGNMENT_CONFLICT,
    STUDENT_ASSIGNMENT_LOCK_FINAL_STAFFING_REQUIRED,
)
from backend.apps.scheduling.constants import (
    STUDENT_ASSIGNMENT_LOCK_TYPE_STUDENT_GROUP,
    STUDENT_ASSIGNMENT_LOCK_TYPE_STUDENT_TEACHER,
    STUDENT_ASSIGNMENT_STAFFING_MODE_FINAL_STAFFING,
)
from backend.apps.scheduling.models import StudentAssignmentLock, StudentAssignmentLockMember


def validate_student_assignment_lock_staffing_mode(*, lock_type, staffing_mode):
    """Reject teacher locks unless the caller explicitly trusts final staffing."""

    # A persistent teacher lock cannot be safely interpreted when the selected
    # run ignores, partially knows, or provisionally knows teacher identity.
    if (
        lock_type == STUDENT_ASSIGNMENT_LOCK_TYPE_STUDENT_TEACHER
        and staffing_mode != STUDENT_ASSIGNMENT_STAFFING_MODE_FINAL_STAFFING
    ):
        raise DomainValidationError({
            "code": STUDENT_ASSIGNMENT_LOCK_FINAL_STAFFING_REQUIRED,
            "detail": "Student-to-teacher locks require final_staffing.",
        })


def _member_ids(students):
    return sorted({student.pk if hasattr(student, "pk") else int(student) for student in students})


@transaction.atomic
def create_student_assignment_lock(
    *, academic_year, lock_type, created_by, reason, student=None, section=None,
    course=None, teacher=None, group_students=(), staffing_mode=None,
):
    """Create one immutable-target lock and, for groups, all memberships atomically."""

    validate_student_assignment_lock_staffing_mode(
        lock_type=lock_type,
        staffing_mode=staffing_mode,
    )
    member_ids = _member_ids(group_students)
    if lock_type == STUDENT_ASSIGNMENT_LOCK_TYPE_STUDENT_GROUP:
        if len(member_ids) < 2:
            raise DomainValidationError({
                "group_students": "A student-group lock requires at least two students."
            })
    elif member_ids:
        raise DomainValidationError({
            "group_students": "Only a student-group lock may contain memberships."
        })

    lock = StudentAssignmentLock(
        academic_year=academic_year,
        lock_type=lock_type,
        student=student,
        section=section,
        course=course,
        teacher=teacher,
        reason=reason,
        created_by=created_by,
    )
    # Model validation owns target shape, academic-year matching, active-section
    # protection, and the non-blank creation reason for non-HTTP callers too.
    lock.full_clean()
    lock.save()

    for student_id in member_ids:
        membership = StudentAssignmentLockMember(
            student_assignment_lock=lock,
            student_id=student_id,
        )
        membership.full_clean()
        membership.save()
    return lock


@transaction.atomic
def release_student_assignment_lock(lock, *, released_by, release_reason):
    """Perform the only allowed mutation: one audited active-to-released transition."""

    if not isinstance(release_reason, str) or not release_reason.strip():
        raise DomainValidationError({
            "release_reason": "A non-blank release reason is required."
        })
    current = StudentAssignmentLock.objects.select_for_update().get(pk=lock.pk)
    if not current.is_active:
        raise DomainConflictError({
            "code": STUDENT_ASSIGNMENT_CONFLICT,
            "detail": "A released student-assignment lock cannot be released again.",
        })
    current.is_active = False
    current.released_at = timezone.now()
    current.released_by = released_by
    current.release_reason = release_reason.strip()
    current.full_clean()
    current.save()
    return current
