from rest_framework import serializers

from backend.apps.scheduling.constants import BLOCK_ROTATION
from backend.apps.scheduling.models import TimeSlot


class TimeSlotSerializer(serializers.ModelSerializer):
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
        attrs = super().validate(attrs)
        academic_year = attrs.get("academic_year", getattr(self.instance, "academic_year", None))
        semester = attrs.get("semester", getattr(self.instance, "semester", None))
        block = attrs.get("block", getattr(self.instance, "block", None))
        if academic_year and semester and block:
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
