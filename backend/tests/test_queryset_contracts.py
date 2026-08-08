"""Lightweight queryset contracts for serializer-dependent list endpoints."""

from backend.apps.courses.views import CourseOfferingViewSet, SectionViewSet
from backend.apps.scheduling.views import CapacityProfileViewSet


def test_section_queryset_prefetches_delivery_group_member_courses():
    assert (
        "delivery_group__offerings__course"
        in SectionViewSet.queryset._prefetch_related_lookups
    )


def test_course_offering_queryset_prefetches_delivery_group_member_courses():
    assert (
        "delivery_group__offerings__course"
        in CourseOfferingViewSet.queryset._prefetch_related_lookups
    )


def test_capacity_profile_queryset_annotates_usage_count():
    query = str(CapacityProfileViewSet().get_policy_queryset().query)

    assert "COUNT" in query.upper()
    assert "course_usage_count" in query
