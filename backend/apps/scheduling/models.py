from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models

from backend.apps.common.constants import (
    CAPACITY_PROFILE_SCOPE_CHOICES,
    CAPACITY_PROFILE_SCOPE_SHARED,
    COURSE_PRIORITY_TIER_CHOICES,
    COURSE_PRIORITY_TIER_STANDARD,
    SCHEDULE_BLOCK_CHOICES,
    SECTION_PLANNING_RUN_STATUS_CHOICES,
    SEMESTER_CHOICES,
)


class CapacityProfile(models.Model):
    """Reusable class-size policy used by future section-planning runs."""

    name = models.CharField(max_length=120, unique=True)
    scope = models.CharField(max_length=20, choices=CAPACITY_PROFILE_SCOPE_CHOICES, default=CAPACITY_PROFILE_SCOPE_SHARED)
    hard_min = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    soft_min = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    target = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    soft_max = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    hard_max = models.PositiveIntegerField(validators=[MinValueValidator(1)])

    class Meta:
        ordering = ["scope", "name"]

    def clean(self):
        if not (self.hard_min <= self.soft_min <= self.target <= self.soft_max <= self.hard_max):
            raise ValidationError("Capacity values must satisfy hard_min <= soft_min <= target <= soft_max <= hard_max.")

    def __str__(self):
        return self.name


class CoursePriorityProfile(models.Model):
    """Named, administrator-owned demand priority; never inferred from requests."""

    name = models.CharField(max_length=120, unique=True)
    tier = models.PositiveSmallIntegerField(choices=COURSE_PRIORITY_TIER_CHOICES, default=COURSE_PRIORITY_TIER_STANDARD)

    class Meta:
        ordering = ["tier", "name"]

    def __str__(self):
        return self.name


class TeacherPlanningCapacity(models.Model):
    """The planning-only source of a teacher's usable semester section load."""

    teacher = models.ForeignKey("people.Teacher", on_delete=models.CASCADE, related_name="planning_capacities")
    academic_year = models.ForeignKey("common.AcademicYear", on_delete=models.CASCADE)
    semester = models.IntegerField(choices=SEMESTER_CHOICES)
    maximum_sections = models.PositiveIntegerField(validators=[MinValueValidator(0)])
    reserved_sections = models.PositiveIntegerField(default=0, validators=[MinValueValidator(0)])

    class Meta:
        ordering = ["academic_year", "semester", "teacher"]
        constraints = [models.UniqueConstraint(fields=["teacher", "academic_year", "semester"], name="unique_teacher_planning_capacity")]

    def clean(self):
        if self.reserved_sections > self.maximum_sections:
            raise ValidationError({"reserved_sections": "Cannot exceed maximum_sections."})

    @property
    def remaining_sections(self):
        return self.maximum_sections - self.reserved_sections

    def __str__(self):
        return f"{self.teacher} {self.academic_year} S{self.semester}"


class SectionPlanningRun(models.Model):
    """An immutable snapshot and result for one base plan or what-if scenario."""

    academic_year = models.ForeignKey("common.AcademicYear", on_delete=models.PROTECT)
    created_by = models.ForeignKey("auth.User", null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=20, choices=SECTION_PLANNING_RUN_STATUS_CHOICES)
    scenario_constraints = models.JSONField(default=dict, blank=True)
    input_snapshot = models.JSONField(default=dict)
    result = models.JSONField(default=dict)
    solver_metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Section planning runs are immutable.")
        return super().save(*args, **kwargs)


class TimeSlot(models.Model):
    block = models.CharField(max_length=1, choices=SCHEDULE_BLOCK_CHOICES)
    academic_year = models.ForeignKey("common.AcademicYear", on_delete=models.CASCADE)

    semester = models.IntegerField(choices=SEMESTER_CHOICES)

    is_available = models.BooleanField(default=True)

    class Meta:
        ordering = ["academic_year", "semester", "block"]
        constraints = [
            models.UniqueConstraint(
                fields=["academic_year", "semester", "block"],
                name="unique_academic_year_semester_block",
            )
        ]

    def __str__(self):
        return f"{self.academic_year} S{self.semester} Block {self.block}"


class SectionSchedule(models.Model):
    section = models.OneToOneField("courses.Section", on_delete=models.CASCADE)
    timeslot = models.ForeignKey(TimeSlot, on_delete=models.SET_NULL, null=True)

    room = models.ForeignKey("common.Room", on_delete=models.SET_NULL, null=True)
    is_manual = models.BooleanField(default=False)

    class Meta:
        ordering = ["section"]

    def __str__(self):
        return f"Schedule for {self.section}"
