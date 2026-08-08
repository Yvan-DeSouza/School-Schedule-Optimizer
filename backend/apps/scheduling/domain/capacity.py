"""Class-size capacity profile invariants."""

CAPACITY_FIELDS = ("hard_min", "soft_min", "target", "soft_max", "hard_max")
CAPACITY_ORDER_MESSAGE = (
    "Capacity values must satisfy "
    "hard_min <= soft_min <= target <= soft_max <= hard_max."
)


def capacity_values(source=None, overrides=None):
    """Merge partial capacity values over an existing source object."""

    overrides = overrides or {}
    return {
        field: overrides.get(field, getattr(source, field, None))
        for field in CAPACITY_FIELDS
    }


def validate_capacity_order(values):
    """Raise ValueError unless the five capacity thresholds are ordered."""

    if not (
        values["hard_min"]
        <= values["soft_min"]
        <= values["target"]
        <= values["soft_max"]
        <= values["hard_max"]
    ):
        raise ValueError(CAPACITY_ORDER_MESSAGE)
    return values
