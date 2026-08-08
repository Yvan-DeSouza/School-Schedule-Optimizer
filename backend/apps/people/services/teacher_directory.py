"""Teacher-directory state transitions with audit and roster side effects."""

from django.db import transaction

from backend.apps.common.exceptions import DomainValidationError
from backend.apps.people.models import Teacher, TeacherStatusDecision
from backend.apps.scheduling.services.staffing_configuration import (
    invalidate_teacher_rosters,
)


def _clean_reason(reason):
    if not isinstance(reason, str) or not reason.strip():
        raise DomainValidationError({"reason": "A teacher status reason is required."})
    return reason.strip()


@transaction.atomic
def archive_teacher(teacher, *, actor, reason):
    """Mark a teacher inactive while preserving staffing history."""

    reason = _clean_reason(reason)
    teacher = Teacher.objects.select_for_update().get(pk=teacher.pk)
    if teacher.is_archived:
        raise DomainValidationError({"detail": "This teacher is already archived."})
    teacher.is_archived = True
    teacher.save(update_fields=["is_archived"])
    TeacherStatusDecision.objects.create(
        teacher=teacher,
        action="archived",
        reason=reason,
        decided_by=actor,
    )
    invalidate_teacher_rosters(teacher.id)
    return teacher


@transaction.atomic
def restore_teacher(teacher, *, actor, reason):
    """Reactivate an archived teacher and record the human reason."""

    reason = _clean_reason(reason)
    teacher = Teacher.objects.select_for_update().get(pk=teacher.pk)
    if not teacher.is_archived:
        raise DomainValidationError({"detail": "This teacher is not archived."})
    teacher.is_archived = False
    teacher.save(update_fields=["is_archived"])
    TeacherStatusDecision.objects.create(
        teacher=teacher,
        action="restored",
        reason=reason,
        decided_by=actor,
    )
    # If this teacher is part of any ready roster, the active-teacher fact has
    # changed and staffing inputs need explicit reconfirmation.
    invalidate_teacher_rosters(teacher.id)
    return teacher
