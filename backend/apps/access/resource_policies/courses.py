"""Resource policies for courses, offered sections, and student requests."""

from backend.apps.access.resource_policies.base import BaseResourcePolicy
from backend.apps.access.rules import AccessRule
from backend.apps.access.scopes import ReadScope, WriteScope
from backend.apps.people.models import RoleChoices


class CoursePolicy(BaseResourcePolicy):
    """Catalog is broadly readable; planning roles alone may mutate it."""

    rules = {
        RoleChoices.STUDENT: AccessRule(read=ReadScope.ALL),
        RoleChoices.TEACHER: AccessRule(read=ReadScope.ALL),
        RoleChoices.COUNSELOR: AccessRule(read=ReadScope.ALL, write=WriteScope.ALL),
        RoleChoices.STAFF: AccessRule(read=ReadScope.ALL, write=WriteScope.ALL),
        RoleChoices.DIRECTOR: AccessRule(read=ReadScope.ALL, write=WriteScope.ALL),
        RoleChoices.UNKNOWN: AccessRule(),
    }


class SectionPolicy(BaseResourcePolicy):
    """Teachers see assigned sections; planning roles manage all sections."""

    rules = {
        RoleChoices.STUDENT: AccessRule(),
        RoleChoices.TEACHER: AccessRule(read=ReadScope.ASSIGNED),
        RoleChoices.COUNSELOR: AccessRule(read=ReadScope.ALL, write=WriteScope.ALL),
        RoleChoices.STAFF: AccessRule(read=ReadScope.ALL, write=WriteScope.ALL),
        RoleChoices.DIRECTOR: AccessRule(read=ReadScope.ALL, write=WriteScope.ALL),
        RoleChoices.UNKNOWN: AccessRule(),
    }

    @classmethod
    def filter_assigned_queryset(cls, user, queryset):
        # Queryset and object predicate below must express the same ownership.
        return queryset.filter(teacher__user=user)

    @classmethod
    def is_assigned_object(cls, user, obj):
        return getattr(getattr(obj, "teacher", None), "user_id", None) == user.id


class CourseRequestPolicy(BaseResourcePolicy):
    """Students manage only their own demand; planning roles manage all."""

    rules = {
        RoleChoices.STUDENT: AccessRule(read=ReadScope.OWN, write=WriteScope.OWN),
        RoleChoices.TEACHER: AccessRule(),
        RoleChoices.COUNSELOR: AccessRule(read=ReadScope.ALL, write=WriteScope.ALL),
        RoleChoices.STAFF: AccessRule(read=ReadScope.ALL, write=WriteScope.ALL),
        RoleChoices.DIRECTOR: AccessRule(read=ReadScope.ALL, write=WriteScope.ALL),
        RoleChoices.UNKNOWN: AccessRule(),
    }

    @classmethod
    def filter_own_queryset(cls, user, queryset):
        return queryset.filter(student__user=user)

    @classmethod
    def is_own_object(cls, user, obj):
        return getattr(getattr(obj, "student", None), "user_id", None) == user.id


class StudentScheduleCommitmentRequestPolicy(BaseResourcePolicy):
    """Special-program requests are planning records, not self-service demand.

    The current release treats every stored Study or Focus request as already
    counselor-authorized.  Restricting writes to planning roles makes that
    temporary policy enforceable rather than merely a convention in the UI.
    """

    rules = {
        RoleChoices.COUNSELOR: AccessRule(read=ReadScope.ALL, write=WriteScope.ALL),
        RoleChoices.STAFF: AccessRule(read=ReadScope.ALL, write=WriteScope.ALL),
        RoleChoices.DIRECTOR: AccessRule(read=ReadScope.ALL, write=WriteScope.ALL),
        RoleChoices.UNKNOWN: AccessRule(),
    }
