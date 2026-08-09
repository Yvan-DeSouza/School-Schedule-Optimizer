"""Regression tests for renamed boolean API fields."""

import pytest

from backend.apps.people.models import Teacher
from backend.apps.people.serializers import TeacherSerializer
from backend.apps.scheduling.serializers import TeacherCapacityAdjustmentSerializer


@pytest.mark.django_db
def test_teacher_serializer_exposes_is_reduced_load():
    teacher = Teacher.objects.create(
        first_name="Ada",
        last_name="Lovelace",
        email="ada@example.com",
        department="Mathematics",
        is_reduced_load=True,
    )

    payload = TeacherSerializer(teacher).data

    assert payload["is_reduced_load"] is True
    assert "reduced_load" not in payload


def test_teacher_capacity_adjustment_accepts_is_excluded():
    serializer = TeacherCapacityAdjustmentSerializer(
        data={"teacher_id": 1, "semester": 1, "is_excluded": True}
    )

    assert serializer.is_valid(), serializer.errors
    assert serializer.validated_data["is_excluded"] is True
    assert "excluded" not in serializer.validated_data
