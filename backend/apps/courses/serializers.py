"""DRF validation for catalog courses, offered sections, and student demand."""

from rest_framework import serializers

from backend.apps.common.exceptions import DomainValidationError
from backend.apps.common.constants import (
    COURSE_REQUEST_TYPE_ALTERNATE,
    DELIVERY_GROUP_STATUS_ACTIVE,
    SECTION_LIFECYCLE_RETIRED,
)
from backend.apps.constraints.services import (
    validate_teacher_course_assignment,
    validate_teacher_delivery_group_assignment,
)
from backend.apps.courses.models import (
    Course,
    CourseCategoryRelationship,
    CourseCombinationRule,
    CourseCombinationRuleMember,
    CourseOffering,
    CoursePrerequisite,
    CourseSequencePreference,
    DeliveryGroup,
    CourseRequest,
    Section,
)
from backend.apps.people.models import RoleChoices
from backend.apps.people.roles import get_user_role


class CapacityValidationMixin:
    """Validate legacy min/max compatibility fields on create and partial update."""

    def validate(self, attrs):
        # Merge PATCH input with the instance before comparing the pair.
        attrs = super().validate(attrs)
        capacity_min = attrs.get("capacity_min", getattr(self.instance, "capacity_min", None))
        capacity_max = attrs.get("capacity_max", getattr(self.instance, "capacity_max", None))
        if capacity_min is not None and capacity_max is not None and capacity_min > capacity_max:
            raise serializers.ValidationError({"capacity_max": "Must be greater than or equal to capacity_min."})
        return attrs


class CourseSerializer(CapacityValidationMixin, serializers.ModelSerializer):
    """Catalog serializer; default capacity profile is assigned by the model."""

    calculated_difficulty = serializers.SerializerMethodField(read_only=True)
    effective_difficulty = serializers.SerializerMethodField(read_only=True)
    difficulty_explanation = serializers.SerializerMethodField(read_only=True)

    def get_calculated_difficulty(self, instance):
        from backend.apps.courses.services.difficulty import course_difficulty_facts

        return course_difficulty_facts(instance)["calculated_difficulty"]

    def get_effective_difficulty(self, instance):
        from backend.apps.courses.services.difficulty import course_difficulty_facts

        return course_difficulty_facts(instance)["effective_difficulty"]

    def get_difficulty_explanation(self, instance):
        from backend.apps.courses.services.difficulty import course_difficulty_facts

        return course_difficulty_facts(instance)

    class Meta:
        model = Course
        fields = (
            "id", "name", "grade_level", "course_code", "category",
            "capacity_min", "capacity_max", "capacity_profile", "priority_profile",
            "allowed_semester", "is_online", "manual_difficulty_override",
            "calculated_difficulty", "effective_difficulty",
            "difficulty_explanation",
        )
        extra_kwargs = {
            # Capacity policy changes use the dedicated copy-on-write endpoint.
            "capacity_profile": {"required": False, "read_only": True},
            "priority_profile": {"required": False},
        }


class CourseCategoryRelationshipSerializer(serializers.ModelSerializer):
    """Planning configuration for category diversity without solver coefficients."""

    class Meta:
        model = CourseCategoryRelationship
        fields = ("id", "category_a", "category_b", "similarity_score")

    def validate(self, attrs):
        attrs = super().validate(attrs)
        category_a = attrs.get("category_a", getattr(self.instance, "category_a", None))
        category_b = attrs.get("category_b", getattr(self.instance, "category_b", None))
        if category_a and category_b and category_a >= category_b:
            raise serializers.ValidationError({
                "category_b": "Use two distinct categories in alphabetical order so each relationship has one stable identity.",
            })
        return attrs


