from rest_framework import serializers

from backend.apps.constraints.services import validate_teacher_course_assignment
from backend.apps.courses.models import Course, CourseRequest, Section
from backend.apps.people.models import RoleChoices
from backend.apps.people.roles import get_user_role


class CapacityValidationMixin:
    def validate(self, attrs):
        attrs = super().validate(attrs)
        capacity_min = attrs.get("capacity_min", getattr(self.instance, "capacity_min", None))
        capacity_max = attrs.get("capacity_max", getattr(self.instance, "capacity_max", None))
        if capacity_min is not None and capacity_max is not None and capacity_min > capacity_max:
            raise serializers.ValidationError({"capacity_max": "Must be greater than or equal to capacity_min."})
        return attrs


class CourseSerializer(CapacityValidationMixin, serializers.ModelSerializer):
    class Meta:
        model = Course
        fields = ("id", "name", "grade_level", "course_code", "category", "capacity_min", "capacity_max", "capacity_profile", "priority_profile", "allowed_semester", "is_online")
        extra_kwargs = {
            "capacity_profile": {"required": False, "read_only": True},
            "priority_profile": {"required": False},
        }


class SectionSerializer(CapacityValidationMixin, serializers.ModelSerializer):
    class Meta:
        model = Section
        fields = ("id", "course", "section_number", "academic_year", "semester", "teacher", "capacity_min", "capacity_max", "is_locked")

    def validate(self, attrs):
        attrs = super().validate(attrs)
        course = attrs.get("course", getattr(self.instance, "course", None))
        teacher = attrs.get("teacher", getattr(self.instance, "teacher", None))
        if course and teacher:
            validate_teacher_course_assignment(course, teacher)
        return attrs


class CourseRequestSerializer(serializers.ModelSerializer):
    class Meta:
        model = CourseRequest
        fields = ("id", "student", "academic_year", "course", "is_mandatory", "request_type")
        extra_kwargs = {"student": {"required": False}}
        validators = []

    def validate(self, attrs):
        attrs = super().validate(attrs)
        request = self.context["request"]
        if get_user_role(request.user) == RoleChoices.STUDENT:
            submitted_student = attrs.get("student")
            if submitted_student and submitted_student.user_id != request.user.id:
                raise serializers.ValidationError({"student": "Students may only create requests for themselves."})
            student = request.user.student_profile
        else:
            student = attrs.get("student", getattr(self.instance, "student", None))
            if student is None:
                raise serializers.ValidationError({"student": "This field is required."})

        course = attrs.get("course", getattr(self.instance, "course", None))
        academic_year = attrs.get("academic_year", getattr(self.instance, "academic_year", None))
        if student and course and academic_year:
            duplicate_requests = CourseRequest.objects.filter(
                student=student,
                course=course,
                academic_year=academic_year,
            )
            if self.instance:
                duplicate_requests = duplicate_requests.exclude(pk=self.instance.pk)
            if duplicate_requests.exists():
                raise serializers.ValidationError("A request for this course already exists for this student and academic year.")
        return attrs
