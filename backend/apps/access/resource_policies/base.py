from backend.apps.access.rules import AccessRule
from backend.apps.access.scopes import ReadScope, WriteScope
from backend.apps.people.models import RoleChoices
from backend.apps.people.roles import get_user_role


class BaseResourcePolicy:
    rules = {}

    @classmethod
    def rule_for(cls, user):
        if not user or not user.is_authenticated:
            return AccessRule()

        role = get_user_role(user)
        if role == RoleChoices.UNKNOWN:
            return AccessRule()

        return cls.rules.get(role, AccessRule())

    @classmethod
    def filter_read_queryset(cls, user, queryset):
        rule = cls.rule_for(user)

        if rule.read == ReadScope.ALL:
            return queryset
        if rule.read == ReadScope.OWN:
            return cls.filter_own_queryset(user, queryset)
        if rule.read == ReadScope.ASSIGNED:
            return cls.filter_assigned_queryset(user, queryset)
        return queryset.none()

    @classmethod
    def can_read_object(cls, user, obj):
        rule = cls.rule_for(user)

        if rule.read == ReadScope.ALL:
            return True
        if rule.read == ReadScope.OWN:
            return cls.is_own_object(user, obj)
        if rule.read == ReadScope.ASSIGNED:
            return cls.is_assigned_object(user, obj)
        return False

    @classmethod
    def can_create(cls, user, data=None):
        return cls.rule_for(user).write != WriteScope.NONE

    @classmethod
    def can_write_object(cls, user, obj):
        rule = cls.rule_for(user)

        if rule.write == WriteScope.ALL:
            return True
        if rule.write == WriteScope.OWN:
            return cls.is_own_object(user, obj)
        if rule.write == WriteScope.ASSIGNED:
            return cls.is_assigned_object(user, obj)
        return False

    @classmethod
    def can_delete_object(cls, user, obj):
        return cls.can_write_object(user, obj)

    @classmethod
    def filter_own_queryset(cls, user, queryset):
        return queryset.none()

    @classmethod
    def filter_assigned_queryset(cls, user, queryset):
        return queryset.none()

    @classmethod
    def is_own_object(cls, user, obj):
        return False

    @classmethod
    def is_assigned_object(cls, user, obj):
        return False
