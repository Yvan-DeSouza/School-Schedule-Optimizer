"""Teacher-owned normalized scheduling qualifications and preferences."""

from django.db import models

from backend.apps.common.constants import (
    QUALIFICATION_REVIEW_CHOICES,
    QUALIFICATION_REVIEW_PENDING,
    QUALIFICATION_SOURCE_CHOICES,
    QUALIFICATION_SOURCE_MANUAL,
)
from backend.apps.constraints.models.base import Qualification


class TeacherQualification(models.Model):
    """Evidence that one teacher holds one normalized qualification."""

    teacher = models.ForeignKey("people.Teacher", on_delete=models.CASCADE)
    qualification = models.ForeignKey(Qualification, on_delete=models.CASCADE)

    # Provenance supports Aspen/manual/import audit without making raw strings
    # part of eligibility matching.
    source_system = models.CharField(
        max_length=20,
        choices=QUALIFICATION_SOURCE_CHOICES,
        default=QUALIFICATION_SOURCE_MANUAL,
    )
    source_record_id = models.CharField(max_length=100, blank=True)
    source_text = models.TextField(blank=True)
    awarded_date_text = models.CharField(max_length=50, blank=True)
    review_status = models.CharField(
        max_length=20,
        choices=QUALIFICATION_REVIEW_CHOICES,
        default=QUALIFICATION_REVIEW_PENDING,
    )
    submitted_by = models.ForeignKey(
        "auth.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="submitted_teacher_qualifications",
    )
    submitted_at = models.DateTimeField(auto_now_add=True)
    reviewed_by = models.ForeignKey(
        "auth.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reviewed_teacher_qualifications",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_reason = models.TextField(blank=True, default="")

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
    """Structured teacher interest in teaching a specific course."""

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
    """Teacher availability for one recurring semester timeslot."""

    teacher = models.ForeignKey("people.Teacher", on_delete=models.CASCADE)

    timeslot = models.ForeignKey("scheduling.TimeSlot", on_delete=models.CASCADE)

    # Explicit false records allow imports to preserve unavailable declarations.
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
    """Teacher/course/year history available to later preference objectives."""

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
