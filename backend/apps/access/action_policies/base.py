from backend.apps.access.rules import ActionRule
from backend.apps.access.scopes import ActionScope
from backend.apps.people.models import RoleChoices
from backend.apps.people.roles import get_user_role


class BaseActionPolicy:
    rules = {}

    @classmethod
    def rule_for(cls, user):
        if not user or not user.is_authenticated:
            return ActionRule()

        role = get_user_role(user)
        if role == RoleChoices.UNKNOWN:
            return ActionRule()

        return cls.rules.get(role, ActionRule())

    @classmethod
    def can_execute(cls, user, action=None, context=None):
        rule = cls.rule_for(user)
        if rule.execute != ActionScope.ALLOWED:
            return False
        return cls.is_action_allowed(user, action=action, context=context)

    @classmethod
    def is_action_allowed(cls, user, action=None, context=None):
        return False
