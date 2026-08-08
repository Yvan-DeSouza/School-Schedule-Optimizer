"""Simple DRF representations for shared reference data."""

from rest_framework import serializers

from backend.apps.common.models import AcademicYear, Room


class AcademicYearSerializer(serializers.ModelSerializer):
    """Academic-year ID and canonical display name."""

    class Meta:
        model = AcademicYear
        fields = ("id", "name")


class RoomSerializer(serializers.ModelSerializer):
    """Room capacity/type attributes consumed by placement planning."""

    class Meta:
        model = Room
        fields = ("id", "name", "room_type", "capacity", "is_specialized")
