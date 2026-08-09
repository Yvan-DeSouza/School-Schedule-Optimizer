"""Mutable teacher-roster workflow used before immutable staffing runs."""

from django.db import transaction
from django.utils import timezone

from backend.apps.common.exceptions import DomainValidationError
from backend.apps.common.constants import (
    TEACHER_ROSTER_STATUS_DRAFT,
    TEACHER_ROSTER_STATUS_READY,
)
from backend.apps.people.models import Teacher
from backend.apps.scheduling.models import (
    TeacherPlanningAnnualCapacity,
    TeacherPlanningCapacity,
    TeacherPlanningRoster,
    TeacherPlanningRosterMember,
)


class StaffingConfigurationError(DomainValidationError):
    """A roster cannot be changed or confirmed as requested."""


def invalidate_roster(academic_year_id):
    """Return a ready roster to draft after any staffing-input mutation."""

    TeacherPlanningRoster.objects.filter(
        academic_year_id=academic_year_id,
        status=TEACHER_ROSTER_STATUS_READY,
    ).update(
        status=TEACHER_ROSTER_STATUS_DRAFT,
        confirmed_by=None,
        confirmed_at=None,
    )


def invalidate_teacher_rosters(teacher_id):
    """Invalidate every ready year that intentionally includes this teacher."""

    TeacherPlanningRoster.objects.filter(
        members__teacher_id=teacher_id,
        status=TEACHER_ROSTER_STATUS_READY,
    ).update(
        status=TEACHER_ROSTER_STATUS_DRAFT,
        confirmed_by=None,
        confirmed_at=None,
    )


@transaction.atomic
def set_roster_members(roster, *, teacher_ids, actor):
    """Replace draft membership and require an explicit later confirmation."""

    roster = TeacherPlanningRoster.objects.select_for_update().get(pk=roster.pk)
    teacher_ids = list(dict.fromkeys(teacher_ids))
    teachers = list(
        Teacher.objects.filter(id__in=teacher_ids, is_archived=False).order_by("id")
    )
    if {teacher.id for teacher in teachers} != set(teacher_ids):
        raise StaffingConfigurationError({
            "teacher_ids": "Every roster teacher must exist and be active."
        })
    roster.members.all().delete()
    TeacherPlanningRosterMember.objects.bulk_create([
        TeacherPlanningRosterMember(roster=roster, teacher=teacher, added_by=actor)
        for teacher in teachers
    ])
    roster.status = TEACHER_ROSTER_STATUS_DRAFT
    roster.confirmed_by = None
    roster.confirmed_at = None
    roster.save(update_fields=["status", "confirmed_by", "confirmed_at"])
    return roster


@transaction.atomic
def confirm_roster_ready(roster, *, actor):
    """Confirm only a complete semester-and-annual capacity snapshot as ready."""

    roster = TeacherPlanningRoster.objects.select_for_update().get(pk=roster.pk)
    teacher_ids = list(roster.members.values_list("teacher_id", flat=True))
    if not teacher_ids:
        raise StaffingConfigurationError({
            "members": "Add at least one active teacher before confirming the roster."
        })
    archived = list(
        Teacher.objects.filter(id__in=teacher_ids, is_archived=True).values_list("id", flat=True)
    )
    if archived:
        raise StaffingConfigurationError({
            "members": f"Archived teachers cannot be confirmed: {archived}."
        })
    capacity_keys = set(
        TeacherPlanningCapacity.objects.filter(
            academic_year=roster.academic_year,
            teacher_id__in=teacher_ids,
        ).values_list("teacher_id", "semester")
    )
    missing = [
        {"teacher_id": teacher_id, "semester": semester}
        for teacher_id in teacher_ids
        for semester in (1, 2)
        if (teacher_id, semester) not in capacity_keys
    ]
    if missing:
        raise StaffingConfigurationError({
            "capacities": {
                "message": (
                    "Every roster teacher needs both semester capacity rows; "
                    "use an explicit zero when unavailable."
                ),
                "missing": missing,
            }
        })
    annual_teacher_ids = set(
        TeacherPlanningAnnualCapacity.objects.filter(
            academic_year=roster.academic_year,
            teacher_id__in=teacher_ids,
        ).values_list("teacher_id", flat=True)
    )
    annual_missing = [teacher_id for teacher_id in teacher_ids if teacher_id not in annual_teacher_ids]
    if annual_missing:
        raise StaffingConfigurationError({
            "annual_capacities": {
                "message": "Every roster teacher needs an annual capacity row; use an explicit zero when unavailable.",
                "missing_teacher_ids": annual_missing,
            }
        })
    roster.status = TEACHER_ROSTER_STATUS_READY
    roster.confirmed_by = actor
    roster.confirmed_at = timezone.now()
    roster.save(update_fields=["status", "confirmed_by", "confirmed_at"])
    return roster
