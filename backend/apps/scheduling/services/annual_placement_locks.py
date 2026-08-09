"""Counselor-managed locks for annual virtual placement slots."""

from django.db import transaction

from backend.apps.common.exceptions import DomainConflictError, DomainValidationError
from backend.apps.scheduling.models import AnnualPlacementLock, SectionBudgetApprovalOffering
from backend.apps.courses.constants import (
    COURSE_ALLOWED_SEMESTER_1_ONLY,
    COURSE_ALLOWED_SEMESTER_2_ONLY,
    DELIVERY_GROUP_STATUS_ACTIVE,
)


def _clean_reason(reason):
    if not isinstance(reason, str) or not reason.strip():
        raise DomainValidationError({"reason": "A reason is required for an annual placement lock."})
    return reason.strip()


def _validate_lock_values(*, academic_year, delivery_group, annual_index, locked_timeslot):
    if delivery_group.academic_year_id != academic_year.id:
        raise DomainValidationError({"delivery_group": "Delivery group must belong to the selected academic year."})
    if locked_timeslot.academic_year_id != academic_year.id:
        raise DomainValidationError({"locked_timeslot": "Timeslot must belong to the selected academic year."})
    if not locked_timeslot.is_available:
        raise DomainValidationError({"locked_timeslot": "An unavailable timeslot cannot be locked."})
    if delivery_group.status != DELIVERY_GROUP_STATUS_ACTIVE:
        raise DomainValidationError({"delivery_group": "Only an active delivery group may receive an annual lock."})
    legal_semesters = {1, 2}
    for offering in delivery_group.offerings.select_related("course"):
        if offering.course.allowed_semester == COURSE_ALLOWED_SEMESTER_1_ONLY:
            legal_semesters &= {1}
        elif offering.course.allowed_semester == COURSE_ALLOWED_SEMESTER_2_ONLY:
            legal_semesters &= {2}
    if locked_timeslot.semester not in legal_semesters:
        raise DomainValidationError({"locked_timeslot": "Timeslot semester is not legal for every course in this delivery group."})
    if annual_index < 1:
        raise DomainValidationError({"annual_index": "Annual slot numbering starts at 1."})
    maximum = max(
        SectionBudgetApprovalOffering.objects.filter(
            approval__budget_run__academic_year=academic_year,
            delivery_group=delivery_group,
        ).values_list("approved_annual_count", flat=True),
        default=0,
    )
    if annual_index > maximum:
        raise DomainConflictError({
            "code": "annual_lock_outside_annual_count",
            "detail": "This annual slot is outside every approved annual count for the delivery group.",
        })


@transaction.atomic
def create_annual_placement_lock(*, academic_year, delivery_group, annual_index, locked_timeslot, reason, actor):
    """Create one exact time lock for a stable virtual annual section identity."""

    _validate_lock_values(
        academic_year=academic_year, delivery_group=delivery_group,
        annual_index=annual_index, locked_timeslot=locked_timeslot,
    )
    if AnnualPlacementLock.objects.select_for_update().filter(
        academic_year=academic_year, delivery_group=delivery_group, annual_index=annual_index,
    ).exists():
        raise DomainConflictError({"detail": "This annual delivery slot already has a placement lock."})
    return AnnualPlacementLock.objects.create(
        academic_year=academic_year, delivery_group=delivery_group,
        annual_index=annual_index, locked_timeslot=locked_timeslot,
        reason=_clean_reason(reason), created_by=actor, updated_by=actor,
    )


@transaction.atomic
def update_annual_placement_lock(lock, *, locked_timeslot, reason, actor):
    """Change an unmaterialized virtual lock with a fresh counselor reason."""

    lock = AnnualPlacementLock.objects.select_for_update().get(pk=lock.pk)
    if lock.materialized_section_id:
        raise DomainConflictError({"detail": "A materialized annual lock is retained as approved history and cannot change."})
    _validate_lock_values(
        academic_year=lock.academic_year, delivery_group=lock.delivery_group,
        annual_index=lock.annual_index, locked_timeslot=locked_timeslot,
    )
    lock.locked_timeslot = locked_timeslot
    lock.reason = _clean_reason(reason)
    lock.updated_by = actor
    lock.save(update_fields=["locked_timeslot", "reason", "updated_by", "updated_at"])
    return lock


@transaction.atomic
def delete_annual_placement_lock(lock, *, reason):
    """Remove only a not-yet-materialized pre-section planning decision."""

    lock = AnnualPlacementLock.objects.select_for_update().get(pk=lock.pk)
    if lock.materialized_section_id:
        raise DomainConflictError({"detail": "A materialized annual lock cannot be deleted from approved history."})
    _clean_reason(reason)
    lock.delete()
