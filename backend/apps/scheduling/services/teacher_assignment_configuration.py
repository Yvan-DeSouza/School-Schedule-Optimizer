"""Writes for mutable named-teacher assignment configuration.

Keeping roster invalidation here prevents a later endpoint from changing a hard
workload fact while accidentally leaving the roster marked ready.
"""

from django.db import transaction

from backend.apps.scheduling.services.staffing_configuration import invalidate_roster


@transaction.atomic
def save_annual_capacity(serializer, *, actor):
    """Persist an annual capacity and invalidate the affected ready roster."""

    instance = serializer.save()
    invalidate_roster(instance.academic_year_id)
    return instance


@transaction.atomic
def delete_annual_capacity(instance):
    """Remove mutable capacity configuration and make readiness explicit again."""

    year_id = instance.academic_year_id
    instance.delete()
    invalidate_roster(year_id)


@transaction.atomic
def save_course_rule(serializer, *, actor):
    """Persist counselor-owned hard course bounds with actor metadata."""

    if serializer.instance:
        return serializer.save(updated_by=actor)
    return serializer.save(created_by=actor, updated_by=actor)


@transaction.atomic
def save_time_preference(serializer, *, actor):
    """Persist a non-binding time preference with actor metadata."""

    if serializer.instance:
        return serializer.save(updated_by=actor)
    return serializer.save(created_by=actor, updated_by=actor)
