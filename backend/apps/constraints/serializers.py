"""Validation for normalized qualifications, preferences, and scheduling locks."""

from rest_framework import serializers

from backend.apps.common.exceptions import DomainValidationError
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
    """Base for teacher-nested routes where teacher comes from the URL.

    Hiding the writable teacher field prevents a teacher from posting data to
    their own URL while assigning the record to somebody else.
    """

    teacher = serializers.PrimaryKeyRelatedField(read_only=True)
    unique_fields = ()

    def validate(self, attrs):
        attrs = super().validate(attrs)
        teacher = self.context["teacher"]
        # Rebuild each model's composite uniqueness rule using the URL-owned
        # teacher plus fields declared by the subclass.
        values = {"teacher": teacher}
        for field in self.unique_fields:
            value = attrs.get(field, getattr(self.instance, field, None))
            if value is None:
                return attrs
            values[field] = value
        matches = self.Meta.model.objects.filter(**values)
        if self.instance:
            # Allow an update to retain its own unique values.
            matches = matches.exclude(pk=self.instance.pk)
        if matches.exists():
            raise serializers.ValidationError("This teacher constraint already exists.")
        return attrs


class TeacherQualificationSerializer(TeacherHiddenSerializer):
    """Normalized credential plus optional import/provenance text."""

    unique_fields = ("qualification",)
    class Meta:
        model = TeacherQualification
        fields = (
            "id", "teacher", "qualification", "source_system", "source_record_id",
            "source_text", "awarded_date_text", "review_status", "submitted_by",
            "submitted_at", "reviewed_by", "reviewed_at", "review_reason",
        )
        read_only_fields = (
            "review_status", "submitted_by", "submitted_at", "reviewed_by",
            "reviewed_at", "review_reason",
        )
        validators = []


class TeacherQualificationReviewSerializer(serializers.Serializer):
    """Optional reviewer note used for verification and required for rejection."""

    reason = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        max_length=2000,
    )


class TeacherCoursePreferenceSerializer(TeacherHiddenSerializer):
    """Structured preferred course; free-text parsing is outside current scope."""

    unique_fields = ("course",)
    class Meta:
        model = TeacherCoursePreference
        fields = ("id", "teacher", "course")
        validators = []


class TeacherCurrentCourseSerializer(TeacherHiddenSerializer):
    """Teacher/course/year history used only as scheduling preference context."""

    unique_fields = ("course", "academic_year")
    class Meta:
        model = TeacherCurrentCourse
        fields = ("id", "teacher", "course", "academic_year")
        validators = []


class TeacherAvailabilitySerializer(TeacherHiddenSerializer):
    """Availability for one recurring semester timeslot."""

    unique_fields = ("timeslot",)
    class Meta:
        model = TeacherAvailability
        fields = ("id", "teacher", "timeslot", "is_available")
        validators = []


class QualificationSerializer(serializers.ModelSerializer):
    """Validate canonical teachable metadata independently of source records."""

    class Meta:
        model = Qualification
        fields = ("id", "code", "name", "kind", "subject_code", "division")

    def validate(self, attrs):
        # Merge PATCH values with the current instance before cross-field checks.
        attrs = super().validate(attrs)
        kind = attrs.get("kind", getattr(self.instance, "kind", None))
        subject_code = attrs.get("subject_code", getattr(self.instance, "subject_code", ""))
        division = attrs.get("division", getattr(self.instance, "division", None))
        if kind == QUALIFICATION_KIND_TEACHABLE:
            # A teachable must identify both subject and official division; a
            # generic/additional qualification may legitimately omit them.
            errors = {}
            if not subject_code:
                errors["subject_code"] = "A teachable qualification needs a canonical subject code."
            if division == QUALIFICATION_DIVISION_NONE:
                errors["division"] = "A teachable qualification needs an official division."
            if errors:
                raise serializers.ValidationError(errors)
        return attrs


class HardConstraintSerializer(serializers.ModelSerializer):
    """Administrative hard-constraint metadata."""

    class Meta:
        model = HardConstraint
        fields = ("id", "name", "type", "priority")


class SoftConstraintSerializer(serializers.ModelSerializer):
    """Administrative soft-objective metadata and default weight."""

    class Meta:
        model = SoftConstraint
        fields = ("id", "name", "category", "default_weight")


class CounselorConstraintPreferenceSerializer(serializers.ModelSerializer):
    """Counselor-specific weight for one configured soft constraint."""

    class Meta:
        model = CounselorConstraintPreference
        fields = ("id", "counselor", "constraint", "weight")


class CourseConflictSerializer(serializers.ModelSerializer):
    """Validate weighted co-request conflict edges between distinct courses."""

    class Meta:
        model = CourseConflict
        fields = ("id", "course_a", "course_b", "weight")

    def validate(self, attrs):
        # Model constraints catch self-conflict at write time; serializer checks
        # provide field-specific errors and also validate non-negative weight.
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
    """Required canonical room type for a course."""

    class Meta:
        model = CourseRoomRequirement
        fields = ("id", "course", "room_type")


class CourseQualificationRequirementSerializer(serializers.ModelSerializer):
    """Enforce legal shape of course-to-qualification mappings."""

    class Meta:
        model = CourseQualificationRequirement
        fields = ("id", "course", "qualification", "enforcement")

    def validate(self, attrs):
        attrs = super().validate(attrs)
        course = attrs.get("course", getattr(self.instance, "course", None))
        qualification = attrs.get("qualification", getattr(self.instance, "qualification", None))
        enforcement = attrs.get("enforcement", getattr(self.instance, "enforcement", None))
        if not course or not qualification:
            # Required-field validation will report omissions; avoid misleading
            # secondary errors when either relationship is absent.
            return attrs

        errors = {}
        if qualification.kind != QUALIFICATION_KIND_TEACHABLE:
            # Additional qualifications can describe staff development but are
            # not legal teachables for course assignment.
            errors["qualification"] = "Only teachable qualifications can be mapped to a course."
        if enforcement == QUALIFICATION_ENFORCEMENT_REQUIRED:
            # Grade 9-10 mappings may express preference, while Grade 11-12 hard
            # requirements must use a normalized Senior teachable.
            if course.grade_level < STATUTORY_TEACHABLE_MIN_GRADE:
                errors["enforcement"] = "Required qualifications apply only to Grade 11 and Grade 12 courses."
            if qualification.division != QUALIFICATION_DIVISION_SENIOR:
                errors["qualification"] = "A Grade 11-12 required qualification must be a Senior teachable."
        if errors:
            raise serializers.ValidationError(errors)
        return attrs


class SectionLockSerializer(serializers.ModelSerializer):
    """Validate counselor-fixed teacher/timeslot/room decisions for a section."""

    class Meta:
        model = SectionLock
        fields = ("id", "section", "locked_teacher", "locked_timeslot", "locked_room")
        read_only_fields = ("id", "section")

    def validate(self, attrs):
        attrs = super().validate(attrs)
        section = self.context["section"]
        teacher = attrs.get("locked_teacher", getattr(self.instance, "locked_teacher", None))
        # Reuse the same qualification service used for direct Section teacher
        # assignment so lock and assignment rules cannot drift.
        try:
            validate_locked_teacher_qualifications(section, teacher)
        except DomainValidationError as error:
            raise serializers.ValidationError(error.detail) from error
        return attrs
