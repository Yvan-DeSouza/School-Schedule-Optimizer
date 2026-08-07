from rest_framework.exceptions import ValidationError

from backend.apps.common.models import AcademicYear, Room
from backend.apps.constraints.models import TeacherAvailability, TeacherCurrentCourse
from backend.apps.control.models import SectionLock
from backend.apps.courses.models import CourseRequest, Section
from backend.apps.scheduling.models import SectionSchedule, TimeSlot


def _in_use(message, is_referenced):
    if is_referenced:
        raise ValidationError({"detail": message})


def ensure_reference_data_can_be_deleted(instance):
    """Reject deletes that would cascade or detach scheduling data."""
    if isinstance(instance, AcademicYear):
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
        _in_use(
            "Room cannot be deleted because it is referenced by scheduling data.",
            SectionSchedule.objects.filter(room=instance).exists()
            or SectionLock.objects.filter(locked_room=instance).exists(),
        )
    elif isinstance(instance, TimeSlot):
        _in_use(
            "Timeslot cannot be deleted because it is referenced by scheduling data.",
            TeacherAvailability.objects.filter(timeslot=instance).exists()
            or SectionSchedule.objects.filter(timeslot=instance).exists()
            or SectionLock.objects.filter(locked_timeslot=instance).exists(),
        )
