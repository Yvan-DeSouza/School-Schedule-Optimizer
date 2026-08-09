"""Planning-role teacher directory API representations."""

from rest_framework import serializers

from backend.apps.people.models import Teacher


class TeacherSerializer(serializers.ModelSerializer):
    """Manage current teacher details while keeping archive state explicit."""

    class Meta:
        model = Teacher
        fields = (
            "id",
            "user",
            "first_name",
            "last_name",
            "email",
            "department",
            "seniority",
            "max_courses_per_semester",
            "max_courses_total",
            "is_reduced_load",
            "is_archived",
        )
        read_only_fields = ("is_archived",)


class TeacherArchiveSerializer(serializers.Serializer):
    """Require a human explanation before changing directory availability."""

    reason = serializers.CharField(max_length=2000, allow_blank=False)

    def validate_reason(self, value):
        if not value.strip():
            raise serializers.ValidationError("An archive reason is required.")
        return value.strip()
