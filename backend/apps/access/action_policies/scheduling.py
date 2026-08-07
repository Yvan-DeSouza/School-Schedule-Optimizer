from backend.apps.access.action_policies.base import BaseActionPolicy
from backend.apps.access.rules import ActionRule
from backend.apps.access.scopes import ActionScope
from backend.apps.people.models import RoleChoices
from backend.apps.people.roles import get_user_role


class SchedulingAction:
    RUN_SECTION_PLACEMENT = "run_section_placement"
    RUN_TEACHER_ASSIGNMENT = "run_teacher_assignment"
    RUN_STUDENT_ASSIGNMENT = "run_student_assignment"
    VIEW_SCHEDULING_RUN_STATUS = "view_scheduling_run_status"


class SchedulingActionPolicy(BaseActionPolicy):
    solver_run_actions = {
        SchedulingAction.RUN_SECTION_PLACEMENT,
        SchedulingAction.RUN_TEACHER_ASSIGNMENT,
        SchedulingAction.RUN_STUDENT_ASSIGNMENT,
    }
    status_actions = {SchedulingAction.VIEW_SCHEDULING_RUN_STATUS}
    rules = {
        RoleChoices.COUNSELOR: ActionRule(execute=ActionScope.ALLOWED),
        RoleChoices.STAFF: ActionRule(execute=ActionScope.ALLOWED),
        RoleChoices.DIRECTOR: ActionRule(execute=ActionScope.ALLOWED),
    }

    @classmethod
    def is_action_allowed(cls, user, action=None, context=None):
        if action in cls.status_actions:
            return True
        if action in cls.solver_run_actions:
            return get_user_role(user) in {RoleChoices.COUNSELOR, RoleChoices.DIRECTOR}
        return False
