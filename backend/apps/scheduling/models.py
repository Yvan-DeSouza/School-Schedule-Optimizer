"""Persistent planning configuration, audit records, and timetable placement.

Planning configuration is mutable and affects future runs. Planning runs and
approvals are immutable audit facts. ``SectionSchedule`` is operational state
for later timetable placement and remains separate from section-count planning.
"""

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models

from backend.apps.common.constants import (
    BACKUP_POLICY_CHOICES,
    BACKUP_POLICY_IGNORE,
    CAPACITY_PROFILE_SCOPE_CHOICES,
    CAPACITY_PROFILE_SCOPE_SHARED,
    COURSE_PRIORITY_TIER_CHOICES,
    COURSE_PRIORITY_TIER_STANDARD,
    SCHEDULE_BLOCK_CHOICES,
    SECTION_LIFECYCLE_CHOICES,
    SECTION_PLANNING_RUN_STATUS_CHOICES,
    SECTION_RECONCILIATION_ACTION_CHOICES,
    SECTION_BUDGET_TYPE_CHOICES,
    TEACHER_ROSTER_STATUS_CHOICES,
    TEACHER_ROSTER_STATUS_DRAFT,
    SEMESTER_CHOICES,
)
from backend.apps.scheduling.constants import (
    SECTION_PLACEMENT_INPUT_MODE_CHOICES,
    SECTION_PLACEMENT_RUN_STATUS_CHOICES,
    STUDENT_ASSIGNMENT_BASIS_CHOICES,
    STUDENT_ASSIGNMENT_LOCK_TYPE_CHOICES,
    STUDENT_ASSIGNMENT_LOCK_TYPE_COURSE_ROSTER,
    STUDENT_ASSIGNMENT_LOCK_TYPE_EXACT_SECTION,
    STUDENT_ASSIGNMENT_LOCK_TYPE_SECTION_ROSTER,
    STUDENT_ASSIGNMENT_LOCK_TYPE_STUDENT_GROUP,
    STUDENT_ASSIGNMENT_LOCK_TYPE_STUDENT_TEACHER,
    STUDENT_ASSIGNMENT_LOCK_TYPE_WHOLE_SCHEDULE,
    STUDENT_ASSIGNMENT_RUN_STATUS_CHOICES,
    STUDENT_ASSIGNMENT_RUN_SCOPE_CHOICES,
    STUDENT_ASSIGNMENT_RUN_SCOPE_FULL,
    STUDENT_ASSIGNMENT_RUN_SCOPE_SCOPED,
    STUDENT_ASSIGNMENT_STAFFING_MODE_CHOICES,
    TEACHER_ASSIGNMENT_RUN_STATUS_CHOICES,
    TEACHER_TIME_PREFERENCE_CHOICES,
)
from backend.apps.scheduling.domain.capacity import (
    CAPACITY_ORDER_MESSAGE,
    capacity_values,
    validate_capacity_order,
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
        try:
            validate_capacity_order(capacity_values(self))
        except ValueError as error:
            raise ValidationError(CAPACITY_ORDER_MESSAGE) from error

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


class TeacherPlanningAnnualCapacity(models.Model):
    """Year-specific total teaching capacity used by named assignment.

    Semester ceilings alone cannot express a counselor decision such as a
    teacher teaching at most two sections across the entire year.  This row is
    separate from the teacher-directory fallback so every ready roster freezes
    the exact annual workload policy used by downstream runs.
    """

    teacher = models.ForeignKey("people.Teacher", on_delete=models.CASCADE, related_name="planning_annual_capacities")
    academic_year = models.ForeignKey("common.AcademicYear", on_delete=models.CASCADE)
    maximum_sections = models.PositiveIntegerField(validators=[MinValueValidator(0)])
    reserved_sections = models.PositiveIntegerField(default=0, validators=[MinValueValidator(0)])

    class Meta:
        ordering = ["academic_year", "teacher"]
        constraints = [
            models.UniqueConstraint(
                fields=["teacher", "academic_year"],
                name="unique_teacher_planning_annual_capacity",
            )
        ]

    def clean(self):
        if self.reserved_sections > self.maximum_sections:
            raise ValidationError({"reserved_sections": "Cannot exceed maximum_sections."})

    @property
    def remaining_sections(self):
        return self.maximum_sections - self.reserved_sections

    def __str__(self):
        return f"{self.teacher} {self.academic_year} annual"


class TeacherCourseAssignmentRule(models.Model):
    """Counselor-owned annual hard bounds for one teacher/course pairing."""

    academic_year = models.ForeignKey("common.AcademicYear", on_delete=models.CASCADE)
    teacher = models.ForeignKey("people.Teacher", on_delete=models.CASCADE, related_name="course_assignment_rules")
    course = models.ForeignKey("courses.Course", on_delete=models.PROTECT, related_name="teacher_assignment_rules")
    minimum_sections = models.PositiveIntegerField(default=0)
    maximum_sections = models.PositiveIntegerField(null=True, blank=True)
    reason = models.TextField()
    created_by = models.ForeignKey("auth.User", null=True, on_delete=models.SET_NULL, related_name="created_teacher_course_assignment_rules")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_by = models.ForeignKey("auth.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="updated_teacher_course_assignment_rules")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["academic_year", "teacher", "course"]
        constraints = [
            models.UniqueConstraint(
                fields=["academic_year", "teacher", "course"],
                name="unique_teacher_course_assignment_rule",
            )
        ]

    def clean(self):
        if self.maximum_sections is not None and self.minimum_sections > self.maximum_sections:
            raise ValidationError({"maximum_sections": "Cannot be less than minimum_sections."})


class TeacherTimePreference(models.Model):
    """A non-binding counselor/teacher preference for one recurring slot."""

    academic_year = models.ForeignKey("common.AcademicYear", on_delete=models.CASCADE)
    teacher = models.ForeignKey("people.Teacher", on_delete=models.CASCADE, related_name="time_preferences")
    timeslot = models.ForeignKey("scheduling.TimeSlot", on_delete=models.CASCADE)
    preference = models.CharField(max_length=20, choices=TEACHER_TIME_PREFERENCE_CHOICES)
    reason = models.TextField()
    created_by = models.ForeignKey("auth.User", null=True, on_delete=models.SET_NULL, related_name="created_teacher_time_preferences")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_by = models.ForeignKey("auth.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="updated_teacher_time_preferences")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["academic_year", "teacher", "timeslot"]
        constraints = [
            models.UniqueConstraint(
                fields=["academic_year", "teacher", "timeslot"],
                name="unique_teacher_time_preference",
            )
        ]

    def clean(self):
        if self.timeslot_id and self.academic_year_id and self.timeslot.academic_year_id != self.academic_year_id:
            raise ValidationError({"timeslot": "Timeslot must belong to the same academic year."})


class TeacherPlanningRoster(models.Model):
    """Explicit readiness checkpoint for one academic year's staffing records."""

    academic_year = models.OneToOneField(
        "common.AcademicYear",
        on_delete=models.CASCADE,
        related_name="teacher_planning_roster",
    )
    status = models.CharField(
        max_length=20,
        choices=TEACHER_ROSTER_STATUS_CHOICES,
        default=TEACHER_ROSTER_STATUS_DRAFT,
    )
    confirmed_by = models.ForeignKey("auth.User", null=True, on_delete=models.SET_NULL)
    confirmed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["academic_year"]

    def __str__(self):
        return f"{self.academic_year} staffing roster ({self.status})"


class TeacherPlanningRosterMember(models.Model):
    """One teacher intentionally included in a particular year's staff plan."""

    roster = models.ForeignKey(
        TeacherPlanningRoster,
        on_delete=models.CASCADE,
        related_name="members",
    )
    teacher = models.ForeignKey(
        "people.Teacher",
        on_delete=models.PROTECT,
        related_name="planning_roster_memberships",
    )
    added_by = models.ForeignKey("auth.User", null=True, on_delete=models.SET_NULL)
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["teacher__last_name", "teacher__first_name", "teacher_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["roster", "teacher"],
                name="unique_teacher_per_planning_roster",
            )
        ]

    def __str__(self):
        return f"{self.teacher} on {self.roster}"


class SectionBudgetRun(models.Model):
    """Immutable teacher-independent allocation of a physical-section budget."""

    academic_year = models.ForeignKey("common.AcademicYear", on_delete=models.PROTECT)
    created_by = models.ForeignKey("auth.User", null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=20, choices=SECTION_PLANNING_RUN_STATUS_CHOICES)
    budget_type = models.CharField(max_length=20, choices=SECTION_BUDGET_TYPE_CHOICES)
    section_budget = models.PositiveIntegerField()
    backup_policy = models.CharField(
        max_length=30,
        choices=BACKUP_POLICY_CHOICES,
        default=BACKUP_POLICY_IGNORE,
    )
    backup_overrides = models.JSONField(default=list, blank=True)
    scenario_constraints = models.JSONField(default=dict, blank=True)
    input_snapshot = models.JSONField(default=dict)
    result = models.JSONField(default=dict)
    solver_metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Section budget runs are immutable.")
        return super().save(*args, **kwargs)


class SectionBudgetApproval(models.Model):
    """Immutable counselor acceptance of a budget result; creates no sections."""

    budget_run = models.OneToOneField(
        SectionBudgetRun,
        on_delete=models.PROTECT,
        related_name="approval",
    )
    approved_by = models.ForeignKey("auth.User", null=True, on_delete=models.SET_NULL)
    approved_at = models.DateTimeField(auto_now_add=True)
    reason = models.TextField()

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Section budget approvals are immutable.")
        return super().save(*args, **kwargs)


class SectionBudgetApprovalOffering(models.Model):
    """Recommended and accepted physical counts for one delivery group."""

    approval = models.ForeignKey(
        SectionBudgetApproval,
        on_delete=models.PROTECT,
        related_name="offering_approvals",
    )
    delivery_group = models.ForeignKey("courses.DeliveryGroup", on_delete=models.PROTECT)
    recommended_annual_count = models.PositiveIntegerField()
    recommended_semester_1_count = models.PositiveIntegerField()
    recommended_semester_2_count = models.PositiveIntegerField()
    approved_annual_count = models.PositiveIntegerField()
    approved_semester_1_count = models.PositiveIntegerField()
    approved_semester_2_count = models.PositiveIntegerField()

    class Meta:
        ordering = ["delivery_group__name", "delivery_group_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["approval", "delivery_group"],
                name="unique_delivery_group_per_budget_approval",
            )
        ]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Budget offering approvals are immutable.")
        return super().save(*args, **kwargs)


class PlanningRequestResolution(models.Model):
    """Immutable explanation of how cancellation affected one student's demand."""

    approval = models.ForeignKey(
        SectionBudgetApproval,
        on_delete=models.PROTECT,
        related_name="request_resolutions",
    )
    student = models.ForeignKey("people.Student", on_delete=models.PROTECT)
    cancelled_course_ids = models.JSONField(default=list)
    backup_request = models.ForeignKey(
        "courses.CourseRequest",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
    )
    outcome = models.CharField(max_length=40)
    unresolved_course_count = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["student_id", "id"]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Planning request resolutions are immutable.")
        return super().save(*args, **kwargs)


class StaffingPlanRun(models.Model):
    """Immutable staffing-aware plan over physical delivery groups.

    A run may stand alone or refine an approved teacher-independent budget.
    Linking the approval, rather than copying only its total, lets reviewers
    explain every reallocation from the earlier planning decision.
    """

    academic_year = models.ForeignKey("common.AcademicYear", on_delete=models.PROTECT)
    budget_approval = models.ForeignKey(
        SectionBudgetApproval,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="staffing_runs",
    )
    teacher_roster = models.ForeignKey(
        TeacherPlanningRoster,
        on_delete=models.PROTECT,
        related_name="staffing_runs",
    )
    created_by = models.ForeignKey("auth.User", null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=20, choices=SECTION_PLANNING_RUN_STATUS_CHOICES)
    backup_policy = models.CharField(
        max_length=30,
        choices=BACKUP_POLICY_CHOICES,
        default=BACKUP_POLICY_IGNORE,
    )
    backup_overrides = models.JSONField(default=list, blank=True)
    scenario_constraints = models.JSONField(default=dict, blank=True)
    input_snapshot = models.JSONField(default=dict)
    result = models.JSONField(default=dict)
    solver_metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Staffing plan runs are immutable.")
        return super().save(*args, **kwargs)


class StaffingPlanApproval(models.Model):
    """One counselor-approved staffing plan that may create draft sections."""

    staffing_run = models.OneToOneField(
        StaffingPlanRun,
        on_delete=models.PROTECT,
        related_name="approval",
    )
    approved_by = models.ForeignKey("auth.User", null=True, on_delete=models.SET_NULL)
    approved_at = models.DateTimeField(auto_now_add=True)
    reason = models.TextField()

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Staffing plan approvals are immutable.")
        return super().save(*args, **kwargs)


class StaffingPlanApprovalOffering(models.Model):
    """Recommended and approved semester counts for one physical class group."""

    approval = models.ForeignKey(
        StaffingPlanApproval,
        on_delete=models.PROTECT,
        related_name="offering_approvals",
    )
    delivery_group = models.ForeignKey("courses.DeliveryGroup", on_delete=models.PROTECT)
    recommended_semester_1_count = models.PositiveIntegerField()
    recommended_semester_2_count = models.PositiveIntegerField()
    approved_semester_1_count = models.PositiveIntegerField()
    approved_semester_2_count = models.PositiveIntegerField()

    class Meta:
        ordering = ["delivery_group__name", "delivery_group_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["approval", "delivery_group"],
                name="unique_delivery_group_per_staffing_approval",
            )
        ]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Staffing offering approvals are immutable.")
        return super().save(*args, **kwargs)


