"""Authorization for counselor-controlled special student commitments."""

from backend.apps.access.action_policies.base import BaseActionPolicy
from backend.apps.access.rules import ActionRule
from backend.apps.access.scopes import ActionScope
from backend.apps.people.models import RoleChoices
from backend.apps.people.roles import get_user_role


class StudentSpecialCommitmentLockAction:
    """Stable semantic names for Study, online, Co-op, and Focus lock actions."""

    CREATE = "create_student_special_commitment_lock"
    RELEASE = "release_student_special_commitment_lock"
    VIEW = "view_student_special_commitment_lock"


class StudentSpecialCommitmentLockActionPolicy(BaseActionPolicy):
    """Counselors/directors decide restrictions; staff may inspect them."""

    write_actions = {
        StudentSpecialCommitmentLockAction.CREATE,
        StudentSpecialCommitmentLockAction.RELEASE,
    }
    rules = {
        RoleChoices.COUNSELOR: ActionRule(execute=ActionScope.ALLOWED),
        RoleChoices.STAFF: ActionRule(execute=ActionScope.ALLOWED),
        RoleChoices.DIRECTOR: ActionRule(execute=ActionScope.ALLOWED),
    }

    @classmethod
    def is_action_allowed(cls, user, action=None, context=None):
        if action == StudentSpecialCommitmentLockAction.VIEW:
            return True
        if action in cls.write_actions:
            return get_user_role(user) in {RoleChoices.COUNSELOR, RoleChoices.DIRECTOR}
        return False
