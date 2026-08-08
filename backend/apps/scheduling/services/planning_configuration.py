"""Transactional mutation services for planning configuration, never run audit.

Configuration edits affect future planning runs only. Existing runs retain the
profile values captured in their immutable input/result snapshots.
"""

from django.db import transaction

from backend.apps.common.constants import (
    CAPACITY_PROFILE_SCOPE_COURSE_SPECIFIC,
    CAPACITY_PROFILE_SCOPE_SHARED,
    COURSE_PRIORITY_TIER_STANDARD,
)
from backend.apps.scheduling.domain.capacity import (
    CAPACITY_FIELDS,
    capacity_values,
    validate_capacity_order,
)
from backend.apps.scheduling.models import CapacityProfile, CoursePriorityProfile


# Stable names let model.save() safely get-or-create defaults during imports and
# tests without producing duplicate policy rows.
DEFAULT_CAPACITY_PROFILE_NAME = "Standard Default"
DEFAULT_PRIORITY_PROFILE_NAME = "Standard Elective"


@transaction.atomic
def ensure_default_planning_profiles():
    """Return the shared defaults used when a catalogue course is created."""
    # get_or_create makes direct Course ORM creation backward compatible and
    # safe when several new courses are imported in one process.
    capacity_profile, _ = CapacityProfile.objects.get_or_create(
        name=DEFAULT_CAPACITY_PROFILE_NAME,
        defaults={
            "scope": CAPACITY_PROFILE_SCOPE_SHARED,
            "hard_min": 10,
            "soft_min": 18,
            "target": 24,
            "soft_max": 30,
            "hard_max": 35,
        },
    )
    priority_profile, _ = CoursePriorityProfile.objects.get_or_create(
        name=DEFAULT_PRIORITY_PROFILE_NAME,
        defaults={"tier": COURSE_PRIORITY_TIER_STANDARD},
    )
    return capacity_profile, priority_profile


@transaction.atomic
def apply_course_capacity_policy(course, *, profile=None, values=None):
    """Attach a shared profile or create a labelled one-course copy-on-write profile."""
    if values is None:
        # Attaching a shared profile is a simple relationship change; the
        # serializer has already rejected course-specific profiles here.
        if profile is None:
            raise ValueError("Provide a shared profile or capacity values.")
        course.capacity_profile = profile
        course.save(update_fields=["capacity_profile"])
        return profile

    source = course.capacity_profile
    merged_values = capacity_values(source, values)
    validate_capacity_order(merged_values)
    if source.scope == CAPACITY_PROFILE_SCOPE_COURSE_SPECIFIC and source.courses.count() == 1:
        # A profile owned solely by this course can be edited in place without
        # affecting any other course.
        for field in CAPACITY_FIELDS:
            setattr(source, field, merged_values[field])
        source.full_clean()
        source.save()
        return source

    # Shared (or unexpectedly multi-course-specific) source profiles must not be
    # mutated. Clone inherited/current values into a uniquely named private row.
    base_name = f"{course.course_code} custom"
    name = base_name
    suffix = 2
    while CapacityProfile.objects.filter(name=name).exists():
        # Deterministic suffixing keeps names human-readable across repeated
        # customization cycles.
        name = f"{base_name} {suffix}"
        suffix += 1
    profile = CapacityProfile.objects.create(
        name=name,
        scope=CAPACITY_PROFILE_SCOPE_COURSE_SPECIFIC,
        **merged_values,
    )
    # Model.save() does not call full_clean automatically; enforce threshold
    # ordering before attaching the new profile.
    profile.full_clean()
    course.capacity_profile = profile
    course.save(update_fields=["capacity_profile"])
    return profile
