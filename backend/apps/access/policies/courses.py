"""Compatibility exports for course resource policies."""

from backend.apps.access.resource_policies.courses import (
    CoursePolicy,
    CourseRequestPolicy,
    SectionPolicy,
)

__all__ = ["CoursePolicy", "CourseRequestPolicy", "SectionPolicy"]
