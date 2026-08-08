"""Course catalog, operational sections, demand requests, and prerequisites."""

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
    """Catalog course plus policy references used by section planning."""

    name = models.CharField(max_length=200)

    grade_level = models.IntegerField(
        choices=GRADE_LEVEL_CHOICES
    )

    course_code = models.CharField(max_length=20, unique=True)
    category = models.CharField(
        max_length=50,
        choices=COURSE_CATEGORY_CHOICES,
    )

    # Deprecated compatibility fields remain for existing section CRUD, imports,
    # and the legacy estimator. The CP-SAT planner uses capacity_profile.
    capacity_min = models.IntegerField(
        validators=[MinValueValidator(1)],
        default=10,
    )
    capacity_max = models.IntegerField(
        validators=[MinValueValidator(1)],
        default=35,
    )
    # PROTECT prevents an active policy from disappearing underneath courses.
    capacity_profile = models.ForeignKey("scheduling.CapacityProfile", on_delete=models.PROTECT, related_name="courses")
    # Priority is explicit administrator policy and is never inferred from a
    # request's mandatory flag, course code, grade, or category.
    priority_profile = models.ForeignKey("scheduling.CoursePriorityProfile", on_delete=models.PROTECT, related_name="courses")
    # Semester availability is a hard catalog rule for planning and placement.
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
        # Direct ORM creation is common in imports and tests, so guarantee
        # profiles even when callers did not pass the new foreign keys. The local
        # import avoids a models/services cycle during Django app initialization.
        if not self.capacity_profile_id or not self.priority_profile_id:
            from backend.apps.scheduling.services.planning_configuration import ensure_default_planning_profiles

            capacity_profile, priority_profile = ensure_default_planning_profiles()
            if not self.capacity_profile_id:
                self.capacity_profile = capacity_profile
            if not self.priority_profile_id:
                self.priority_profile = priority_profile
        return super().save(*args, **kwargs)


class Section(models.Model):
    """One offered instance of a course in a particular semester."""

    course = models.ForeignKey(Course, on_delete=models.CASCADE)
    section_number = models.CharField(max_length=10)
    academic_year = models.ForeignKey("common.AcademicYear", on_delete=models.CASCADE)
    semester = models.IntegerField(choices=SEMESTER_CHOICES)
    # Teacher is optional because plan approval creates unstaffed drafts; named
    # assignment belongs to a later scheduling stage.
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

    # Locked sections are accepted decisions that downstream automation must not
    # silently move or reassign.
    is_locked = models.BooleanField(default=False)
    # Manual/legacy sections legitimately have no approval source. Generated
    # drafts link to the exact per-course decision, then to approval/run/user.
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
    """Assignment of one student to one offered section."""

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
    """Student demand signal for a course in one planning year."""

    student = models.ForeignKey("people.Student", on_delete=models.CASCADE)
    academic_year = models.ForeignKey("common.AcademicYear", on_delete=models.CASCADE)

    course = models.ForeignKey(Course, on_delete=models.CASCADE)

    # Mandatory is request provenance, not a source for course priority.
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
    """Directed catalog prerequisite relation for student scheduling."""

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