class SectionSerializer(CapacityValidationMixin, serializers.ModelSerializer):
    """Operational section plus read-only planning provenance."""

    # Follow the normalized audit relationship so API clients can trace a draft
    # without exposing a writable approval foreign key.
    planning_approval = serializers.IntegerField(
        source="planning_approval_course.approval_id",
        read_only=True,
    )
    planning_run = serializers.IntegerField(
        source="planning_approval_course.approval.planning_run_id",
        read_only=True,
    )
    staffing_approval = serializers.IntegerField(
        source="staffing_approval_offering.approval_id",
        read_only=True,
    )
    staffing_run = serializers.IntegerField(
        source="staffing_approval_offering.approval.staffing_run_id",
        read_only=True,
    )
    member_courses = serializers.SerializerMethodField(read_only=True)
    is_combined = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = Section
        fields = (
            "id",
            "course",
            "delivery_group",
            "member_courses",
            "is_combined",
            "section_number",
            "academic_year",
            "semester",
            "teacher",
            "capacity_min",
            "capacity_max",
            "is_locked",
            "lifecycle_status",
            "planning_approval",
            "planning_run",
            "staffing_approval",
            "staffing_run",
        )
        extra_kwargs = {
            # Lifecycle transitions are reconciliation decisions, not ordinary
            # CRUD fields that API callers may toggle without an audit record.
            "lifecycle_status": {"read_only": True},
        }

    def validate(self, attrs):
        attrs = super().validate(attrs)
        if self.instance and self.instance.lifecycle_status == SECTION_LIFECYCLE_RETIRED:
            raise serializers.ValidationError({
                "detail": "Retired sections are read-only. Reconcile a newer section plan to reactivate one."
            })
        if self.instance and (
            self.instance.planning_approval_course_id
            or self.instance.staffing_approval_offering_id
        ):
            # A generated section's identity is part of its planning audit.
            # Staffing/capacity/lock edits remain allowed, while moving or
            # relabeling it must pass through reconciliation.
            protected_fields = {
                "course": "course_id",
                "delivery_group": "delivery_group_id",
                "section_number": "section_number",
                "academic_year": "academic_year_id",
                "semester": "semester",
            }
            changed = []
            for input_name, instance_name in protected_fields.items():
                if input_name not in attrs:
                    continue
                submitted = attrs[input_name]
                submitted_value = getattr(submitted, "pk", submitted)
                if submitted_value != getattr(self.instance, instance_name):
                    changed.append(input_name)
            if changed:
                raise serializers.ValidationError({
                    field: "Generated section identity can only be changed by section-plan reconciliation."
                    for field in changed
                })
        course = attrs.get("course", getattr(self.instance, "course", None))
        delivery_group = attrs.get(
            "delivery_group",
            getattr(self.instance, "delivery_group", None),
        )
        if course and delivery_group and not delivery_group.offerings.filter(course=course).exists():
            raise serializers.ValidationError({
                "course": "The compatibility course must belong to the selected delivery group."
            })
        if not course and not delivery_group:
            raise serializers.ValidationError({
                "delivery_group": "A section needs a physical delivery group or a legacy course."
            })
        if delivery_group:
            member_count = delivery_group.offerings.count()
            if delivery_group.status != DELIVERY_GROUP_STATUS_ACTIVE:
                raise serializers.ValidationError({
                    "delivery_group": "A new or active section cannot use a retired delivery group."
                })
            if member_count == 0:
                raise serializers.ValidationError({
                    "delivery_group": "The delivery group has no course offerings."
                })
            if member_count > 1 and course:
                raise serializers.ValidationError({
                    "course": "A combined physical section has no single canonical course; omit course."
                })
        academic_year = attrs.get(
            "academic_year",
            getattr(self.instance, "academic_year", None),
        )
        if delivery_group and academic_year and delivery_group.academic_year_id != academic_year.id:
            raise serializers.ValidationError({
                "delivery_group": "The delivery group belongs to a different academic year."
            })
        teacher = attrs.get("teacher", getattr(self.instance, "teacher", None))
        try:
            if delivery_group and teacher:
                validate_teacher_delivery_group_assignment(delivery_group, teacher)
            elif course and teacher:
                # Direct section assignment and lock assignment share the same
                # normalized senior-course qualification rules.
                validate_teacher_course_assignment(course, teacher)
        except DomainValidationError as error:
            raise serializers.ValidationError(error.detail) from error
        return attrs

    def get_member_courses(self, instance):
        if instance.delivery_group_id:
            return [
                {
                    "offering_id": offering.id,
                    "course_id": offering.course_id,
                    "course_code": offering.course.course_code,
                }
                for offering in instance.delivery_group.offerings.select_related("course").all()
            ]
        if instance.course_id:
            return [{
                "offering_id": None,
                "course_id": instance.course_id,
                "course_code": instance.course.course_code,
            }]
        return []

    def get_is_combined(self, instance):
        return len(self.get_member_courses(instance)) > 1


