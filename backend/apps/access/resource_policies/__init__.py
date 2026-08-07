from backend.apps.access.resource_policies.base import BaseResourcePolicy
from backend.apps.access.resource_policies.courses import (
    CoursePolicy,
    CourseRequestPolicy,
    SectionPolicy,
)

__all__ = [
    "BaseResourcePolicy",
    "CoursePolicy",
    "CourseRequestPolicy",
    "SectionPolicy",
]
