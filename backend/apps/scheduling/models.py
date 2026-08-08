"""Persistent planning configuration, audit records, and timetable placement.

Planning configuration is mutable and affects future runs. Planning runs and
approvals are immutable audit facts. ``SectionSchedule`` is operational state
for later timetable placement and remains separate from section-count planning.
"""

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models

from backend.apps.common.constants import (
    CAPACITY_PROFILE_SCOPE_CHOICES,
    CAPACITY_PROFILE_SCOPE_SHARED,
    COURSE_PRIORITY_TIER_CHOICES,
    COURSE_PRIORITY_TIER_STANDARD,
    SCHEDULE_BLOCK_CHOICES,
    SECTION_LIFECYCLE_CHOICES,
    SECTION_PLANNING_RUN_STATUS_CHOICES,
    SECTION_RECONCILIATION_ACTION_CHOICES,
    SEMESTER_CHOICES,
)


class CapacityProfile(models.Model):
    """Reusable class-size policy used by future section-planning runs."""

    # Shared profiles may be reused by many courses. Course-specific profiles
    # support copy-on-write customization without changing other courses.
    name = models.CharField(max_length=120, unique=True)
    scope = models.CharField(max_length=20, choices=CAPACITY_PROFILE_SCOPE_CHOICES, default=CAPACITY_PROFILE_SCOPE_SHARED)
    # Hard values define candidate legality, soft values define preferences, and
    # target is the ideal class size used after demand priorities are protected.
    hard_min = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    soft_min = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    target = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    soft_max = models.PositiveIntegerField(validators=[MinValueValidator(1)])
    hard_max = models.PositiveIntegerField(validators=[MinValueValidator(1)])

    class Meta:
        ordering = ["scope", "name"]

    def clean(self):
        # Positivity alone is insufficient; the complete five-point policy must
        # be monotonically ordered.
        if not (self.hard_min <= self.soft_min <= self.target <= self.soft_max <= self.hard_max):
            raise ValidationError("Capacity values must satisfy hard_min <= soft_min <= target <= soft_max <= hard_max.")

    def __str__(self):
        return self.name


class CoursePriorityProfile(models.Model):
    """Named, administrator-owned demand priority; never inferred from requests."""

    # Named profiles make prioritization visible and prevent hidden inference
    # from grade, category, code, or individual mandatory-request flags.
    name = models.CharField(max_length=120, unique=True)
    tier = models.PositiveSmallIntegerField(choices=COURSE_PRIORITY_TIER_CHOICES, default=COURSE_PRIORITY_TIER_STANDARD)

    class Meta:
        ordering = ["tier", "name"]

    def __str__(self):
        return self.name


class TeacherPlanningCapacity(models.Model):
    """The planning-only source of a teacher's usable semester section load."""

    # Capacity is scoped per year/semester because leave, release time, and other
    # staffing conditions can change between terms and planning cycles.
    teacher = models.ForeignKey("people.Teacher", on_delete=models.CASCADE, related_name="planning_capacities")
    academic_year = models.ForeignKey("common.AcademicYear", on_delete=models.CASCADE)
    semester = models.IntegerField(choices=SEMESTER_CHOICES)
    # Maximum is a ceiling, never a utilization target. Reserved load is removed
    # before the engine receives remaining capacity.
    maximum_sections = models.PositiveIntegerField(validators=[MinValueValidator(0)])
    reserved_sections = models.PositiveIntegerField(default=0, validators=[MinValueValidator(0)])

    class Meta:
        ordering = ["academic_year", "semester", "teacher"]
        constraints = [models.UniqueConstraint(fields=["teacher", "academic_year", "semester"], name="unique_teacher_planning_capacity")]

    def clean(self):
        # Reject negative effective capacity instead of relying on the engine to
        # clamp it and hide a configuration mistake.
        if self.reserved_sections > self.maximum_sections:
            raise ValidationError({"reserved_sections": "Cannot exceed maximum_sections."})

    @property
    def remaining_sections(self):
        return self.maximum_sections - self.reserved_sections

    def __str__(self):
        return f"{self.teacher} {self.academic_year} S{self.semester}"


class SectionPlanningRun(models.Model):
    """An immutable snapshot and result for one base plan or what-if scenario."""

    # PROTECT retains the year identity required to interpret an audit record.
    academic_year = models.ForeignKey("common.AcademicYear", on_delete=models.PROTECT)
    # User deletion must not erase the planning fact; null means the account no
    # longer exists while the snapshot/result remain intact.
    created_by = models.ForeignKey("auth.User", null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=20, choices=SECTION_PLANNING_RUN_STATUS_CHOICES)
    # Concise counselor overrides, exact expanded engine input, and solver output
    # answer different audit questions and are stored separately.
    scenario_constraints = models.JSONField(default=dict, blank=True)
    input_snapshot = models.JSONField(default=dict)
    result = models.JSONField(default=dict)
    solver_metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def save(self, *args, **kwargs):
        # Runs are append-only. A changed assumption must produce a new run rather
        # than rewriting the historical explanation.
        if self.pk:
            raise ValidationError("Section planning runs are immutable.")
        return super().save(*args, **kwargs)


class SectionPlanningApproval(models.Model):
    """One immutable planning-role decision approving all or part of a run."""

    # One run may be approved in multiple disjoint batches. Per-course records
    # below describe exactly what each batch covered.
    planning_run = models.ForeignKey(
        SectionPlanningRun,
        on_delete=models.PROTECT,
        related_name="approvals",
    )
    approved_by = models.ForeignKey("auth.User", null=True, on_delete=models.SET_NULL)
    approved_at = models.DateTimeField(auto_now_add=True)
    reason = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["approved_at", "id"]

    def save(self, *args, **kwargs):
        # Corrections belong in a later explicit workflow; never rewrite who
        # approved an existing decision or the supplied reason.
        if self.pk:
            raise ValidationError("Section planning approvals are immutable.")
        return super().save(*args, **kwargs)


