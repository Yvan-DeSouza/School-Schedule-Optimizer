"""Course-level room, qualification, and co-request conflict constraints."""

from django.db import models
from django.core.validators import MaxValueValidator, MinValueValidator

from backend.apps.common.constants import (
    QUALIFICATION_ENFORCEMENT_CHOICES,
    QUALIFICATION_ENFORCEMENT_REQUIRED,
    ROOM_TYPE_CHOICES,
)
from backend.apps.constraints.models.base import Qualification
from backend.apps.constraints.constants import COURSE_CONFLICT_MATRIX_INITIALIZATION_CHOICES


class CourseRoomRequirement(models.Model):
    """Canonical room capability required by a course."""

    course = models.ForeignKey("courses.Course", on_delete=models.CASCADE)

    room_type = models.CharField(max_length=20, choices=ROOM_TYPE_CHOICES)

    class Meta:
        ordering = ["course", "room_type"]
        constraints = [
            models.UniqueConstraint(
                fields=["course", "room_type"],
                name="unique_course_room_requirement"
            )
        ]

    def __str__(self):
        return f"{self.course} requires {self.room_type}"


class CourseQualificationRequirement(models.Model):
    """Required or preferred normalized teachable for a course."""

    course = models.ForeignKey("courses.Course", on_delete=models.CASCADE)

    qualification = models.ForeignKey(Qualification, on_delete=models.CASCADE)

    # Required rules constrain senior-course eligibility; preferred rules remain
    # available for softer assignment objectives.
    enforcement = models.CharField(
        max_length=20,
        choices=QUALIFICATION_ENFORCEMENT_CHOICES,
        default=QUALIFICATION_ENFORCEMENT_REQUIRED,
    )

    class Meta:
        ordering = ["course", "enforcement", "qualification"]
        constraints = [
            models.UniqueConstraint(
                fields=["course", "qualification"],
                name="unique_course_qualification_requirement"
            )
        ]

    def __str__(self):
        return f"{self.course} {self.enforcement} {self.qualification}"


class CourseConflictMatrix(models.Model):
    """Counselor-owned co-request matrix for one academic-year cohort."""

    academic_year = models.OneToOneField("common.AcademicYear", on_delete=models.CASCADE)
    initialization_mode = models.CharField(
        max_length=30,
        choices=COURSE_CONFLICT_MATRIX_INITIALIZATION_CHOICES,
    )
    source_matrix = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="copied_to_matrices",
    )
    revision = models.PositiveIntegerField(default=1)
    request_fingerprint = models.CharField(max_length=64, blank=True, default="")
    created_by = models.ForeignKey("auth.User", null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    refreshed_by = models.ForeignKey(
        "auth.User", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="refreshed_course_conflict_matrices",
    )
    refreshed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-academic_year__name"]

    def __str__(self):
        return f"Course conflict matrix for {self.academic_year}"


class CourseConflict(models.Model):
    """One unordered, year-specific counselor-adjustable course-pair score."""

    # ``matrix`` remains nullable only for legacy development rows created
    # before the annual matrix workflow. Placement deliberately ignores those
    # rows; all new rows belong to a matrix.
    matrix = models.ForeignKey(
        CourseConflictMatrix, null=True, blank=True, on_delete=models.CASCADE,
        related_name="conflicts",
    )

    course_a = models.ForeignKey("courses.Course", on_delete=models.CASCADE, related_name="conflicts_as_a")
    course_b = models.ForeignKey("courses.Course", on_delete=models.CASCADE, related_name="conflicts_as_b")
    # The calculated value remains visible after a counselor changes ``weight``.
    # Keeping both prevents expert judgment from erasing the demand evidence.
    computed_weight = models.DecimalField(
        max_digits=5, decimal_places=2, default=0,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )
    weight = models.DecimalField(
        max_digits=5, decimal_places=2, default=0,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )
    co_request_count = models.PositiveIntegerField(default=0)
    union_request_count = models.PositiveIntegerField(default=0)
    estimated_retained_co_request_count = models.DecimalField(
        max_digits=10, decimal_places=2, default=0,
    )
    uses_current_demand_fallback = models.BooleanField(default=False)
    is_overridden = models.BooleanField(default=False)

    class Meta:
        ordering = ["course_a", "course_b"]
        constraints = [
            models.UniqueConstraint(
                fields=["matrix", "course_a", "course_b"],
                name="unique_matrix_course_conflict",
            ),
            models.CheckConstraint(
                # A self-edge provides no scheduling information and would
                # distort placement objectives.
                condition=~models.Q(course_a=models.F("course_b")),
                name="course_cannot_conflict_with_itself",
            )
        ]

    def __str__(self):
        return f"{self.course_a} conflicts with {self.course_b}"


class CourseConflictAdjustment(models.Model):
    """Append-only explanation for a counselor change to a matrix score."""

    conflict = models.ForeignKey(
        CourseConflict, on_delete=models.PROTECT, related_name="adjustments",
    )
    previous_weight = models.DecimalField(max_digits=5, decimal_places=2)
    new_weight = models.DecimalField(max_digits=5, decimal_places=2)
    reason = models.TextField()
    adjusted_by = models.ForeignKey("auth.User", null=True, on_delete=models.SET_NULL)
    adjusted_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["adjusted_at", "id"]

    def save(self, *args, **kwargs):
        if self.pk:
            from django.core.exceptions import ValidationError

            raise ValidationError("Course conflict adjustments are immutable.")
        return super().save(*args, **kwargs)
