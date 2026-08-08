"""DRF validation and representations for planning configuration and runs.

Serializers validate transport shape and current database relationships. Domain
orchestration—including transactional approval conflict checks—stays in service
modules so non-HTTP callers receive the same behavior.
"""

from rest_framework import serializers

from backend.apps.common.constants import BLOCK_ROTATION, CAPACITY_PROFILE_SCOPE_SHARED
from backend.apps.scheduling.models import (
    CapacityProfile,
    CoursePriorityProfile,
    SectionPlanningApproval,
    SectionPlanningApprovalCourse,
    SectionPlanningRun,
    TeacherPlanningCapacity,
    TimeSlot,
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
        values = {
            field: attrs.get(field, getattr(self.instance, field, None))
            for field in ("hard_min", "soft_min", "target", "soft_max", "hard_max")
        }
        if not (values["hard_min"] <= values["soft_min"] <= values["target"] <= values["soft_max"] <= values["hard_max"]):
            raise serializers.ValidationError("Capacity values must satisfy hard_min <= soft_min <= target <= soft_max <= hard_max.")
        return attrs

    def to_representation(self, instance):
        # Compute at response time so the count reflects current attachments.
        result = super().to_representation(instance)
        result["usage_count"] = instance.courses.count()
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


class CourseCapacityPolicySerializer(serializers.Serializer):
    """Accept either a shared profile reference or copy-on-write custom values."""

    capacity_profile = serializers.PrimaryKeyRelatedField(queryset=CapacityProfile.objects.all(), required=False)
    hard_min = serializers.IntegerField(required=False, min_value=1)
    soft_min = serializers.IntegerField(required=False, min_value=1)
    target = serializers.IntegerField(required=False, min_value=1)
    soft_max = serializers.IntegerField(required=False, min_value=1)
    hard_max = serializers.IntegerField(required=False, min_value=1)

    def validate(self, attrs):
        values = {key: attrs[key] for key in ("hard_min", "soft_min", "target", "soft_max", "hard_max") if key in attrs}
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
            merged = {field: values.get(field, getattr(source, field)) for field in ("hard_min", "soft_min", "target", "soft_max", "hard_max")}
            if not (merged["hard_min"] <= merged["soft_min"] <= merged["target"] <= merged["soft_max"] <= merged["hard_max"]):
                raise serializers.ValidationError("Capacity values must satisfy hard_min <= soft_min <= target <= soft_max <= hard_max.")
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
            if item.get("excluded") and "reduce_by" in item:
                # Exclusion already means zero remaining capacity; combining it
                # with a numeric reduction obscures intent.
                raise serializers.ValidationError({"teacher_capacity_adjustments": "excluded cannot be combined with reduce_by."})
            if not item.get("excluded"):
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


class SectionPlanningApprovalSerializer(serializers.ModelSerializer):
    """Read-only approval header with normalized per-course decisions."""

    course_approvals = SectionPlanningApprovalCourseSerializer(many=True, read_only=True)

    class Meta:
        model = SectionPlanningApproval
        fields = (
            "id",
            "planning_run",
            "approved_by",
            "approved_at",
            "reason",
            "course_approvals",
        )
        read_only_fields = fields


class SectionPlanningRunSerializer(serializers.ModelSerializer):
    """Return the frozen run and all approval batches derived from it."""

    approvals = SectionPlanningApprovalSerializer(many=True, read_only=True)

    class Meta:
        model = SectionPlanningRun
        fields = ("id", "academic_year", "created_by", "created_at", "status", "scenario_constraints", "input_snapshot", "result", "solver_metadata", "approvals")
        read_only_fields = fields
