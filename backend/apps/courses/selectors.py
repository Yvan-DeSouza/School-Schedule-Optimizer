"""Reusable course/section/offering query contracts.

Selectors keep common data-access rules out of individual views, services, and
engine adapters. They are intentionally plain query helpers, not a repository
layer; callers still decide when to add joins, locks, or annotations.
"""

from backend.apps.courses.constants import (
    COURSE_OFFERING_STATUS_OFFERED,
    COURSE_REQUEST_TYPE_PRIMARY,
    DELIVERY_GROUP_STATUS_ACTIVE,
)
from backend.apps.courses.models import CourseOffering, CourseRequest, DeliveryGroup, Section
from backend.apps.scheduling.constants import SECTION_LIFECYCLE_ACTIVE


def active_sections_queryset(queryset=None):
    """Return sections that participate in current operational scheduling."""

    base = queryset if queryset is not None else Section.objects.all()
    return base.filter(lifecycle_status=SECTION_LIFECYCLE_ACTIVE)


def active_sections_for_year(academic_year_id, queryset=None):
    """Return active sections for one planning year."""

    return active_sections_queryset(queryset).filter(academic_year_id=academic_year_id)


def offered_course_offerings_queryset(queryset=None):
    """Return course offerings currently available to be planned."""

    base = queryset if queryset is not None else CourseOffering.objects.all()
    return base.filter(status=COURSE_OFFERING_STATUS_OFFERED)


def offered_course_offerings_for_year(academic_year_id, queryset=None):
    """Return offered course/year rows for the selected academic year."""

    return offered_course_offerings_queryset(queryset).filter(
        academic_year_id=academic_year_id,
    )


def active_delivery_groups_queryset(queryset=None):
    """Return active physical delivery groups with at least one offered member."""

    base = queryset if queryset is not None else DeliveryGroup.objects.all()
    return base.filter(
        status=DELIVERY_GROUP_STATUS_ACTIVE,
        offerings__status=COURSE_OFFERING_STATUS_OFFERED,
    ).distinct()


def active_delivery_groups_for_year(academic_year_id, queryset=None):
    """Return active physical delivery groups for one planning year."""

    return active_delivery_groups_queryset(queryset).filter(
        academic_year_id=academic_year_id,
    )


def primary_course_requests_queryset(queryset=None):
    """Return primary demand rows; alternates are promoted only by run policy."""

    base = queryset if queryset is not None else CourseRequest.objects.all()
    return base.filter(request_type=COURSE_REQUEST_TYPE_PRIMARY)