class SectionPlanningApprovalCourse(models.Model):
    """The exact semester counts approved for one course in an approval."""

    approval = models.ForeignKey(
        SectionPlanningApproval,
        on_delete=models.PROTECT,
        related_name="course_approvals",
    )
    course = models.ForeignKey("courses.Course", on_delete=models.PROTECT)
    # Store recommendation and decision even when equal. This proves whether a
    # planning-role user accepted or adjusted the solver result.
    recommended_semester_1_count = models.PositiveIntegerField()
    recommended_semester_2_count = models.PositiveIntegerField()
    approved_semester_1_count = models.PositiveIntegerField()
    approved_semester_2_count = models.PositiveIntegerField()

    class Meta:
        ordering = ["course__course_code", "course_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["approval", "course"],
                name="unique_course_per_section_planning_approval",
            )
        ]

    def save(self, *args, **kwargs):
        # Generated sections refer back to this normalized immutable audit line.
        if self.pk:
            raise ValidationError("Approved course counts are immutable.")
        return super().save(*args, **kwargs)


class SectionPlanningReconciliation(models.Model):
    """Immutable header for applying a newer approval to existing sections."""

    # The approval remains the source of actor, reason, timestamp, run, and
    # accepted course counts. Reconciliation adds the concrete section delta.
    approval = models.OneToOneField(
        SectionPlanningApproval,
        on_delete=models.PROTECT,
        related_name="reconciliation",
    )
    preview_token = models.CharField(max_length=64)
    previous_active_section_count = models.PositiveIntegerField()
    final_active_section_count = models.PositiveIntegerField()

    class Meta:
        ordering = ["-approval__approved_at", "-id"]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Section planning reconciliations are immutable.")
        return super().save(*args, **kwargs)


class SectionPlanningReconciliationCourse(models.Model):
    """Before/after semester totals for one reconciled approval course."""

    reconciliation = models.ForeignKey(
        SectionPlanningReconciliation,
        on_delete=models.PROTECT,
        related_name="course_reconciliations",
    )
    approval_course = models.OneToOneField(
        SectionPlanningApprovalCourse,
        on_delete=models.PROTECT,
        related_name="reconciliation_course",
    )
    previous_semester_1_count = models.PositiveIntegerField()
    previous_semester_2_count = models.PositiveIntegerField()
    final_semester_1_count = models.PositiveIntegerField()
    final_semester_2_count = models.PositiveIntegerField()

    class Meta:
        ordering = ["approval_course__course__course_code", "approval_course__course_id"]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Section planning course reconciliations are immutable.")
        return super().save(*args, **kwargs)


class SectionPlanningReconciliationAction(models.Model):
    """Immutable before/after record for one section in a reconciliation."""

    course_reconciliation = models.ForeignKey(
        SectionPlanningReconciliationCourse,
        on_delete=models.PROTECT,
        related_name="actions",
    )
    # PROTECT ensures an audited section cannot later disappear from history.
    section = models.ForeignKey(
        "courses.Section",
        on_delete=models.PROTECT,
        related_name="planning_reconciliation_actions",
    )
    action = models.CharField(max_length=20, choices=SECTION_RECONCILIATION_ACTION_CHOICES)
    previous_lifecycle_status = models.CharField(
        max_length=20,
        choices=SECTION_LIFECYCLE_CHOICES,
        blank=True,
        default="",
    )
    new_lifecycle_status = models.CharField(
        max_length=20,
        choices=SECTION_LIFECYCLE_CHOICES,
        blank=True,
        default="",
    )
    previous_semester = models.IntegerField(choices=SEMESTER_CHOICES, null=True, blank=True)
    new_semester = models.IntegerField(choices=SEMESTER_CHOICES, null=True, blank=True)
    previous_section_number = models.CharField(max_length=10, blank=True, default="")
    new_section_number = models.CharField(max_length=10, blank=True, default="")
    previous_capacity_min = models.PositiveIntegerField(null=True, blank=True)
    previous_capacity_max = models.PositiveIntegerField(null=True, blank=True)
    new_capacity_min = models.PositiveIntegerField(null=True, blank=True)
    new_capacity_max = models.PositiveIntegerField(null=True, blank=True)
    protection_reasons = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["course_reconciliation", "section_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["course_reconciliation", "section"],
                name="unique_section_per_planning_reconciliation_course",
            )
        ]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Section planning reconciliation actions are immutable.")
        return super().save(*args, **kwargs)


class TimeSlot(models.Model):
    """One recurring A-D timetable block in one semester/year."""

    # Rotation details live in shared constants and are exposed by the serializer
    # rather than duplicated on every row.
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
    """Operational room/timeslot placement for an existing section."""

    # OneToOne permits at most one current placement. Nullable room/slot values
    # allow draft and partially scheduled sections to exist safely.
    section = models.OneToOneField("courses.Section", on_delete=models.CASCADE)
    timeslot = models.ForeignKey(TimeSlot, on_delete=models.SET_NULL, null=True)

    room = models.ForeignKey("common.Room", on_delete=models.SET_NULL, null=True)
    # This distinguishes counselor placement from future solver output.
    is_manual = models.BooleanField(default=False)

    class Meta:
        ordering = ["section"]

    def __str__(self):
        return f"Schedule for {self.section}"