class CourseRequestSerializer(serializers.ModelSerializer):
    """Validate request ownership and one request per student/course/year."""

    class Meta:
        model = CourseRequest
        fields = ("id", "student", "academic_year", "course", "is_mandatory", "request_type")
        extra_kwargs = {"student": {"required": False}}
        validators = []

    def validate(self, attrs):
        attrs = super().validate(attrs)
        request = self.context["request"]
        if get_user_role(request.user) == RoleChoices.STUDENT:
            # Student identity comes from authentication, not caller-controlled
            # JSON. A supplied mismatching student receives an explicit error.
            submitted_student = attrs.get("student")
            if submitted_student and submitted_student.user_id != request.user.id:
                raise serializers.ValidationError({"student": "Students may only create requests for themselves."})
            student = request.user.student_profile
        else:
            # Planning roles may submit on behalf of a student but must identify
            # that student explicitly on create.
            student = attrs.get("student", getattr(self.instance, "student", None))
            if student is None:
                raise serializers.ValidationError({"student": "This field is required."})

        course = attrs.get("course", getattr(self.instance, "course", None))
        academic_year = attrs.get("academic_year", getattr(self.instance, "academic_year", None))
        if student and course and academic_year:
            # Disable DRF's generated validator above so create/PATCH can share
            # this clear, ownership-aware duplicate check.
            duplicate_requests = CourseRequest.objects.filter(
                student=student,
                course=course,
                academic_year=academic_year,
            )
            if self.instance:
                # Retaining the same combination on PATCH is valid.
                duplicate_requests = duplicate_requests.exclude(pk=self.instance.pk)
            if duplicate_requests.exists():
                raise serializers.ValidationError("A request for this course already exists for this student and academic year.")
            request_type = attrs.get(
                "request_type",
                getattr(self.instance, "request_type", None),
            )
            if request_type == COURSE_REQUEST_TYPE_ALTERNATE:
                existing_backup = CourseRequest.objects.filter(
                    student=student,
                    academic_year=academic_year,
                    request_type=COURSE_REQUEST_TYPE_ALTERNATE,
                )
                if self.instance:
                    existing_backup = existing_backup.exclude(pk=self.instance.pk)
                if existing_backup.exists():
                    raise serializers.ValidationError({
                        "request_type": "A student may have only one backup course per academic year."
                    })
        return attrs


def _would_create_directed_cycle(model_class, *, source_id, target_id, source_field, target_field, instance_id=None):
    """Return whether adding source -> target closes a catalog graph cycle."""

    if source_id == target_id:
        return True
    edges = model_class.objects.all()
    if instance_id:
        edges = edges.exclude(pk=instance_id)
    adjacency = {}
    for edge_source, edge_target in edges.values_list(
        f"{source_field}_id", f"{target_field}_id"
    ):
        adjacency.setdefault(edge_source, set()).add(edge_target)
    # A path from proposed target back to proposed source would close a cycle.
    pending = [target_id]
    visited = set()
    while pending:
        current = pending.pop()
        if current == source_id:
            return True
        if current not in visited:
            visited.add(current)
            pending.extend(adjacency.get(current, ()))
    return False


class CoursePrerequisiteSerializer(serializers.ModelSerializer):
    """Planning-owned hard prerequisite configuration with cycle protection."""

    class Meta:
        model = CoursePrerequisite
        fields = ("id", "course", "prerequisite")
        validators = []

    def validate(self, attrs):
        attrs = super().validate(attrs)
        course = attrs.get("course", getattr(self.instance, "course", None))
        prerequisite = attrs.get("prerequisite", getattr(self.instance, "prerequisite", None))
        if course and prerequisite and _would_create_directed_cycle(
            CoursePrerequisite,
            source_id=prerequisite.id,
            target_id=course.id,
            source_field="prerequisite",
            target_field="course",
            instance_id=getattr(self.instance, "id", None),
        ):
            raise serializers.ValidationError({
                "prerequisite": "Prerequisites must not be self-referential or form a directed cycle."
            })
        duplicate = CoursePrerequisite.objects.filter(course=course, prerequisite=prerequisite)
        if self.instance:
            duplicate = duplicate.exclude(pk=self.instance.pk)
        if duplicate.exists():
            raise serializers.ValidationError("This prerequisite already exists.")
        return attrs


