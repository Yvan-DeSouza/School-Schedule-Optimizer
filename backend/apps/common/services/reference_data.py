"""Deletion guards that protect shared reference data from destructive cascades."""

from backend.apps.common.exceptions import DomainValidationError

from backend.apps.common.models import AcademicYear, Room
from backend.apps.constraints.models import TeacherAvailability, TeacherCurrentCourse
from backend.apps.control.models import SectionLock
from backend.apps.courses.models import CourseRequest, Section
from backend.apps.scheduling.models import SectionSchedule, TimeSlot


def _in_use(message, is_referenced):
    """Raise a consistent API error when a precomputed reference check is true."""

    if is_referenced:
        raise DomainValidationError({"detail": message})


def ensure_reference_data_can_be_deleted(instance):
    """Reject deletes that would cascade or detach scheduling data."""
    if isinstance(instance, AcademicYear):
        # Academic years connect nearly every planning area; enumerate important
        # reverse uses explicitly instead of permitting surprising cascades.
        _in_use(
            "Academic year cannot be deleted because it is referenced by school data.",
            any((
                Section.objects.filter(academic_year=instance).exists(),
                CourseRequest.objects.filter(academic_year=instance).exists(),
                TeacherCurrentCourse.objects.filter(academic_year=instance).exists(),
                TimeSlot.objects.filter(academic_year=instance).exists(),
                instance.student_set.exists(),
                instance.historicalcoursedemand_set.exists(),
            )),
        )
    elif isinstance(instance, Room):
        # SET_NULL relationships would otherwise erase accepted placement/lock
        # context without asking the administrator to reconcile it.
        _in_use(
            "Room cannot be deleted because it is referenced by scheduling data.",
            SectionSchedule.objects.filter(room=instance).exists()
            or SectionLock.objects.filter(locked_room=instance).exists(),
        )
    elif isinstance(instance, TimeSlot):
        # Availability, placements, and locks all give a slot operational meaning.
        _in_use(
            "Timeslot cannot be deleted because it is referenced by scheduling data.",
            TeacherAvailability.objects.filter(timeslot=instance).exists()
            or SectionSchedule.objects.filter(timeslot=instance).exists()
            or SectionLock.objects.filter(locked_timeslot=instance).exists(),
        )
