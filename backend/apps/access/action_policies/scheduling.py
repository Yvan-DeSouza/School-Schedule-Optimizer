"""Authorization for future timetable solver execution and status viewing."""

from backend.apps.access.action_policies.base import BaseActionPolicy
from backend.apps.access.rules import ActionRule
from backend.apps.access.scopes import ActionScope
from backend.apps.people.models import RoleChoices
from backend.apps.people.roles import get_user_role


class SchedulingAction:
    """Stable names for the three downstream solver stages and status."""

    RUN_SECTION_PLACEMENT = "run_section_placement"
    APPROVE_SECTION_PLACEMENT = "approve_section_placement"
    RUN_TEACHER_ASSIGNMENT = "run_teacher_assignment"
    APPROVE_TEACHER_ASSIGNMENT = "approve_teacher_assignment"
    RUN_STUDENT_ASSIGNMENT = "run_student_assignment"
    VIEW_SCHEDULING_RUN_STATUS = "view_scheduling_run_status"


class SchedulingActionPolicy(BaseActionPolicy):
    """Staff may monitor runs; counselor/director may start solver work."""

    solver_run_actions = {
        SchedulingAction.RUN_SECTION_PLACEMENT,
        SchedulingAction.RUN_TEACHER_ASSIGNMENT,
        SchedulingAction.RUN_STUDENT_ASSIGNMENT,
    }
    approval_actions = {
        SchedulingAction.APPROVE_SECTION_PLACEMENT,
        SchedulingAction.APPROVE_TEACHER_ASSIGNMENT,
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
            # The base role rule already limits this to planning roles.
            return True
        if action in cls.solver_run_actions:
            # Starting a schedule-changing solve is narrower than viewing status.
            return get_user_role(user) in {RoleChoices.COUNSELOR, RoleChoices.DIRECTOR}
        if action in cls.approval_actions:
            return get_user_role(user) in {RoleChoices.COUNSELOR, RoleChoices.DIRECTOR}
        return False
