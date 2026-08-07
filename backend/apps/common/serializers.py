from rest_framework import serializers

from backend.apps.common.models import AcademicYear, Room


class AcademicYearSerializer(serializers.ModelSerializer):
    class Meta:
        model = AcademicYear
        fields = ("id", "name")


class RoomSerializer(serializers.ModelSerializer):
    class Meta:
        model = Room
        fields = ("id", "name", "room_type", "capacity", "is_specialized")
