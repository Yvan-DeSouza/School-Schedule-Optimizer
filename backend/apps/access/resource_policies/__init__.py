"""Public import surface for model/resource policies."""

from backend.apps.access.resource_policies.base import BaseResourcePolicy
from backend.apps.access.resource_policies.courses import (
    CoursePolicy,
    CourseRequestPolicy,
    SectionPolicy,
)
from backend.apps.access.resource_policies.constraints import (
    PlanningResourcePolicy,
    TeacherOwnedResourcePolicy,
)
from backend.apps.access.resource_policies.reference_data import ReferenceDataPolicy

# Explicit exports preserve a stable policy API as modules are reorganized.
__all__ = [
    "BaseResourcePolicy",
    "CoursePolicy",
    "CourseRequestPolicy",
    "SectionPolicy",
    "PlanningResourcePolicy",
    "TeacherOwnedResourcePolicy",
    "ReferenceDataPolicy",
]
