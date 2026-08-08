"""DRF validation for catalog courses, offered sections, and student demand."""

from rest_framework import serializers

from backend.apps.common.constants import SECTION_LIFECYCLE_RETIRED
from backend.apps.constraints.services import validate_teacher_course_assignment
from backend.apps.courses.models import Course, CourseRequest, Section
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

    class Meta:
        model = Course
        fields = ("id", "name", "grade_level", "course_code", "category", "capacity_min", "capacity_max", "capacity_profile", "priority_profile", "allowed_semester", "is_online")
        extra_kwargs = {
            # Capacity policy changes use the dedicated copy-on-write endpoint.
            "capacity_profile": {"required": False, "read_only": True},
            "priority_profile": {"required": False},
        }


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

    class Meta:
        model = Section
        fields = (
            "id",
            "course",
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
        if self.instance and self.instance.planning_approval_course_id:
            # A generated section's identity is part of its planning audit.
            # Staffing/capacity/lock edits remain allowed, while moving or
            # relabeling it must pass through reconciliation.
            protected_fields = {
                "course": "course_id",
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
        teacher = attrs.get("teacher", getattr(self.instance, "teacher", None))
        if course and teacher:
            # Direct section assignment and lock assignment share the same
            # normalized senior-course qualification rules.
            validate_teacher_course_assignment(course, teacher)
        return attrs


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
        return attrs