class StaffingRequestResolution(models.Model):
    """Frozen backup/cancellation outcome used by a staffing-aware plan."""

    staffing_run = models.ForeignKey(
        StaffingPlanRun,
        on_delete=models.PROTECT,
        related_name="request_resolutions",
    )
    student = models.ForeignKey("people.Student", on_delete=models.PROTECT)
    cancelled_course_ids = models.JSONField(default=list)
    backup_request = models.ForeignKey(
        "courses.CourseRequest",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
    )
    outcome = models.CharField(max_length=40)
    unresolved_course_count = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["student_id", "id"]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Staffing request resolutions are immutable.")
        return super().save(*args, **kwargs)


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
    # Timing placement is deliberately earlier than room assignment. The
    # provenance row proves which immutable recommendation supplied a partial
    # timeslot-only schedule without pretending a room was selected.
    placement_approval_assignment = models.OneToOneField(
        "SectionPlacementApprovalAssignment",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="scheduled_section",
    )

    class Meta:
        ordering = ["section"]

    def __str__(self):
        return f"Schedule for {self.section}"


class AnnualPlacementLock(models.Model):
    """A pre-section timeslot lock for one annual physical delivery slot."""

    academic_year = models.ForeignKey("common.AcademicYear", on_delete=models.CASCADE)
    delivery_group = models.ForeignKey("courses.DeliveryGroup", on_delete=models.CASCADE)
    annual_index = models.PositiveIntegerField()
    locked_timeslot = models.ForeignKey(TimeSlot, on_delete=models.PROTECT)
    reason = models.TextField()
    created_by = models.ForeignKey("auth.User", null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_by = models.ForeignKey(
        "auth.User", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="updated_annual_placement_locks",
    )
    updated_at = models.DateTimeField(auto_now=True)
    materialized_section = models.OneToOneField(
        "courses.Section", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="annual_placement_lock",
    )

    class Meta:
        ordering = ["academic_year", "delivery_group", "annual_index"]
        constraints = [
            models.UniqueConstraint(
                fields=["academic_year", "delivery_group", "annual_index"],
                name="unique_annual_placement_lock_slot",
            ),
        ]

    def __str__(self):
        return f"{self.delivery_group} annual slot {self.annual_index}"


class SectionPlacementRun(models.Model):
    """Immutable semester/A-D recommendation with a hidden staffing witness."""

    academic_year = models.ForeignKey("common.AcademicYear", on_delete=models.PROTECT)
    input_mode = models.CharField(max_length=30, choices=SECTION_PLACEMENT_INPUT_MODE_CHOICES)
    budget_approval = models.ForeignKey(
        SectionBudgetApproval, null=True, blank=True, on_delete=models.PROTECT,
        related_name="placement_runs",
    )
    conflict_matrix = models.ForeignKey(
        "constraints.CourseConflictMatrix", on_delete=models.PROTECT,
        related_name="placement_runs",
    )
    teacher_roster = models.ForeignKey(
        TeacherPlanningRoster, on_delete=models.PROTECT, related_name="placement_runs",
    )
    created_by = models.ForeignKey("auth.User", null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=20, choices=SECTION_PLACEMENT_RUN_STATUS_CHOICES)
    input_snapshot = models.JSONField(default=dict)
    result = models.JSONField(default=dict)
    solver_metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Section placement runs are immutable.")
        return super().save(*args, **kwargs)


class SectionPlacementApproval(models.Model):
    """One immutable approval for a complete, unchanged placement candidate."""

    placement_run = models.OneToOneField(
        SectionPlacementRun, on_delete=models.PROTECT, related_name="approval",
    )
    approved_by = models.ForeignKey("auth.User", null=True, on_delete=models.SET_NULL)
    approved_at = models.DateTimeField(auto_now_add=True)
    reason = models.TextField()

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Section placement approvals are immutable.")
        return super().save(*args, **kwargs)


class SectionPlacementApprovalAssignment(models.Model):
    """Immutable per-section timing fact approved from a placement run."""

    approval = models.ForeignKey(
        SectionPlacementApproval, on_delete=models.PROTECT, related_name="assignments",
    )
    section = models.OneToOneField(
        "courses.Section", on_delete=models.PROTECT, related_name="placement_assignment",
    )
    timeslot = models.ForeignKey(TimeSlot, on_delete=models.PROTECT)
    annual_index = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        ordering = ["approval", "section"]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Section placement assignments are immutable.")
        return super().save(*args, **kwargs)


class TeacherAssignmentRun(models.Model):
    """Immutable named-teacher recommendation over accepted timing context."""

    academic_year = models.ForeignKey("common.AcademicYear", on_delete=models.PROTECT)
    teacher_roster = models.ForeignKey(
        TeacherPlanningRoster, on_delete=models.PROTECT, related_name="teacher_assignment_runs",
    )
    created_by = models.ForeignKey("auth.User", null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=20, choices=TEACHER_ASSIGNMENT_RUN_STATUS_CHOICES)
    input_snapshot = models.JSONField(default=dict)
    result = models.JSONField(default=dict)
    solver_metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Teacher assignment runs are immutable.")
        return super().save(*args, **kwargs)


class TeacherAssignmentApproval(models.Model):
    """One immutable counselor decision accepting a complete teacher run."""

    teacher_assignment_run = models.OneToOneField(
        TeacherAssignmentRun, on_delete=models.PROTECT, related_name="approval",
    )
    approved_by = models.ForeignKey("auth.User", null=True, on_delete=models.SET_NULL)
    approved_at = models.DateTimeField(auto_now_add=True)
    reason = models.TextField()

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Teacher assignment approvals are immutable.")
        return super().save(*args, **kwargs)


class TeacherAssignmentApprovalAssignment(models.Model):
    """Immutable provenance for one named assignment written to Section.teacher."""

    approval = models.ForeignKey(
        TeacherAssignmentApproval, on_delete=models.PROTECT, related_name="assignments",
    )
    section = models.OneToOneField(
        "courses.Section", on_delete=models.PROTECT, related_name="teacher_assignment",
    )
    teacher = models.ForeignKey("people.Teacher", on_delete=models.PROTECT)

    class Meta:
        ordering = ["approval", "section"]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Teacher assignment approval lines are immutable.")
        return super().save(*args, **kwargs)


class StudentAssignmentRun(models.Model):
    """Immutable student-to-section recommendation over accepted section context."""

    academic_year = models.ForeignKey("common.AcademicYear", on_delete=models.PROTECT)
    staffing_mode = models.CharField(
        max_length=30,
        choices=STUDENT_ASSIGNMENT_STAFFING_MODE_CHOICES,
    )
    provisional_teacher_assignment_run = models.ForeignKey(
        TeacherAssignmentRun,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="provisional_student_assignment_runs",
    )
    # A scoped rerun is based on an accepted student-assignment approval, not
    # on a mutable current table state. The exact resolved scope remains in the
    # JSON snapshot; these fields make run history queryable without replacing
    # that canonical snapshot.
    source_approval = models.ForeignKey(
        "StudentAssignmentApproval",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="scoped_student_assignment_runs",
    )
    scope_type = models.CharField(
        max_length=20,
        choices=STUDENT_ASSIGNMENT_RUN_SCOPE_CHOICES,
        default=STUDENT_ASSIGNMENT_RUN_SCOPE_FULL,
    )
    created_by = models.ForeignKey("auth.User", null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=20, choices=STUDENT_ASSIGNMENT_RUN_STATUS_CHOICES)
    input_snapshot = models.JSONField(default=dict)
    result = models.JSONField(default=dict)
    solver_metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def clean(self):
        super().clean()
        errors = {}
        if self.scope_type == STUDENT_ASSIGNMENT_RUN_SCOPE_FULL and self.source_approval_id:
            errors["source_approval"] = "A full student-assignment run cannot have a source approval."
        if self.scope_type == STUDENT_ASSIGNMENT_RUN_SCOPE_SCOPED and not self.source_approval_id:
            errors["source_approval"] = "A scoped student-assignment run requires a source approval."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Student assignment runs are immutable.")
        return super().save(*args, **kwargs)


class StudentAssignmentApproval(models.Model):
    """One immutable approval accepting an unchanged complete student run."""

    student_assignment_run = models.OneToOneField(
        StudentAssignmentRun,
        on_delete=models.PROTECT,
        related_name="approval",
    )
    approved_by = models.ForeignKey("auth.User", null=True, on_delete=models.SET_NULL)
    approved_at = models.DateTimeField(auto_now_add=True)
    reason = models.TextField()

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Student assignment approvals are immutable.")
        return super().save(*args, **kwargs)


class StudentAssignmentApprovalEnrollment(models.Model):
    """Immutable provenance for one enrollment written by student approval."""

    approval = models.ForeignKey(
        StudentAssignmentApproval,
        on_delete=models.PROTECT,
        related_name="enrollment_provenance",
    )
    enrollment = models.OneToOneField(
        "courses.Enrollment",
        on_delete=models.PROTECT,
        related_name="student_assignment_provenance",
    )
    course_request = models.ForeignKey("courses.CourseRequest", on_delete=models.PROTECT)
    assignment_basis = models.CharField(max_length=30, choices=STUDENT_ASSIGNMENT_BASIS_CHOICES)
    backup_resolution_snapshot = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["approval", "enrollment"]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Student assignment enrollment provenance is immutable.")
        return super().save(*args, **kwargs)


class StudentAssignmentLock(models.Model):
    """One counselor decision that protects a student-assignment fact."""

    academic_year = models.ForeignKey("common.AcademicYear", on_delete=models.PROTECT)
    lock_type = models.CharField(max_length=40, choices=STUDENT_ASSIGNMENT_LOCK_TYPE_CHOICES)

    # These nullable targets share one table because each lock type protects a
    # different level of the same workflow. ``clean`` enforces the exact target
    # shape; nullable columns are not permission to create an ambiguous lock.
    student = models.ForeignKey(
        "people.Student",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="student_assignment_locks",
    )
    section = models.ForeignKey(
        "courses.Section",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="student_assignment_locks",
    )
    course = models.ForeignKey(
        "courses.Course",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="student_assignment_locks",
    )
    teacher = models.ForeignKey(
        "people.Teacher",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="student_assignment_locks",
    )

    reason = models.TextField()
    created_by = models.ForeignKey(
        "auth.User",
        on_delete=models.PROTECT,
        related_name="created_student_assignment_locks",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    # Release is a one-way state transition. Keeping release facts on the lock
    # makes the current state queryable while preserving who ended the
    # decision and why; target and creation facts cannot be rewritten.
    is_active = models.BooleanField(default=True)
    released_at = models.DateTimeField(null=True, blank=True)
    released_by = models.ForeignKey(
        "auth.User",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="released_student_assignment_locks",
    )
    release_reason = models.TextField(null=True, blank=True)

    class Meta:
        ordering = ["academic_year", "created_at", "id"]
        indexes = [
            models.Index(fields=["academic_year", "is_active", "lock_type"]),
        ]

    @property
    def is_released(self):
        """Expose the domain state in prose without storing a second flag."""

        return not self.is_active

    def clean(self):
        super().clean()
        errors = {}
        if not isinstance(self.reason, str) or not self.reason.strip():
            errors["reason"] = "A non-blank reason is required for a student-assignment lock."
        if not self.created_by_id:
            errors["created_by"] = "A creator is required for a student-assignment lock."

        expected_targets = {
            STUDENT_ASSIGNMENT_LOCK_TYPE_EXACT_SECTION: {"student", "section", "course"},
            STUDENT_ASSIGNMENT_LOCK_TYPE_WHOLE_SCHEDULE: {"student"},
            STUDENT_ASSIGNMENT_LOCK_TYPE_SECTION_ROSTER: {"section"},
            STUDENT_ASSIGNMENT_LOCK_TYPE_COURSE_ROSTER: {"course"},
            STUDENT_ASSIGNMENT_LOCK_TYPE_STUDENT_GROUP: {"course"},
            STUDENT_ASSIGNMENT_LOCK_TYPE_STUDENT_TEACHER: {"student", "course", "teacher"},
        }
        required_targets = expected_targets.get(self.lock_type)
        if required_targets is None:
            errors["lock_type"] = "Choose a recognized student-assignment lock type."
        else:
            target_ids = {
                field_name
                for field_name in ("student", "section", "course", "teacher")
                if getattr(self, f"{field_name}_id") is not None
            }
            missing = required_targets - target_ids
            unexpected = target_ids - required_targets
            for field_name in sorted(missing):
                errors[field_name] = "This target is required for the selected lock type."
            for field_name in sorted(unexpected):
                errors[field_name] = "This target is not used by the selected lock type."

        if self.is_active:
            if self.released_at is not None or self.released_by_id or self.release_reason:
                errors["is_active"] = "An active lock cannot contain release facts."
        else:
            if self.released_at is None:
                errors["released_at"] = "A released lock must record when it was released."
            if not self.released_by_id:
                errors["released_by"] = "A released lock must record the releasing actor."
            if not isinstance(self.release_reason, str) or not self.release_reason.strip():
                errors["release_reason"] = "A non-blank release reason is required."

        if self.student_id and self.student.academic_year_id != self.academic_year_id:
            errors["student"] = "The student must belong to the lock's academic year."
        if self.section_id:
            if self.section.academic_year_id != self.academic_year_id:
                errors["section"] = "The section must belong to the lock's academic year."
            elif self.section.lifecycle_status != "active":
                errors["section"] = "Retired sections cannot receive student-assignment locks."
        if self.section_id and self.course_id:
            belongs_to_section = (
                self.section.delivery_group.offerings.filter(course_id=self.course_id).exists()
                if self.section.delivery_group_id
                else self.section.course_id == self.course_id
            )
            if not belongs_to_section:
                errors["course"] = "The course must be offered by the selected section."

        # A group lock is created with its members by the service. This check
        # also protects later direct model validation from accepting an empty
        # group after the lock row already exists.
        if self.pk and self.lock_type == STUDENT_ASSIGNMENT_LOCK_TYPE_STUDENT_GROUP and not self.members.exists():
            errors["members"] = "A student-group lock must contain at least two students."

        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if self.pk:
            previous = type(self).objects.get(pk=self.pk)
            immutable_fields = (
                "academic_year_id", "lock_type", "student_id", "section_id",
                "course_id", "teacher_id", "reason", "created_by_id", "created_at",
            )
            if not previous.is_active:
                raise ValidationError("Released student-assignment locks are immutable.")
            if any(getattr(previous, field) != getattr(self, field) for field in immutable_fields):
                raise ValidationError("Student-assignment lock decisions are append-only; release instead.")
            if self.is_active:
                raise ValidationError("An active student-assignment lock can only be released.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Student-assignment locks are append-only; release instead of deleting.")


class StudentAssignmentLockMember(models.Model):
    """One student included in a same-section group lock."""

    student_assignment_lock = models.ForeignKey(
        StudentAssignmentLock,
        on_delete=models.PROTECT,
        related_name="members",
    )
    student = models.ForeignKey(
        "people.Student",
        on_delete=models.PROTECT,
        related_name="student_assignment_lock_memberships",
    )

    class Meta:
        ordering = ["student_assignment_lock", "student"]
        constraints = [
            models.UniqueConstraint(
                fields=["student_assignment_lock", "student"],
                name="unique_student_assignment_lock_member",
            ),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if self.student_assignment_lock.lock_type != STUDENT_ASSIGNMENT_LOCK_TYPE_STUDENT_GROUP:
            errors["student_assignment_lock"] = "Only group locks may have student members."
        if not self.student_assignment_lock.is_active:
            errors["student_assignment_lock"] = "Released group locks cannot gain members."
        if self.student.academic_year_id != self.student_assignment_lock.academic_year_id:
            errors["student"] = "The member must belong to the lock's academic year."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Student-assignment lock membership is immutable.")
        return super().save(*args, **kwargs)
