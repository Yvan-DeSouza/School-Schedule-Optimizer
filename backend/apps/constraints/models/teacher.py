from django.db import models

from backend.apps.constraints.models.base import Qualification


class TeacherQualification(models.Model):
    teacher = models.ForeignKey("people.Teacher", on_delete=models.CASCADE)
    qualification = models.ForeignKey(Qualification, on_delete=models.CASCADE)

    class Meta:
        ordering = ["teacher", "qualification"]
        constraints = [
            models.UniqueConstraint(
                fields=["teacher", "qualification"],
                name="unique_teacher_qualification"
            )
        ]

    def __str__(self):
        return f"{self.teacher} - {self.qualification}"


class TeacherCoursePreference(models.Model):
    teacher = models.ForeignKey("people.Teacher", on_delete=models.CASCADE)

    course = models.ForeignKey("courses.Course", on_delete=models.CASCADE)

    class Meta:
        ordering = ["teacher", "course"]
        constraints = [
            models.UniqueConstraint(
                fields=["teacher", "course"],
                name="unique_teacher_preference"
            )
        ]

    def __str__(self):
        return f"{self.teacher} prefers {self.course}"


class TeacherAvailability(models.Model):
    teacher = models.ForeignKey("people.Teacher", on_delete=models.CASCADE)

    timeslot = models.ForeignKey("scheduling.TimeSlot", on_delete=models.CASCADE)

    is_available = models.BooleanField(default=True)

    class Meta:
        ordering = ["teacher", "timeslot"]
        constraints = [
            models.UniqueConstraint(
                fields=["teacher", "timeslot"],
                name="unique_teacher_availability"
            )
        ]

    def __str__(self):
        return f"{self.teacher} availability for {self.timeslot}"


class TeacherCurrentCourse(models.Model):
    teacher = models.ForeignKey("people.Teacher", on_delete=models.CASCADE)

    course = models.ForeignKey("courses.Course", on_delete=models.CASCADE)

    academic_year = models.ForeignKey("common.AcademicYear", on_delete=models.CASCADE)

    class Meta:
        ordering = ["teacher", "academic_year", "course"]
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

    def __str__(self):
        return f"{self.teacher} teaches {self.course} in {self.academic_year}"
