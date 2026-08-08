"""Shared capacity-profile validation contracts."""

import pytest
from django.core.exceptions import ValidationError

from backend.apps.scheduling.domain.capacity import (
    CAPACITY_ORDER_MESSAGE,
    capacity_values,
    validate_capacity_order,
)
from backend.apps.scheduling.models import CapacityProfile


def test_capacity_order_accepts_valid_values():
    values = {
        "hard_min": 10,
        "soft_min": 18,
        "target": 24,
        "soft_max": 30,
        "hard_max": 35,
    }

    assert validate_capacity_order(values) == values


def test_capacity_order_rejects_out_of_order_values():
    values = {
        "hard_min": 10,
        "soft_min": 18,
        "target": 36,
        "soft_max": 30,
        "hard_max": 35,
    }

    with pytest.raises(ValueError, match=CAPACITY_ORDER_MESSAGE):
        validate_capacity_order(values)


def test_capacity_values_merge_partial_over_source():
    class Source:
        hard_min = 10
        soft_min = 18
        target = 24
        soft_max = 30
        hard_max = 35

    assert capacity_values(Source(), {"target": 25}) == {
        "hard_min": 10,
        "soft_min": 18,
        "target": 25,
        "soft_max": 30,
        "hard_max": 35,
    }


@pytest.mark.django_db
def test_capacity_profile_model_clean_uses_shared_order_rule():
    profile = CapacityProfile(
        name="Invalid",
        scope="shared",
        hard_min=10,
        soft_min=18,
        target=36,
        soft_max=30,
        hard_max=35,
    )

    with pytest.raises(ValidationError):
        profile.full_clean()
