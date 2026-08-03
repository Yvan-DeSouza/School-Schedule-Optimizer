from backend.apps.core import models

from backend.apps.core.models.constraints.base import Qualification
from backend.apps.core.models.scheduling import TimeSlot
from backend.apps.core.models.time import AcademicYear
from backend.apps.core.models.courses import Course
from backend.apps.core.models.people import Teacher

class TeacherQualification(models.Model):
    teacher = models.ForeignKey(Teacher, on_delete=models.CASCADE)
    qualification = models.ForeignKey(Qualification, on_delete=models.CASCADE)
    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["teacher","qualification"],
                name="unique_teacher_qualification"
            )
        ]

class TeacherCoursePreference(models.Model):
    teacher = models.ForeignKey(Teacher, on_delete=models.CASCADE)

    course = models.ForeignKey(Course, on_delete=models.CASCADE)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["teacher", "course"],
                name="unique_teacher_preference"
            )
        ]

class TeacherAvailability(models.Model):
    teacher = models.ForeignKey(Teacher, on_delete=models.CASCADE)

    timeslot = models.ForeignKey(TimeSlot, on_delete=models.CASCADE)

    is_available = models.BooleanField(default=True)
    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["teacher", "timeslot"],
                name="unique_teacher_availability"
            )
        ]

class TeacherCurrentCourse(models.Model):
    teacher = models.ForeignKey(Teacher, on_delete=models.CASCADE)

    course = models.ForeignKey(Course, on_delete=models.CASCADE)

    academic_year = models.ForeignKey(AcademicYear, on_delete=models.CASCADE)
    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "teacher",
                    "course",
                    "academic_year"
                ],
                name="unique_teacher_current_course"
            )
        ]