class CourseSequencePreferenceSerializer(serializers.ModelSerializer):
    """Planning-owned non-binding course ordering preference configuration."""

    class Meta:
        model = CourseSequencePreference
        fields = ("id", "earlier_course", "later_course", "is_active", "created_by", "created_at")
        read_only_fields = ("created_by", "created_at")
        validators = []

    def validate(self, attrs):
        attrs = super().validate(attrs)
        earlier_course = attrs.get("earlier_course", getattr(self.instance, "earlier_course", None))
        later_course = attrs.get("later_course", getattr(self.instance, "later_course", None))
        if earlier_course and later_course and _would_create_directed_cycle(
            CourseSequencePreference,
            source_id=earlier_course.id,
            target_id=later_course.id,
            source_field="earlier_course",
            target_field="later_course",
            instance_id=getattr(self.instance, "id", None),
        ):
            raise serializers.ValidationError({
                "later_course": "Sequence preferences must not be self-referential or form a directed cycle."
            })
        duplicate = CourseSequencePreference.objects.filter(
            earlier_course=earlier_course,
            later_course=later_course,
        )
        if self.instance:
            duplicate = duplicate.exclude(pk=self.instance.pk)
        if duplicate.exists():
            raise serializers.ValidationError("This course sequence preference already exists.")
        return attrs


class CourseOfferingSerializer(serializers.ModelSerializer):
    """Current year-specific offering state and physical delivery membership."""

    course_code = serializers.CharField(source="course.course_code", read_only=True)
    delivery_group_name = serializers.CharField(
        source="delivery_group.name",
        read_only=True,
    )
    member_course_codes = serializers.SerializerMethodField(read_only=True)
    is_combined = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = CourseOffering
        fields = (
            "id",
            "course",
            "course_code",
            "academic_year",
            "status",
            "delivery_group",
            "delivery_group_name",
            "member_course_codes",
            "is_combined",
            "decision_reason",
            "decided_by",
            "decided_at",
        )
        read_only_fields = fields

    def get_member_course_codes(self, instance):
        if not instance.delivery_group_id:
            return []
        return [
            offering.course.course_code
            for offering in instance.delivery_group.offerings.select_related("course").all()
        ]

    def get_is_combined(self, instance):
        return len(self.get_member_course_codes(instance)) > 1


class DeliveryGroupSerializer(serializers.ModelSerializer):
    """Physical delivery identity shown as one class even when cross-listed."""

    offerings = CourseOfferingSerializer(many=True, read_only=True)
    is_combined = serializers.BooleanField(read_only=True)

    class Meta:
        model = DeliveryGroup
        fields = (
            "id",
            "academic_year",
            "name",
            "capacity_profile",
            "combination_rule",
            "status",
            "is_combined",
            "reason",
            "created_by",
            "created_at",
            "offerings",
        )
        read_only_fields = fields


class CourseCombinationRuleSerializer(serializers.ModelSerializer):
    """Manage the explicit compatibility groups eligible for suggestions."""

    course_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        write_only=True,
    )
    courses = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = CourseCombinationRule
        fields = (
            "id",
            "name",
            "capacity_profile",
            "is_active",
            "course_ids",
            "courses",
        )

    def validate_course_ids(self, values):
        if len(set(values)) < 2:
            raise serializers.ValidationError(
                "A combination rule requires at least two distinct courses."
            )
        if len(values) != len(set(values)):
            raise serializers.ValidationError("Each course may appear only once.")
        found = set(Course.objects.filter(id__in=values).values_list("id", flat=True))
        if found != set(values):
            raise serializers.ValidationError("Every course must exist.")
        return values

    def create(self, validated_data):
        course_ids = validated_data.pop("course_ids")
        rule = CourseCombinationRule.objects.create(**validated_data)
        CourseCombinationRuleMember.objects.bulk_create([
            CourseCombinationRuleMember(rule=rule, course_id=course_id)
            for course_id in course_ids
        ])
        return rule

    def update(self, instance, validated_data):
        course_ids = validated_data.pop("course_ids", None)
        instance = super().update(instance, validated_data)
        if course_ids is not None:
            instance.members.all().delete()
            CourseCombinationRuleMember.objects.bulk_create([
                CourseCombinationRuleMember(rule=instance, course_id=course_id)
                for course_id in course_ids
            ])
        return instance

    def get_courses(self, instance):
        return [
            {
                "id": member.course_id,
                "course_code": member.course.course_code,
                "name": member.course.name,
            }
            for member in instance.members.select_related("course").all()
        ]


class OfferingDecisionRequestSerializer(serializers.Serializer):
    reason = serializers.CharField(allow_blank=False, max_length=2000)

    def validate_reason(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("A decision reason is required.")
        return value


class CombineOfferingsRequestSerializer(OfferingDecisionRequestSerializer):
    academic_year = serializers.IntegerField(min_value=1)
    rule_id = serializers.IntegerField(min_value=1)
