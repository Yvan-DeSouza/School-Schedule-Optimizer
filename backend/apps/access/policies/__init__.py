"""Compatibility package forwarding to current resource-policy modules."""

from backend.apps.access.resource_policies.courses import (
    CoursePolicy,
    CourseRequestPolicy,
    SectionPolicy,
)

__all__ = ["CoursePolicy", "CourseRequestPolicy", "SectionPolicy"]
