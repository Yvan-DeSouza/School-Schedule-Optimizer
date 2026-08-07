from rest_framework import serializers

from backend.apps.common.constants import (
    QUALIFICATION_DIVISION_NONE,
    QUALIFICATION_DIVISION_SENIOR,
    QUALIFICATION_ENFORCEMENT_REQUIRED,
    QUALIFICATION_KIND_TEACHABLE,
    STATUTORY_TEACHABLE_MIN_GRADE,
)
from backend.apps.constraints.models import (
    CounselorConstraintPreference, CourseConflict, CourseQualificationRequirement,
    CourseRoomRequirement, HardConstraint, Qualification, SoftConstraint,
    TeacherAvailability, TeacherCoursePreference, TeacherCurrentCourse, TeacherQualification,
)
from backend.apps.constraints.services import validate_locked_teacher_qualifications
from backend.apps.control.models import SectionLock


class TeacherHiddenSerializer(serializers.ModelSerializer):
    teacher = serializers.PrimaryKeyRelatedField(read_only=True)
    unique_fields = ()

    def validate(self, attrs):
        attrs = super().validate(attrs)
        teacher = self.context["teacher"]
        values = {"teacher": teacher}
        for field in self.unique_fields:
            value = attrs.get(field, getattr(self.instance, field, None))
            if value is None:
                return attrs
            values[field] = value
        matches = self.Meta.model.objects.filter(**values)
        if self.instance:
            matches = matches.exclude(pk=self.instance.pk)
        if matches.exists():
            raise serializers.ValidationError("This teacher constraint already exists.")
        return attrs


class TeacherQualificationSerializer(TeacherHiddenSerializer):
    unique_fields = ("qualification",)
    class Meta:
        model = TeacherQualification
        fields = (
            "id", "teacher", "qualification", "source_system", "source_record_id",
            "source_text", "awarded_date_text",
        )
        validators = []


class TeacherCoursePreferenceSerializer(TeacherHiddenSerializer):
    unique_fields = ("course",)
    class Meta:
        model = TeacherCoursePreference
        fields = ("id", "teacher", "course")
        validators = []


class TeacherCurrentCourseSerializer(TeacherHiddenSerializer):
    unique_fields = ("course", "academic_year")
    class Meta:
        model = TeacherCurrentCourse
        fields = ("id", "teacher", "course", "academic_year")
        validators = []


class TeacherAvailabilitySerializer(TeacherHiddenSerializer):
    unique_fields = ("timeslot",)
    class Meta:
        model = TeacherAvailability
        fields = ("id", "teacher", "timeslot", "is_available")
        validators = []


class QualificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Qualification
        fields = ("id", "code", "name", "kind", "subject_code", "division")

    def validate(self, attrs):
        attrs = super().validate(attrs)
        kind = attrs.get("kind", getattr(self.instance, "kind", None))
        subject_code = attrs.get("subject_code", getattr(self.instance, "subject_code", ""))
        division = attrs.get("division", getattr(self.instance, "division", None))
        if kind == QUALIFICATION_KIND_TEACHABLE:
            errors = {}
            if not subject_code:
                errors["subject_code"] = "A teachable qualification needs a canonical subject code."
            if division == QUALIFICATION_DIVISION_NONE:
                errors["division"] = "A teachable qualification needs an official division."
            if errors:
                raise serializers.ValidationError(errors)
        return attrs


class HardConstraintSerializer(serializers.ModelSerializer):
    class Meta:
        model = HardConstraint
        fields = ("id", "name", "type", "priority")


class SoftConstraintSerializer(serializers.ModelSerializer):
    class Meta:
        model = SoftConstraint
        fields = ("id", "name", "category", "default_weight")


class CounselorConstraintPreferenceSerializer(serializers.ModelSerializer):
    class Meta:
        model = CounselorConstraintPreference
        fields = ("id", "counselor", "constraint", "weight")


class CourseConflictSerializer(serializers.ModelSerializer):
    class Meta:
        model = CourseConflict
        fields = ("id", "course_a", "course_b", "weight")

    def validate(self, attrs):
        attrs = super().validate(attrs)
        course_a = attrs.get("course_a", getattr(self.instance, "course_a", None))
        course_b = attrs.get("course_b", getattr(self.instance, "course_b", None))
        weight = attrs.get("weight", getattr(self.instance, "weight", None))
        errors = {}
        if course_a and course_b and course_a == course_b:
            errors["course_b"] = "A course cannot conflict with itself."
        if weight is not None and weight < 0:
            errors["weight"] = "Must be greater than or equal to zero."
        if errors:
            raise serializers.ValidationError(errors)
        return attrs


class CourseRoomRequirementSerializer(serializers.ModelSerializer):
    class Meta:
        model = CourseRoomRequirement
        fields = ("id", "course", "room_type")


class CourseQualificationRequirementSerializer(serializers.ModelSerializer):
    class Meta:
        model = CourseQualificationRequirement
        fields = ("id", "course", "qualification", "enforcement")

    def validate(self, attrs):
        attrs = super().validate(attrs)
        course = attrs.get("course", getattr(self.instance, "course", None))
        qualification = attrs.get("qualification", getattr(self.instance, "qualification", None))
        enforcement = attrs.get("enforcement", getattr(self.instance, "enforcement", None))
        if not course or not qualification:
            return attrs

        errors = {}
        if qualification.kind != QUALIFICATION_KIND_TEACHABLE:
            errors["qualification"] = "Only teachable qualifications can be mapped to a course."
        if enforcement == QUALIFICATION_ENFORCEMENT_REQUIRED:
            if course.grade_level < STATUTORY_TEACHABLE_MIN_GRADE:
                errors["enforcement"] = "Required qualifications apply only to Grade 11 and Grade 12 courses."
            if qualification.division != QUALIFICATION_DIVISION_SENIOR:
                errors["qualification"] = "A Grade 11-12 required qualification must be a Senior teachable."
        if errors:
            raise serializers.ValidationError(errors)
        return attrs


class SectionLockSerializer(serializers.ModelSerializer):
    class Meta:
        model = SectionLock
        fields = ("id", "section", "locked_teacher", "locked_timeslot", "locked_room")
        read_only_fields = ("id", "section")

    def validate(self, attrs):
        attrs = super().validate(attrs)
        section = self.context["section"]
        teacher = attrs.get("locked_teacher", getattr(self.instance, "locked_teacher", None))
        validate_locked_teacher_qualifications(section, teacher)
        return attrs
