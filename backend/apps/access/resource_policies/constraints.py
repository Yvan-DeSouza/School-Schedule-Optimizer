from backend.apps.access.resource_policies.base import BaseResourcePolicy
from backend.apps.access.rules import AccessRule
from backend.apps.access.scopes import ReadScope, WriteScope
from backend.apps.people.models import RoleChoices


class PlanningResourcePolicy(BaseResourcePolicy):
    """Shared planning records are managed only by planning roles."""

    rules = {
        RoleChoices.COUNSELOR: AccessRule(read=ReadScope.ALL, write=WriteScope.ALL),
        RoleChoices.STAFF: AccessRule(read=ReadScope.ALL, write=WriteScope.ALL),
        RoleChoices.DIRECTOR: AccessRule(read=ReadScope.ALL, write=WriteScope.ALL),
    }


class TeacherOwnedResourcePolicy(PlanningResourcePolicy):
    """Planning roles manage all; teachers manage their own nested records."""

    rules = {
        **PlanningResourcePolicy.rules,
        RoleChoices.TEACHER: AccessRule(read=ReadScope.OWN, write=WriteScope.OWN),
    }

    @classmethod
    def filter_own_queryset(cls, user, queryset):
        return queryset.filter(teacher__user=user)

    @classmethod
    def is_own_object(cls, user, obj):
        return getattr(getattr(obj, "teacher", None), "user_id", None) == user.id

    @classmethod
    def can_create(cls, user, data=None, context=None):
        rule = cls.rule_for(user)
        if rule.write != WriteScope.OWN:
            # Planning roles retain the base all-write behavior.
            return super().can_create(user, data, context=context)
        # For teacher writes, trust the URL-derived teacher user ID—not request
        # JSON—to prevent creating records under another teacher.
        return bool(context and context.get("teacher_user_id") == user.id)
