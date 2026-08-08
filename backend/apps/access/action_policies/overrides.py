"""Authorization for manual override creation/application/history."""

from backend.apps.access.action_policies.base import BaseActionPolicy
from backend.apps.access.rules import ActionRule
from backend.apps.access.scopes import ActionScope
from backend.apps.people.models import RoleChoices
from backend.apps.people.roles import get_user_role


class OverrideAction:
    """Stable names for the future manual-override workflow."""

    CREATE_OVERRIDE = "create_override"
    APPLY_OVERRIDE = "apply_override"
    VIEW_OVERRIDE_HISTORY = "view_override_history"


class OverrideActionPolicy(BaseActionPolicy):
    """Staff may review history; counselor/director may change decisions."""

    write_actions = {
        OverrideAction.CREATE_OVERRIDE,
        OverrideAction.APPLY_OVERRIDE,
    }
    history_actions = {OverrideAction.VIEW_OVERRIDE_HISTORY}
    rules = {
        RoleChoices.COUNSELOR: ActionRule(execute=ActionScope.ALLOWED),
        RoleChoices.STAFF: ActionRule(execute=ActionScope.ALLOWED),
        RoleChoices.DIRECTOR: ActionRule(execute=ActionScope.ALLOWED),
    }

    @classmethod
    def is_action_allowed(cls, user, action=None, context=None):
        if action in cls.history_actions:
            # Base role gating already excludes teachers/students/unknown roles.
            return True
        if action in cls.write_actions:
            # Applying overrides is intentionally narrower than read-only audit.
            return get_user_role(user) in {RoleChoices.COUNSELOR, RoleChoices.DIRECTOR}
        return False
