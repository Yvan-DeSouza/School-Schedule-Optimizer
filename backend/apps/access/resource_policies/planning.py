"""Resource policy for mutable section-planning configuration."""

from backend.apps.access.resource_policies.base import BaseResourcePolicy
from backend.apps.access.rules import AccessRule
from backend.apps.access.scopes import ReadScope, WriteScope
from backend.apps.people.models import RoleChoices


class PlanningConfigurationPolicy(BaseResourcePolicy):
    """Counselor, staff, and director may read/write all configuration."""

    rules = {
        RoleChoices.COUNSELOR: AccessRule(read=ReadScope.ALL, write=WriteScope.ALL),
        RoleChoices.STAFF: AccessRule(read=ReadScope.ALL, write=WriteScope.ALL),
        RoleChoices.DIRECTOR: AccessRule(read=ReadScope.ALL, write=WriteScope.ALL),
        RoleChoices.UNKNOWN: AccessRule(),
    }
