"""DRF validation and representations for planning configuration and runs.

Serializers validate transport shape and current database relationships. Domain
orchestration—including transactional approval conflict checks—stays in service
modules so non-HTTP callers receive the same behavior.
"""

from rest_framework import serializers

from backend.apps.common.constants import (
    BACKUP_POLICY_CHOICES,
    BLOCK_ROTATION,
    CAPACITY_PROFILE_SCOPE_SHARED,
    SECTION_BUDGET_TYPE_CHOICES,
)
from backend.apps.scheduling.constants import (
    SOFT_CONSTRAINT_IMPORTANCE_CHOICES,
    STUDENT_ASSIGNMENT_DEFAULT_MAX_PRIORITY_REQUESTS,
    STUDENT_ASSIGNMENT_LOCK_TYPE_CHOICES,
    STUDENT_ASSIGNMENT_RUN_SCOPE_CHOICES,
    STUDENT_ASSIGNMENT_SCHEDULE_PRESERVATION_CHOICES,
    STUDENT_ASSIGNMENT_STAFFING_MODE_PROVISIONAL_STAFFING,
    STUDENT_ASSIGNMENT_STAFFING_MODE_CHOICES,
    CO_OP_BLOCK_PAIR_CHOICES,
    STUDENT_SPECIAL_COMMITMENT_LOCK_MODE_CHOICES,
    STUDENT_SPECIAL_COMMITMENT_LOCK_TYPE_CHOICES,
)
from backend.apps.scheduling.models import (
    SchedulingExecution,
    CapacityProfile,
    CoursePriorityProfile,
    SectionPlanningApproval,
    SectionPlanningApprovalCourse,
    SectionPlanningReconciliation,
    SectionPlanningReconciliationAction,
    SectionPlanningReconciliationCourse,
    SectionPlanningRun,
    PlanningRequestResolution,
    SectionBudgetApproval,
    SectionBudgetApprovalOffering,
    SectionBudgetRun,
    StaffingPlanApproval,
    StaffingPlanApprovalOffering,
    StaffingPlanRun,
    StaffingRequestResolution,
    TeacherPlanningCapacity,
    TeacherPlanningAnnualCapacity,
    TeacherCourseAssignmentRule,
    TeacherTimePreference,
    TeacherPlanningRoster,
    TimeSlot,
    AnnualPlacementLock,
    SectionPlacementApproval,
    SectionPlacementApprovalAssignment,
    SectionPlacementRun,
    TeacherAssignmentRun,
    TeacherAssignmentApproval,
    TeacherAssignmentApprovalAssignment,
    TeacherAssignmentApprovalOnlineSupervision,
    StudentAssignmentRun,
    StudentAssignmentApproval,
    StudentAssignmentApprovalEnrollment,
    StudentAssignmentApprovalOnlineEnrollment,
    StudentAssignmentApprovalCommitment,
    StudentAssignmentLock,
    StudentSpecialCommitmentLock,
    OnlineSupervisionConfiguration,
    OnlineSupervisionPlanRun,
    OnlineSupervisionPlanApproval,
    OnlineSupervisionPlanApprovalSession,
    OnlineSupervisionSession,
)
from backend.apps.scheduling.domain.capacity import (
    CAPACITY_FIELDS,
    CAPACITY_ORDER_MESSAGE,
    capacity_values,
    validate_capacity_order,
)


class TimeSlotSerializer(serializers.ModelSerializer):
    """Expose a stored A-D block plus its fixed recurring rotation."""

    # Rotation is derived from canonical constants and cannot drift per row.
    rotation = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = TimeSlot
        fields = ("id", "academic_year", "semester", "block", "is_available", "rotation")
        validators = []

    def get_rotation(self, instance):
        return [
            {"rotation_day": rotation_day, "period": period}
            for rotation_day, period in BLOCK_ROTATION[instance.block]
        ]

    def validate(self, attrs):
        # Meta validators are disabled so partial updates can receive one clear,
        # controlled duplicate message using the merged instance/input values.
        attrs = super().validate(attrs)
        academic_year = attrs.get("academic_year", getattr(self.instance, "academic_year", None))
        semester = attrs.get("semester", getattr(self.instance, "semester", None))
        block = attrs.get("block", getattr(self.instance, "block", None))
        if academic_year and semester and block:
            # Exclude the current instance during update; otherwise every PATCH
            # would appear to collide with itself.
            duplicate = TimeSlot.objects.filter(
                academic_year=academic_year,
                semester=semester,
                block=block,
            )
            if self.instance:
                duplicate = duplicate.exclude(pk=self.instance.pk)
            if duplicate.exists():
                raise serializers.ValidationError(
                    "A timeslot for this academic year, semester, and block already exists."
                )
        return attrs


class OnlineSupervisionConfigurationSerializer(serializers.ModelSerializer):
    """Year-specific capacity policy for shared, non-instructional supervision."""

    class Meta:
        model = OnlineSupervisionConfiguration
        fields = ("id", "academic_year", "capacity_profile", "updated_by", "updated_at")
        read_only_fields = ("updated_by", "updated_at")

    def validate(self, attrs):
        attrs = super().validate(attrs)
        profile = attrs.get("capacity_profile", getattr(self.instance, "capacity_profile", None))
        if profile and profile.target > profile.hard_max:
            raise serializers.ValidationError({
                "capacity_profile": "The supervision target cannot exceed the hard maximum."
            })
        return attrs


class OnlineSupervisionPlanRunCreateSerializer(serializers.Serializer):
    """Only the target year is configurable; the service derives demand facts."""

    academic_year = serializers.IntegerField(min_value=1)


