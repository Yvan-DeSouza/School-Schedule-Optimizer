"""Append-only lock workflow for Study, online, Co-op, and Focus decisions."""

from django.db import transaction
from django.utils import timezone

from backend.apps.common.exceptions import DomainConflictError, DomainValidationError
from backend.apps.scheduling.models import StudentSpecialCommitmentLock


def _clean_reason(value, *, field_name):
    value = value.strip() if isinstance(value, str) else ""
    if not value:
        raise DomainValidationError({field_name: "A non-blank reason is required."})
    return value


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
