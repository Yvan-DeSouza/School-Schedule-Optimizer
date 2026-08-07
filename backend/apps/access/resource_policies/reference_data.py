from backend.apps.access.resource_policies.base import BaseResourcePolicy
from backend.apps.access.rules import AccessRule
from backend.apps.access.scopes import ReadScope, WriteScope
from backend.apps.people.models import RoleChoices


class ReferenceDataPolicy(BaseResourcePolicy):
    """Readable school reference data, writable only by system administrators."""

    rules = {
        RoleChoices.STUDENT: AccessRule(read=ReadScope.ALL),
        RoleChoices.TEACHER: AccessRule(read=ReadScope.ALL),
        RoleChoices.COUNSELOR: AccessRule(read=ReadScope.ALL),
        RoleChoices.STAFF: AccessRule(read=ReadScope.ALL, write=WriteScope.ALL),
        RoleChoices.DIRECTOR: AccessRule(read=ReadScope.ALL, write=WriteScope.ALL),
    }