class OnlineSupervisionPlanApprovalRequestSerializer(serializers.Serializer):
    reason = serializers.CharField(required=True, allow_blank=False, max_length=2000)

    def validate_reason(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("An approval reason is required.")
        return value


class OnlineSupervisionPlanApprovalSessionSerializer(serializers.ModelSerializer):
    class Meta:
        model = OnlineSupervisionPlanApprovalSession
        fields = ("id", "annual_index", "allowed_semesters", "capacity_max", "target_capacity")
        read_only_fields = fields


class SchedulingExecutionSerializer(serializers.ModelSerializer):
    """Stable asynchronous delivery status plus the eventual immutable run."""

    solver_status = serializers.SerializerMethodField()

    class Meta:
        model = SchedulingExecution
        fields = (
            "id", "operation", "status", "created_by", "created_at", "started_at",
            "finished_at", "celery_task_id", "result_model", "result_id",
            "solver_status", "error_code", "error_detail",
        )
        read_only_fields = fields

    def get_solver_status(self, obj):
        from backend.apps.scheduling.services.execution import execution_result_status

        return execution_result_status(obj)


class OnlineSupervisionPlanApprovalSerializer(serializers.ModelSerializer):
    session_approvals = OnlineSupervisionPlanApprovalSessionSerializer(many=True, read_only=True)

    class Meta:
        model = OnlineSupervisionPlanApproval
        fields = ("id", "plan_run", "approved_by", "approved_at", "reason", "session_approvals")
        read_only_fields = fields


class OnlineSupervisionPlanRunSerializer(serializers.ModelSerializer):
    approval = OnlineSupervisionPlanApprovalSerializer(read_only=True)

    class Meta:
        model = OnlineSupervisionPlanRun
        fields = (
            "id", "academic_year", "configuration", "created_by", "created_at", "status",
            "input_snapshot", "result", "solver_metadata", "approval",
        )
        read_only_fields = fields


class OnlineSupervisionSessionSerializer(serializers.ModelSerializer):
    """Read-only operational resource shown separately from instructional sections."""

    class Meta:
        model = OnlineSupervisionSession
        fields = (
            "id", "academic_year", "session_number", "timeslot", "capacity_max", "target_capacity",
            "supervisor", "lifecycle_status", "plan_approval_session", "placement_approval",
        )
        read_only_fields = fields


class AnnualPlacementLockSerializer(serializers.ModelSerializer):
    """Transport validation for annual virtual-slot locks; service owns policy."""

    class Meta:
        model = AnnualPlacementLock
        fields = (
            "id", "academic_year", "delivery_group", "annual_index", "locked_timeslot",
            "reason", "created_by", "created_at", "updated_by", "updated_at",
            "materialized_section",
        )
        read_only_fields = (
            "id", "created_by", "created_at", "updated_by", "updated_at",
            "materialized_section",
        )

    def validate(self, attrs):
        # A virtual slot's identity is immutable. Moving it to a different group
        # or ordinal would silently change what a counselor's earlier reason
        # referred to; delete/recreate is the explicit workflow instead.
        if self.instance:
            for field in ("academic_year", "delivery_group", "annual_index"):
                if field in attrs and attrs[field] != getattr(self.instance, field):
                    raise serializers.ValidationError({field: "Annual lock identity cannot be changed."})
        return attrs


class SectionPlacementRunCreateSerializer(serializers.Serializer):
    """Small request contract for fixed-semester or annual-total placement."""

    academic_year = serializers.IntegerField(min_value=1)
    input_mode = serializers.ChoiceField(choices=("fixed_semester", "annual_total"))
    budget_approval = serializers.PrimaryKeyRelatedField(
        queryset=SectionBudgetApproval.objects.all(), required=False, allow_null=True,
    )

    def validate(self, attrs):
        annual = attrs["input_mode"] == "annual_total"
        if annual and not attrs.get("budget_approval"):
            raise serializers.ValidationError({"budget_approval": "Annual-total placement requires an approved section budget."})
        if not annual and attrs.get("budget_approval"):
            raise serializers.ValidationError({"budget_approval": "Fixed-semester placement does not accept a budget approval."})
        return attrs


class SectionPlacementApprovalRequestSerializer(serializers.Serializer):
    """Approval has no edit payload: locks are the explicit change mechanism."""

    reason = serializers.CharField(required=True, allow_blank=False, max_length=2000)

    def validate_reason(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("An approval reason is required.")
        return value


class SectionPlacementApprovalAssignmentSerializer(serializers.ModelSerializer):
    """Read-only per-section approved timeslot provenance."""

    class Meta:
        model = SectionPlacementApprovalAssignment
        fields = ("id", "section", "timeslot", "annual_index")
        read_only_fields = fields


class SectionPlacementApprovalSerializer(serializers.ModelSerializer):
    """Append-only approval header and exact timing lines it created."""

    assignments = SectionPlacementApprovalAssignmentSerializer(many=True, read_only=True)

    class Meta:
        model = SectionPlacementApproval
        fields = ("id", "placement_run", "approved_by", "approved_at", "reason", "assignments")
        read_only_fields = fields


class SectionPlacementRunSerializer(serializers.ModelSerializer):
    """Immutable recommendation; result deliberately omits witness teacher IDs."""

    approval = SectionPlacementApprovalSerializer(read_only=True)

    class Meta:
        model = SectionPlacementRun
        fields = (
            "id", "academic_year", "input_mode", "budget_approval", "conflict_matrix",
            "teacher_roster", "created_by", "created_at", "status", "input_snapshot",
            "result", "solver_metadata", "approval",
        )
        read_only_fields = fields


class TeacherAssignmentRunCreateSerializer(serializers.Serializer):
    """Small request contract: all accepted placements in one year are scoped."""

    academic_year = serializers.IntegerField(min_value=1)


class TeacherAssignmentApprovalRequestSerializer(serializers.Serializer):
    """Approval cannot edit candidates; configuration changes require a new run."""

    reason = serializers.CharField(required=True, allow_blank=False, max_length=2000)

    def validate_reason(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("An approval reason is required.")
        return value


class TeacherAssignmentApprovalAssignmentSerializer(serializers.ModelSerializer):
    """Read-only provenance for one Section.teacher write."""

    class Meta:
        model = TeacherAssignmentApprovalAssignment
        fields = ("id", "section", "teacher")
        read_only_fields = fields


class TeacherAssignmentApprovalOnlineSupervisionSerializer(serializers.ModelSerializer):
    """Read-only supervisor provenance kept distinct from section assignments."""

    class Meta:
        model = TeacherAssignmentApprovalOnlineSupervision
        fields = ("id", "online_supervision_session", "teacher")
        read_only_fields = fields


class TeacherAssignmentApprovalSerializer(serializers.ModelSerializer):
    """Append-only counselor approval and its exact named assignments."""

    assignments = TeacherAssignmentApprovalAssignmentSerializer(many=True, read_only=True)
    online_supervision_assignments = TeacherAssignmentApprovalOnlineSupervisionSerializer(
        many=True,
        read_only=True,
    )

    class Meta:
        model = TeacherAssignmentApproval
        fields = (
            "id", "teacher_assignment_run", "approved_by", "approved_at", "reason",
            "assignments", "online_supervision_assignments",
        )
        read_only_fields = fields


class TeacherAssignmentRunSerializer(serializers.ModelSerializer):
    """Immutable named-teacher candidate and optionally accepted provenance."""

    approval = TeacherAssignmentApprovalSerializer(read_only=True)

    class Meta:
        model = TeacherAssignmentRun
        fields = (
            "id", "academic_year", "teacher_roster", "created_by", "created_at", "status",
            "input_snapshot", "result", "solver_metadata", "approval",
        )
        read_only_fields = fields


class StudentAssignmentSoftConstraintImportanceSerializer(serializers.Serializer):
    """Counselor labels only; engine weights are intentionally never exposed."""

    section_utilization_balance = serializers.ChoiceField(choices=SOFT_CONSTRAINT_IMPORTANCE_CHOICES)
    student_semester_balance = serializers.ChoiceField(choices=SOFT_CONSTRAINT_IMPORTANCE_CHOICES)
    course_sequence_preferences = serializers.ChoiceField(choices=SOFT_CONSTRAINT_IMPORTANCE_CHOICES)
    difficulty_balance = serializers.ChoiceField(choices=SOFT_CONSTRAINT_IMPORTANCE_CHOICES)
    course_category_diversity = serializers.ChoiceField(choices=SOFT_CONSTRAINT_IMPORTANCE_CHOICES)


class StudentAssignmentRunCreateSerializer(serializers.Serializer):
    """Create one immutable student assignment run over fixed section context."""

    academic_year = serializers.IntegerField(min_value=1)
    staffing_mode = serializers.ChoiceField(choices=STUDENT_ASSIGNMENT_STAFFING_MODE_CHOICES)
    provisional_teacher_assignment_run = serializers.PrimaryKeyRelatedField(
        queryset=TeacherAssignmentRun.objects.all(),
        required=False,
        allow_null=True,
    )
    soft_constraint_importance = StudentAssignmentSoftConstraintImportanceSerializer()
    scope_type = serializers.ChoiceField(choices=STUDENT_ASSIGNMENT_RUN_SCOPE_CHOICES, default="full")
    source_approval = serializers.PrimaryKeyRelatedField(
        queryset=StudentAssignmentApproval.objects.all(), required=False, allow_null=True,
    )
    scope_student_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1), required=False, default=list,
    )
    scope_course_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1), required=False, default=list,
    )
    scope_section_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1), required=False, default=list,
    )
    selected_lock_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1), required=False, allow_null=True, default=None,
    )
    priority_request_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1), required=False, default=list,
    )
    schedule_preservation_level = serializers.ChoiceField(
        choices=STUDENT_ASSIGNMENT_SCHEDULE_PRESERVATION_CHOICES, default="none",
    )

    def validate(self, attrs):
        attrs = super().validate(attrs)
        provisional = attrs.get("provisional_teacher_assignment_run")
        if attrs["staffing_mode"] == STUDENT_ASSIGNMENT_STAFFING_MODE_PROVISIONAL_STAFFING:
            if provisional is None:
                raise serializers.ValidationError({
                    "provisional_teacher_assignment_run": "This field is required for provisional_staffing."
                })
        elif provisional is not None:
            raise serializers.ValidationError({
                "provisional_teacher_assignment_run": "This field is allowed only for provisional_staffing."
            })
        priority_ids = attrs.get("priority_request_ids", [])
        if len(priority_ids) > STUDENT_ASSIGNMENT_DEFAULT_MAX_PRIORITY_REQUESTS:
            raise serializers.ValidationError({
                "priority_request_ids": (
                    f"At most {STUDENT_ASSIGNMENT_DEFAULT_MAX_PRIORITY_REQUESTS} priority requests may be selected."
                ),
            })
        if len(priority_ids) != len(set(priority_ids)):
            raise serializers.ValidationError({"priority_request_ids": "Priority request IDs must be unique."})
        return attrs


