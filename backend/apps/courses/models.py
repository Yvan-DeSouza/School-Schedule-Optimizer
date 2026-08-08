from django.core.validators import MinValueValidator
from django.db import models

from backend.apps.common.constants import (
    COURSE_ALLOWED_SEMESTER_CHOICES,
    COURSE_ALLOWED_SEMESTER_EITHER,
    COURSE_CATEGORY_CHOICES,
    COURSE_REQUEST_TYPE_CHOICES,
    GRADE_LEVEL_CHOICES,
    SEMESTER_CHOICES,
)


class Course(models.Model):
    name = models.CharField(max_length=200)

    grade_level = models.IntegerField(
        choices=GRADE_LEVEL_CHOICES
    )

    course_code = models.CharField(max_length=20, unique=True)
    category = models.CharField(
        max_length=50,
        choices=COURSE_CATEGORY_CHOICES,
    )

    # Deprecated compatibility fields remain for existing section CRUD and
    # imports. The planning engine exclusively uses capacity_profile.
    capacity_min = models.IntegerField(
        validators=[MinValueValidator(1)],
        default=10,
    )
    capacity_max = models.IntegerField(
        validators=[MinValueValidator(1)],
        default=35,
    )
    capacity_profile = models.ForeignKey("scheduling.CapacityProfile", on_delete=models.PROTECT, related_name="courses")
    priority_profile = models.ForeignKey("scheduling.CoursePriorityProfile", on_delete=models.PROTECT, related_name="courses")
    allowed_semester = models.CharField(
        max_length=20,
        choices=COURSE_ALLOWED_SEMESTER_CHOICES,
        default=COURSE_ALLOWED_SEMESTER_EITHER,
    )
    is_online = models.BooleanField(default=False)

    class Meta:
        ordering = ["course_code"]

    def __str__(self):
        return f"{self.course_code} - {self.name}"

    def save(self, *args, **kwargs):
        # Direct ORM creation is common in imports and tests, so guarantees
        # profiles even when callers did not pass the new foreign keys.
        if not self.capacity_profile_id or not self.priority_profile_id:
            from backend.apps.scheduling.services.planning_configuration import ensure_default_planning_profiles

            capacity_profile, priority_profile = ensure_default_planning_profiles()
            if not self.capacity_profile_id:
                self.capacity_profile = capacity_profile
            if not self.priority_profile_id:
                self.priority_profile = priority_profile
        return super().save(*args, **kwargs)


class Section(models.Model):
    course = models.ForeignKey(Course, on_delete=models.CASCADE)
    section_number = models.CharField(max_length=10)
    academic_year = models.ForeignKey("common.AcademicYear", on_delete=models.CASCADE)
    semester = models.IntegerField(choices=SEMESTER_CHOICES)
    teacher = models.ForeignKey(
        "people.Teacher",
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )

    capacity_min = models.IntegerField(
        validators=[MinValueValidator(1)]
    )
    capacity_max = models.IntegerField(
        validators=[MinValueValidator(1)]
    )

    is_locked = models.BooleanField(default=False)
    planning_approval_course = models.ForeignKey(
        "scheduling.SectionPlanningApprovalCourse",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="generated_sections",
    )

    class Meta:
        ordering = ["academic_year", "course", "section_number"]
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "course",
                    "section_number",
                    "academic_year"
                ],
                name="unique_course_section_year"
            )
        ]

    def __str__(self):
        return f"{self.course.course_code}-{self.section_number} ({self.academic_year})"


class Enrollment(models.Model):
    student = models.ForeignKey("people.Student", on_delete=models.CASCADE)
    section = models.ForeignKey(Section, on_delete=models.CASCADE)

    class Meta:
        ordering = ["student", "section"]
        constraints = [
            models.UniqueConstraint(
                fields=["student", "section"],
                name="unique_student_section_enrollment"
            )
        ]

    def __str__(self):
        return f"{self.student} -> {self.section}"


class CourseRequest(models.Model):
    student = models.ForeignKey("people.Student", on_delete=models.CASCADE)
    academic_year = models.ForeignKey("common.AcademicYear", on_delete=models.CASCADE)

    course = models.ForeignKey(Course, on_delete=models.CASCADE)

    is_mandatory = models.BooleanField(default=False)

    request_type = models.CharField(max_length=20, choices=COURSE_REQUEST_TYPE_CHOICES)

    class Meta:
        ordering = ["academic_year", "student", "request_type", "course"]
        constraints = [
            models.UniqueConstraint(
                fields=["student", "course", "academic_year"],
                name="unique_student_course_request"
            )
        ]

    def __str__(self):
        return f"{self.student} requests {self.course} ({self.request_type})"


class CoursePrerequisite(models.Model):
    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name="prerequisites")

    prerequisite = models.ForeignKey(Course, on_delete=models.CASCADE, related_name="required_for")

    class Meta:
        ordering = ["course", "prerequisite"]
        constraints = [
            models.UniqueConstraint(
                fields=["course", "prerequisite"],
                name="unique_course_prerequisite"
            )
        ]

    def __str__(self):
        return f"{self.course} requires {self.prerequisite}"
