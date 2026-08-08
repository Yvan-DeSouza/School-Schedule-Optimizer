"""Fail-closed base policy for named non-CRUD application actions."""

from backend.apps.access.rules import ActionRule
from backend.apps.access.scopes import ActionScope
from backend.apps.people.models import RoleChoices
from backend.apps.people.roles import get_user_role


class BaseActionPolicy:
    """Authorize a semantic action only when role and action are both allowed."""

    rules = {}

    @classmethod
    def rule_for(cls, user):
        # Authentication and recognized role are prerequisites for all actions.
        if not user or not user.is_authenticated:
            return ActionRule()

        role = get_user_role(user)
        if role == RoleChoices.UNKNOWN:
            return ActionRule()

        return cls.rules.get(role, ActionRule())

    @classmethod
    def can_execute(cls, user, action=None, context=None):
        # First gate by role, then let the subclass split individual actions.
        rule = cls.rule_for(user)
        if rule.execute != ActionScope.ALLOWED:
            return False
        return cls.is_action_allowed(user, action=action, context=context)

    @classmethod
    def is_action_allowed(cls, user, action=None, context=None):
        # Subclasses must opt in every stable action name.
        return False