class StudentAssignmentLockCreateSerializer(serializers.Serializer):
    """Transport shape for all six lock types; the service owns target rules."""

    academic_year = serializers.IntegerField(min_value=1)
    lock_type = serializers.ChoiceField(choices=STUDENT_ASSIGNMENT_LOCK_TYPE_CHOICES)
    student = serializers.IntegerField(min_value=1, required=False, allow_null=True)
    section = serializers.IntegerField(min_value=1, required=False, allow_null=True)
    course = serializers.IntegerField(min_value=1, required=False, allow_null=True)
    teacher = serializers.IntegerField(min_value=1, required=False, allow_null=True)
    group_student_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1), required=False, default=list,
    )
    reason = serializers.CharField(required=True, allow_blank=False, max_length=2000)
    staffing_mode = serializers.CharField(required=False, allow_blank=False)

    def validate_reason(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("A non-blank lock reason is required.")
        return value

    def validate(self, attrs):
        group_ids = attrs.get("group_student_ids", ())
        if len(group_ids) != len(set(group_ids)):
            raise serializers.ValidationError({"group_student_ids": "Group student IDs must be unique."})
        return attrs


class StudentAssignmentLockReleaseSerializer(serializers.Serializer):
    """Every release is an audited one-way transition with a human reason."""

    release_reason = serializers.CharField(required=True, allow_blank=False, max_length=2000)

    def validate_release_reason(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("A non-blank release reason is required.")
        return value


class StudentAssignmentLockSerializer(serializers.ModelSerializer):
    """Read-only lock history, including group membership as a real relation."""

    group_student_ids = serializers.SerializerMethodField()

    class Meta:
        model = StudentAssignmentLock
        fields = (
            "id", "academic_year", "lock_type", "student", "section", "course", "teacher",
            "group_student_ids", "reason", "created_by", "created_at", "is_active",
            "released_at", "released_by", "release_reason",
        )
        read_only_fields = fields

    def get_group_student_ids(self, instance):
        return list(instance.members.values_list("student_id", flat=True))


class StudentSpecialCommitmentLockCreateSerializer(serializers.Serializer):
    """Transport shape for one exact or excluded special-program choice."""

    academic_year = serializers.IntegerField(min_value=1)
    lock_type = serializers.ChoiceField(choices=STUDENT_SPECIAL_COMMITMENT_LOCK_TYPE_CHOICES)
    lock_mode = serializers.ChoiceField(choices=STUDENT_SPECIAL_COMMITMENT_LOCK_MODE_CHOICES)
    schedule_commitment_request = serializers.IntegerField(min_value=1, required=False, allow_null=True)
    course_request = serializers.IntegerField(min_value=1, required=False, allow_null=True)
    timeslot = serializers.IntegerField(min_value=1, required=False, allow_null=True)
    semester = serializers.ChoiceField(choices=(1, 2), required=False, allow_null=True)
    co_op_block_pair = serializers.ChoiceField(choices=CO_OP_BLOCK_PAIR_CHOICES, required=False, allow_null=True)
    reason = serializers.CharField(required=True, allow_blank=False, max_length=2000)

    def validate_reason(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("A non-blank lock reason is required.")
        return value


class StudentSpecialCommitmentLockReleaseSerializer(StudentAssignmentLockReleaseSerializer):
    """Reuse the standard release-reason contract for this append-only lock."""


class StudentSpecialCommitmentLockSerializer(serializers.ModelSerializer):
    """Read-only audit record for a special student scheduling restriction."""

    class Meta:
        model = StudentSpecialCommitmentLock
        fields = (
            "id", "academic_year", "lock_type", "lock_mode",
            "schedule_commitment_request", "course_request", "timeslot",
            "semester", "co_op_block_pair", "reason", "created_by", "created_at",
            "is_active", "released_at", "released_by", "release_reason",
        )
        read_only_fields = fields


class StudentAssignmentWhatIfUnlockSerializer(serializers.Serializer):
    """Read-only what-if input; no approval or persistence is possible here."""

    lock_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1), min_length=1,
    )

    def validate_lock_ids(self, value):
        if len(value) != len(set(value)):
            raise serializers.ValidationError("Lock IDs must be unique.")
        return value


class StudentAssignmentApprovalRequestSerializer(serializers.Serializer):
    """Approval is an auditable acceptance, never an in-place candidate edit."""

    reason = serializers.CharField(required=True, allow_blank=False, max_length=2000)

    def validate_reason(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("An approval reason is required.")
        return value


class StudentAssignmentApprovalEnrollmentSerializer(serializers.ModelSerializer):
    """Read-only provenance for a new enrollment created by run approval."""

    class Meta:
        model = StudentAssignmentApprovalEnrollment
        fields = (
            "id", "enrollment", "course_request", "assignment_basis",
            "backup_resolution_snapshot",
        )
        read_only_fields = fields


class StudentAssignmentApprovalOnlineEnrollmentSerializer(serializers.ModelSerializer):
    """Read-only provenance for one academic online enrollment and its seat."""

    class Meta:
        model = StudentAssignmentApprovalOnlineEnrollment
        fields = (
            "id", "online_enrollment", "course_request", "superseded_online_enrollment",
        )
        read_only_fields = fields


class StudentAssignmentApprovalCommitmentSerializer(serializers.ModelSerializer):
    """Read-only provenance for an approved Study, Co-op, or Focus commitment."""

    class Meta:
        model = StudentAssignmentApprovalCommitment
        fields = (
            "id", "commitment", "schedule_commitment_request", "course_request",
            "superseded_commitment",
        )
        read_only_fields = fields


class StudentAssignmentApprovalSerializer(serializers.ModelSerializer):
    """Immutable approval plus each enrollment fact it created."""

    enrollment_provenance = StudentAssignmentApprovalEnrollmentSerializer(many=True, read_only=True)
    online_enrollment_provenance = StudentAssignmentApprovalOnlineEnrollmentSerializer(
        many=True,
        read_only=True,
    )
    commitment_provenance = StudentAssignmentApprovalCommitmentSerializer(
        many=True,
        read_only=True,
    )

    class Meta:
        model = StudentAssignmentApproval
        fields = (
            "id", "student_assignment_run", "approved_by", "approved_at", "reason",
            "enrollment_provenance", "online_enrollment_provenance",
            "commitment_provenance",
        )
        read_only_fields = fields


class StudentAssignmentRunSerializer(serializers.ModelSerializer):
    """Immutable student assignment candidate and, when present, approval audit."""

    approval = StudentAssignmentApprovalSerializer(read_only=True)

    class Meta:
        model = StudentAssignmentRun
        fields = (
            "id", "academic_year", "staffing_mode", "provisional_teacher_assignment_run",
            "source_approval", "scope_type", "created_by", "created_at", "status",
            "input_snapshot", "result",
            "solver_metadata", "approval",
        )
        read_only_fields = fields


class SectionCountRecommendationSerializer(serializers.Serializer):
    """Read-only shape for the legacy heuristic recommendation endpoint."""

    course_id = serializers.IntegerField()
    course_code = serializers.CharField()
    current_requests = serializers.IntegerField()
    conversion_ratio = serializers.FloatField()
    predicted_enrollment = serializers.FloatField()
    capacity_min = serializers.IntegerField()
    capacity_max = serializers.IntegerField()
    recommended_section_count = serializers.IntegerField()
    used_fallback_ratio = serializers.BooleanField()
    warnings = serializers.ListField(child=serializers.CharField())


class CapacityProfileSerializer(serializers.ModelSerializer):
    """Validate the ordered five-point class-size policy."""

    # usage_count is informational and prevents clients from guessing whether a
    # delete will be blocked by attached courses.
    usage_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = CapacityProfile
        fields = ("id", "name", "scope", "hard_min", "soft_min", "target", "soft_max", "hard_max", "usage_count")

    def validate(self, attrs):
        # Merge PATCH fields with the instance before checking cross-field order.
        try:
            validate_capacity_order(capacity_values(self.instance, attrs))
        except ValueError as error:
            raise serializers.ValidationError(CAPACITY_ORDER_MESSAGE) from error
        return attrs

    def to_representation(self, instance):
        # Compute at response time so the count reflects current attachments.
        result = super().to_representation(instance)
        usage_count = getattr(instance, "course_usage_count", None)
        result["usage_count"] = (
            usage_count if usage_count is not None else instance.courses.count()
        )
        return result


class CoursePriorityProfileSerializer(serializers.ModelSerializer):
    """Simple administrator-owned name/tier policy representation."""

    class Meta:
        model = CoursePriorityProfile
        fields = ("id", "name", "tier")


class TeacherPlanningCapacitySerializer(serializers.ModelSerializer):
    """Validate a unique teacher/year/semester planning ceiling."""

    # remaining_sections is a model property, not independently writable state.
    remaining_sections = serializers.IntegerField(read_only=True)

    class Meta:
        model = TeacherPlanningCapacity
        fields = ("id", "teacher", "academic_year", "semester", "maximum_sections", "reserved_sections", "remaining_sections")
        validators = []

    def validate(self, attrs):
        # Merge existing values for PATCH before applying the cross-field rule.
        maximum = attrs.get("maximum_sections", getattr(self.instance, "maximum_sections", None))
        reserved = attrs.get("reserved_sections", getattr(self.instance, "reserved_sections", 0))
        if reserved > maximum:
            raise serializers.ValidationError({"reserved_sections": "Cannot exceed maximum_sections."})
        teacher = attrs.get("teacher", getattr(self.instance, "teacher", None))
        academic_year = attrs.get("academic_year", getattr(self.instance, "academic_year", None))
        semester = attrs.get("semester", getattr(self.instance, "semester", None))
        if teacher and academic_year and semester:
            # The model constraint is the final concurrency guard; this query
            # provides a readable API error before attempting the write.
            duplicate = TeacherPlanningCapacity.objects.filter(teacher=teacher, academic_year=academic_year, semester=semester)
            if self.instance:
                duplicate = duplicate.exclude(pk=self.instance.pk)
            if duplicate.exists():
                raise serializers.ValidationError("A planning capacity already exists for this teacher, year, and semester.")
        return attrs


class TeacherPlanningAnnualCapacitySerializer(serializers.ModelSerializer):
    """Year-scoped total teacher load, separate from semester ceilings."""

    remaining_sections = serializers.IntegerField(read_only=True)

    class Meta:
        model = TeacherPlanningAnnualCapacity
        fields = ("id", "teacher", "academic_year", "maximum_sections", "reserved_sections", "remaining_sections")
        validators = []

    def validate(self, attrs):
        maximum = attrs.get("maximum_sections", getattr(self.instance, "maximum_sections", None))
        reserved = attrs.get("reserved_sections", getattr(self.instance, "reserved_sections", 0))
        if reserved > maximum:
            raise serializers.ValidationError({"reserved_sections": "Cannot exceed maximum_sections."})
        teacher = attrs.get("teacher", getattr(self.instance, "teacher", None))
        year = attrs.get("academic_year", getattr(self.instance, "academic_year", None))
        if teacher and year:
            duplicate = TeacherPlanningAnnualCapacity.objects.filter(teacher=teacher, academic_year=year)
            if self.instance:
                duplicate = duplicate.exclude(pk=self.instance.pk)
            if duplicate.exists():
                raise serializers.ValidationError("An annual planning capacity already exists for this teacher and year.")
        return attrs


class TeacherCourseAssignmentRuleSerializer(serializers.ModelSerializer):
    """Transport contract for annual course-specific hard teaching bounds."""

    class Meta:
        model = TeacherCourseAssignmentRule
        fields = (
            "id", "academic_year", "teacher", "course", "minimum_sections", "maximum_sections",
            "reason", "created_by", "created_at", "updated_by", "updated_at",
        )
        read_only_fields = ("id", "created_by", "created_at", "updated_by", "updated_at")
        validators = []

    def validate(self, attrs):
        minimum = attrs.get("minimum_sections", getattr(self.instance, "minimum_sections", 0))
        maximum = attrs.get("maximum_sections", getattr(self.instance, "maximum_sections", None))
        if maximum is not None and minimum > maximum:
            raise serializers.ValidationError({"maximum_sections": "Cannot be less than minimum_sections."})
        reason = attrs.get("reason", getattr(self.instance, "reason", ""))
        if not isinstance(reason, str) or not reason.strip():
            raise serializers.ValidationError({"reason": "A reason is required for a teacher course rule."})
        teacher = attrs.get("teacher", getattr(self.instance, "teacher", None))
        course = attrs.get("course", getattr(self.instance, "course", None))
        year = attrs.get("academic_year", getattr(self.instance, "academic_year", None))
        if teacher and course and year:
            duplicate = TeacherCourseAssignmentRule.objects.filter(teacher=teacher, course=course, academic_year=year)
            if self.instance:
                duplicate = duplicate.exclude(pk=self.instance.pk)
            if duplicate.exists():
                raise serializers.ValidationError("A rule already exists for this teacher, course, and year.")
        return attrs


class TeacherTimePreferenceSerializer(serializers.ModelSerializer):
    """A soft preferred/avoid slot; hard denial remains TeacherAvailability."""

    class Meta:
        model = TeacherTimePreference
        fields = (
            "id", "academic_year", "teacher", "timeslot", "preference", "reason",
            "created_by", "created_at", "updated_by", "updated_at",
        )
        read_only_fields = ("id", "created_by", "created_at", "updated_by", "updated_at")
        validators = []

    def validate(self, attrs):
        year = attrs.get("academic_year", getattr(self.instance, "academic_year", None))
        timeslot = attrs.get("timeslot", getattr(self.instance, "timeslot", None))
        reason = attrs.get("reason", getattr(self.instance, "reason", ""))
        if year and timeslot and timeslot.academic_year_id != year.id:
            raise serializers.ValidationError({"timeslot": "Timeslot must belong to the selected academic year."})
        if not isinstance(reason, str) or not reason.strip():
            raise serializers.ValidationError({"reason": "A reason is required for a teacher time preference."})
        teacher = attrs.get("teacher", getattr(self.instance, "teacher", None))
        if teacher and year and timeslot:
            duplicate = TeacherTimePreference.objects.filter(teacher=teacher, academic_year=year, timeslot=timeslot)
            if self.instance:
                duplicate = duplicate.exclude(pk=self.instance.pk)
            if duplicate.exists():
                raise serializers.ValidationError("A preference already exists for this teacher and timeslot.")
        return attrs


class TeacherPlanningRosterSerializer(serializers.ModelSerializer):
    """Expose readiness and the exact teachers included in the year snapshot."""

    teacher_ids = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = TeacherPlanningRoster
        fields = (
            "id",
            "academic_year",
            "status",
            "teacher_ids",
            "confirmed_by",
            "confirmed_at",
        )
        read_only_fields = ("status", "confirmed_by", "confirmed_at")
        validators = []

    def validate_academic_year(self, value):
        duplicate = TeacherPlanningRoster.objects.filter(academic_year=value)
        if self.instance:
            duplicate = duplicate.exclude(pk=self.instance.pk)
        if duplicate.exists():
            raise serializers.ValidationError("This year already has a teacher roster.")
        return value

    def get_teacher_ids(self, instance):
        return list(instance.members.values_list("teacher_id", flat=True))


class TeacherPlanningRosterMembersSerializer(serializers.Serializer):
    """Replace the explicit membership of a draft/ready roster."""

    teacher_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        allow_empty=True,
    )

    def validate_teacher_ids(self, values):
        if len(values) != len(set(values)):
            raise serializers.ValidationError("Each teacher may appear only once.")
        return values


class CourseCapacityPolicySerializer(serializers.Serializer):
    """Accept either a shared profile reference or copy-on-write custom values."""

    capacity_profile = serializers.PrimaryKeyRelatedField(queryset=CapacityProfile.objects.all(), required=False)
    hard_min = serializers.IntegerField(required=False, min_value=1)
    soft_min = serializers.IntegerField(required=False, min_value=1)
    target = serializers.IntegerField(required=False, min_value=1)
    soft_max = serializers.IntegerField(required=False, min_value=1)
    hard_max = serializers.IntegerField(required=False, min_value=1)

    def validate(self, attrs):
        values = {key: attrs[key] for key in CAPACITY_FIELDS if key in attrs}
        # Mixing a profile ID and values makes ownership/precedence ambiguous.
        if attrs.get("capacity_profile") and values:
            raise serializers.ValidationError("Provide either capacity_profile or custom capacity values, not both.")
        if not attrs.get("capacity_profile") and not values:
            raise serializers.ValidationError("Provide a shared capacity_profile or custom capacity values.")
        if attrs.get("capacity_profile") and attrs["capacity_profile"].scope != CAPACITY_PROFILE_SCOPE_SHARED:
            # Course-specific profiles belong to their existing course; callers
            # customize another course through values and copy-on-write instead.
            raise serializers.ValidationError({"capacity_profile": "Only shared profiles may be assigned directly."})
        if values:
            course = self.context["course"]
            source = course.capacity_profile
            # Partial custom input inherits omitted thresholds from the current
            # profile before validating the five-value ordering.
            merged = capacity_values(source, values)
            try:
                validate_capacity_order(merged)
            except ValueError as error:
                raise serializers.ValidationError(CAPACITY_ORDER_MESSAGE) from error
        return attrs


class SectionPlanningRunCreateSerializer(serializers.Serializer):
    """Validate immutable what-if overlays accepted by the section planner."""

    academic_year = serializers.IntegerField(min_value=1)
    course_constraints = serializers.ListField(child=serializers.DictField(), required=False, default=list)
    teacher_capacity_adjustments = serializers.ListField(child=serializers.DictField(), required=False, default=list)

    def validate(self, attrs):
        seen_courses = set()
        for item in attrs["course_constraints"]:
            # Course existence is checked after academic-year validation in the
            # service/engine boundary; this layer validates generic request shape.
            if not isinstance(item.get("course_id"), int) or item["course_id"] <= 0:
                raise serializers.ValidationError({"course_constraints": "Each course constraint requires a positive integer course_id."})
            if item["course_id"] in seen_courses:
                raise serializers.ValidationError({"course_constraints": "Only one constraint is allowed per course."})
            seen_courses.add(item["course_id"])
            keys = {key for key in ("exact_sections", "min_sections", "max_sections") if key in item}
            if not keys:
                raise serializers.ValidationError({"course_constraints": "Each course constraint needs exact_sections, min_sections, or max_sections."})
            for key in keys:
                if not isinstance(item[key], int) or item[key] < 0:
                    raise serializers.ValidationError({"course_constraints": f"{key} must be a non-negative integer."})
            if "exact_sections" in item and len(keys) > 1:
                # Exact plus range is redundant at best and contradictory at
                # worst, so keep the scenario contract unambiguous.
                raise serializers.ValidationError({"course_constraints": "exact_sections cannot be combined with min_sections or max_sections."})
            if item.get("min_sections", 0) > item.get("max_sections", float("inf")):
                raise serializers.ValidationError({"course_constraints": "min_sections cannot exceed max_sections."})
        seen_capacities = set()
        for item in attrs["teacher_capacity_adjustments"]:
            # Adjustments are scenario-only reductions to one teacher/semester.
            if not isinstance(item.get("teacher_id"), int) or item["teacher_id"] <= 0 or item.get("semester") not in (1, 2):
                raise serializers.ValidationError({"teacher_capacity_adjustments": "Each adjustment requires a positive teacher_id and semester 1 or 2."})
            key = (item["teacher_id"], item["semester"])
            if key in seen_capacities:
                raise serializers.ValidationError({"teacher_capacity_adjustments": "Only one adjustment is allowed per teacher and semester."})
            seen_capacities.add(key)
            if item.get("is_excluded") and "reduce_by" in item:
                # Exclusion already means zero remaining capacity; combining it
                # with a numeric reduction obscures intent.
                raise serializers.ValidationError({"teacher_capacity_adjustments": "is_excluded cannot be combined with reduce_by."})
            if not item.get("is_excluded"):
                reduction = item.get("reduce_by", 0)
                if not isinstance(reduction, int) or reduction < 0:
                    raise serializers.ValidationError({"teacher_capacity_adjustments": "reduce_by must be a non-negative integer."})
        return attrs


class SectionPlanningCourseSelectionSerializer(serializers.Serializer):
    """One counselor-approved Semester 1/2 count pair."""

    course_id = serializers.IntegerField(min_value=1)
    semester_1_count = serializers.IntegerField(min_value=0)
    semester_2_count = serializers.IntegerField(min_value=0)


class SectionPlanningApprovalRequestSerializer(serializers.Serializer):
    """Approval/preview payload; omitted courses means all remaining results."""

    courses = SectionPlanningCourseSelectionSerializer(many=True, required=False)
    reason = serializers.CharField(required=False, allow_blank=True, default="", max_length=2000)

    def validate(self, attrs):
        attrs = super().validate(attrs)
        if "courses" in attrs:
            # Omission and an explicit empty list have different human meanings;
            # reject the latter instead of silently approving everything.
            if not attrs["courses"]:
                raise serializers.ValidationError({
                    "courses": "Omit courses to approve all remaining recommendations, or provide at least one course."
                })
            course_ids = [item["course_id"] for item in attrs["courses"]]
            # One course with two competing count pairs cannot be interpreted
            # deterministically by the approval service.
            if len(course_ids) != len(set(course_ids)):
                raise serializers.ValidationError({
                    "courses": "Each course may appear only once in an approval."
                })
        return attrs


class SectionPlanningReconciliationApplySerializer(SectionPlanningApprovalRequestSerializer):
    """Require proof of a reviewed preview before changing section rows."""

    preview_token = serializers.CharField(min_length=64, max_length=64)
    reason = serializers.CharField(required=True, allow_blank=False, max_length=2000)

    def validate_reason(self, value):
        # Whitespace is not a useful audit reason even though it is technically
        # non-empty input. Store the normalized text presented to reviewers.
        value = value.strip()
        if not value:
            raise serializers.ValidationError("A reconciliation reason is required.")
        return value


class SectionPlanningApprovalCourseSerializer(serializers.ModelSerializer):
    """Read-only audit line and the draft sections it generated."""

    generated_section_ids = serializers.PrimaryKeyRelatedField(
        source="generated_sections",
        many=True,
        read_only=True,
    )

    class Meta:
        model = SectionPlanningApprovalCourse
        fields = (
            "id",
            "course",
            "recommended_semester_1_count",
            "recommended_semester_2_count",
            "approved_semester_1_count",
            "approved_semester_2_count",
            "generated_section_ids",
        )
        read_only_fields = fields


class SectionPlanningReconciliationActionSerializer(serializers.ModelSerializer):
    """Expose one immutable section-level before/after audit event."""

    class Meta:
        model = SectionPlanningReconciliationAction
        fields = (
            "id",
            "section",
            "action",
            "previous_lifecycle_status",
            "new_lifecycle_status",
            "previous_semester",
            "new_semester",
            "previous_section_number",
            "new_section_number",
            "previous_capacity_min",
            "previous_capacity_max",
            "new_capacity_min",
            "new_capacity_max",
            "protection_reasons",
        )
        read_only_fields = fields


class SectionPlanningReconciliationCourseSerializer(serializers.ModelSerializer):
    """Expose a reconciled course decision and its concrete section actions."""

    course = serializers.IntegerField(source="approval_course.course_id", read_only=True)
    approval_course = serializers.IntegerField(source="approval_course_id", read_only=True)
    actions = SectionPlanningReconciliationActionSerializer(many=True, read_only=True)

    class Meta:
        model = SectionPlanningReconciliationCourse
        fields = (
            "id",
            "approval_course",
            "course",
            "previous_semester_1_count",
            "previous_semester_2_count",
            "final_semester_1_count",
            "final_semester_2_count",
            "actions",
        )
        read_only_fields = fields


class SectionPlanningReconciliationSerializer(serializers.ModelSerializer):
    """Read-only reconciliation header nested beneath its approval."""

    approval = serializers.IntegerField(source="approval_id", read_only=True)
    planning_run = serializers.IntegerField(
        source="approval.planning_run_id",
        read_only=True,
    )
    reconciled_by = serializers.IntegerField(
        source="approval.approved_by_id",
        read_only=True,
    )
    reconciled_at = serializers.DateTimeField(
        source="approval.approved_at",
        read_only=True,
    )
    reason = serializers.CharField(source="approval.reason", read_only=True)
    course_reconciliations = SectionPlanningReconciliationCourseSerializer(
        many=True,
        read_only=True,
    )

    class Meta:
        model = SectionPlanningReconciliation
        fields = (
            "id",
            "approval",
            "planning_run",
            "reconciled_by",
            "reconciled_at",
            "reason",
            "preview_token",
            "previous_active_section_count",
            "final_active_section_count",
            "course_reconciliations",
        )
        read_only_fields = fields


class SectionPlanningApprovalSerializer(serializers.ModelSerializer):
    """Read-only approval header with normalized per-course decisions."""

    course_approvals = SectionPlanningApprovalCourseSerializer(many=True, read_only=True)
    reconciliation = SectionPlanningReconciliationSerializer(read_only=True)

    class Meta:
        model = SectionPlanningApproval
        fields = (
            "id",
            "planning_run",
            "approved_by",
            "approved_at",
            "reason",
            "course_approvals",
            "reconciliation",
        )
        read_only_fields = fields


class SectionPlanningRunSerializer(serializers.ModelSerializer):
    """Return the frozen run and all approval batches derived from it."""

    approvals = SectionPlanningApprovalSerializer(many=True, read_only=True)

    class Meta:
        model = SectionPlanningRun
        fields = ("id", "academic_year", "created_by", "created_at", "status", "scenario_constraints", "input_snapshot", "result", "solver_metadata", "approvals")
        read_only_fields = fields


class SectionBudgetOfferingConstraintSerializer(serializers.Serializer):
    offering_id = serializers.IntegerField(min_value=1)
    exact_sections = serializers.IntegerField(min_value=0, required=False)
    min_sections = serializers.IntegerField(min_value=0, required=False)
    max_sections = serializers.IntegerField(min_value=0, required=False)

    def validate(self, attrs):
        attrs = super().validate(attrs)
        count_fields = {
            field for field in ("exact_sections", "min_sections", "max_sections")
            if field in attrs
        }
        if not count_fields:
            raise serializers.ValidationError("At least one section constraint is required.")
        if "exact_sections" in attrs and len(count_fields) > 1:
            raise serializers.ValidationError(
                "exact_sections cannot be combined with minimum or maximum."
            )
        if attrs.get("min_sections", 0) > attrs.get("max_sections", float("inf")):
            raise serializers.ValidationError("min_sections cannot exceed max_sections.")
        return attrs


class BackupPolicyOverrideSerializer(serializers.Serializer):
    course_id = serializers.IntegerField(min_value=1)
    policy = serializers.ChoiceField(choices=BACKUP_POLICY_CHOICES)


class SectionBudgetRunCreateSerializer(serializers.Serializer):
    academic_year = serializers.IntegerField(min_value=1)
    budget_type = serializers.ChoiceField(choices=SECTION_BUDGET_TYPE_CHOICES)
    section_budget = serializers.IntegerField(min_value=0)
    backup_policy = serializers.ChoiceField(choices=BACKUP_POLICY_CHOICES)
    backup_overrides = BackupPolicyOverrideSerializer(many=True, required=False, default=list)
    offering_constraints = SectionBudgetOfferingConstraintSerializer(
        many=True,
        required=False,
        default=list,
    )

    def validate(self, attrs):
        attrs = super().validate(attrs)
        override_ids = [item["course_id"] for item in attrs["backup_overrides"]]
        if len(override_ids) != len(set(override_ids)):
            raise serializers.ValidationError({
                "backup_overrides": "Each course may have only one backup-policy override."
            })
        offering_ids = [item["offering_id"] for item in attrs["offering_constraints"]]
        if len(offering_ids) != len(set(offering_ids)):
            raise serializers.ValidationError({
                "offering_constraints": "Each offering may have only one constraint."
            })
        return attrs


class SectionBudgetApprovalSelectionSerializer(serializers.Serializer):
    offering_id = serializers.IntegerField(min_value=1)
    semester_1_count = serializers.IntegerField(min_value=0)
    semester_2_count = serializers.IntegerField(min_value=0)


class SectionBudgetApprovalRequestSerializer(serializers.Serializer):
    offerings = SectionBudgetApprovalSelectionSerializer(many=True, required=False)
    reason = serializers.CharField(allow_blank=False, max_length=2000)

    def validate(self, attrs):
        attrs = super().validate(attrs)
        attrs["reason"] = attrs["reason"].strip()
        if not attrs["reason"]:
            raise serializers.ValidationError({"reason": "An approval reason is required."})
        if "offerings" in attrs:
            if not attrs["offerings"]:
                raise serializers.ValidationError({
                    "offerings": "Omit offerings to accept all recommendations."
                })
            ids = [item["offering_id"] for item in attrs["offerings"]]
            if len(ids) != len(set(ids)):
                raise serializers.ValidationError({
                    "offerings": "Each offering may appear only once."
                })
        return attrs


class PlanningRequestResolutionSerializer(serializers.ModelSerializer):
    backup_course = serializers.IntegerField(
        source="backup_request.course_id",
        read_only=True,
    )

    class Meta:
        model = PlanningRequestResolution
        fields = (
            "id",
            "student",
            "cancelled_course_ids",
            "backup_request",
            "backup_course",
            "outcome",
            "unresolved_course_count",
        )
        read_only_fields = fields


class SectionBudgetApprovalOfferingSerializer(serializers.ModelSerializer):
    class Meta:
        model = SectionBudgetApprovalOffering
        fields = (
            "id",
            "delivery_group",
            "recommended_annual_count",
            "recommended_semester_1_count",
            "recommended_semester_2_count",
            "approved_annual_count",
            "approved_semester_1_count",
            "approved_semester_2_count",
        )
        read_only_fields = fields


class SectionBudgetApprovalSerializer(serializers.ModelSerializer):
    offering_approvals = SectionBudgetApprovalOfferingSerializer(many=True, read_only=True)
    request_resolutions = PlanningRequestResolutionSerializer(many=True, read_only=True)

    class Meta:
        model = SectionBudgetApproval
        fields = (
            "id",
            "budget_run",
            "approved_by",
            "approved_at",
            "reason",
            "offering_approvals",
            "request_resolutions",
        )
        read_only_fields = fields


class SectionBudgetRunSerializer(serializers.ModelSerializer):
    approval = SectionBudgetApprovalSerializer(read_only=True)

    class Meta:
        model = SectionBudgetRun
        fields = (
            "id",
            "academic_year",
            "created_by",
            "created_at",
            "status",
            "budget_type",
            "section_budget",
            "backup_policy",
            "backup_overrides",
            "scenario_constraints",
            "input_snapshot",
            "result",
            "solver_metadata",
            "approval",
        )
        read_only_fields = fields


class TeacherCapacityAdjustmentSerializer(serializers.Serializer):
    """Non-persistent reduction used to explore one staffing scenario."""

    teacher_id = serializers.IntegerField(min_value=1)
    semester = serializers.ChoiceField(choices=(1, 2))
    is_excluded = serializers.BooleanField(required=False, default=False)
    reduce_by = serializers.IntegerField(min_value=0, required=False, default=0)

    def validate(self, attrs):
        attrs = super().validate(attrs)
        if attrs["is_excluded"] and attrs["reduce_by"]:
            raise serializers.ValidationError("is_excluded cannot be combined with reduce_by.")
        return attrs


class StaffingPlanRunCreateSerializer(serializers.Serializer):
    """Create a direct staffing run or refine one approved section budget."""

    academic_year = serializers.IntegerField(min_value=1)
    budget_approval = serializers.PrimaryKeyRelatedField(
        queryset=SectionBudgetApproval.objects.select_related("budget_run"),
        required=False,
        allow_null=True,
    )
    backup_policy = serializers.ChoiceField(
        choices=BACKUP_POLICY_CHOICES,
        required=False,
        default="ignore",
    )
    backup_overrides = BackupPolicyOverrideSerializer(many=True, required=False, default=list)
    offering_constraints = SectionBudgetOfferingConstraintSerializer(
        many=True,
        required=False,
        default=list,
    )
    teacher_capacity_adjustments = TeacherCapacityAdjustmentSerializer(
        many=True,
        required=False,
        default=list,
    )

    def validate(self, attrs):
        attrs = super().validate(attrs)
        for field, key in (
            ("backup_overrides", "course_id"),
            ("offering_constraints", "offering_id"),
        ):
            values = [item[key] for item in attrs[field]]
            if len(values) != len(set(values)):
                raise serializers.ValidationError({field: f"Each {key} may appear only once."})
        adjustment_keys = [
            (item["teacher_id"], item["semester"])
            for item in attrs["teacher_capacity_adjustments"]
        ]
        if len(adjustment_keys) != len(set(adjustment_keys)):
            raise serializers.ValidationError({
                "teacher_capacity_adjustments": "Each teacher/semester may appear only once."
            })
        return attrs


class StaffingRequestResolutionSerializer(serializers.ModelSerializer):
    backup_course = serializers.IntegerField(source="backup_request.course_id", read_only=True)

    class Meta:
        model = StaffingRequestResolution
        fields = (
            "id", "student", "cancelled_course_ids", "backup_request",
            "backup_course", "outcome", "unresolved_course_count",
        )
        read_only_fields = fields


class StaffingPlanApprovalOfferingSerializer(serializers.ModelSerializer):
    generated_section_ids = serializers.PrimaryKeyRelatedField(
        source="generated_sections",
        many=True,
        read_only=True,
    )

    class Meta:
        model = StaffingPlanApprovalOffering
        fields = (
            "id", "delivery_group", "recommended_semester_1_count",
            "recommended_semester_2_count", "approved_semester_1_count",
            "approved_semester_2_count", "generated_section_ids",
        )
        read_only_fields = fields


class StaffingPlanApprovalSerializer(serializers.ModelSerializer):
    offering_approvals = StaffingPlanApprovalOfferingSerializer(many=True, read_only=True)

    class Meta:
        model = StaffingPlanApproval
        fields = (
            "id", "staffing_run", "approved_by", "approved_at", "reason",
            "offering_approvals",
        )
        read_only_fields = fields


class StaffingPlanRunSerializer(serializers.ModelSerializer):
    approval = StaffingPlanApprovalSerializer(read_only=True)
    request_resolutions = StaffingRequestResolutionSerializer(many=True, read_only=True)

    class Meta:
        model = StaffingPlanRun
        fields = (
            "id", "academic_year", "budget_approval", "teacher_roster",
            "created_by", "created_at",
            "status", "backup_policy", "backup_overrides", "scenario_constraints",
            "input_snapshot", "result", "solver_metadata", "request_resolutions",
            "approval",
        )
        read_only_fields = fields
