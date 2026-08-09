"""Authorization for counselor-controlled student-assignment locks."""

from backend.apps.access.action_policies.base import BaseActionPolicy
from backend.apps.access.rules import ActionRule
from backend.apps.access.scopes import ActionScope
from backend.apps.people.models import RoleChoices
from backend.apps.people.roles import get_user_role


class StudentAssignmentLockAction:
    """Stable names for each lock operation and lock type combination."""

    CREATE_EXACT_SECTION_LOCK = "create_student_assignment_exact_section_lock"
    RELEASE_EXACT_SECTION_LOCK = "release_student_assignment_exact_section_lock"
    VIEW_EXACT_SECTION_LOCK = "view_student_assignment_exact_section_lock"
    CREATE_WHOLE_SCHEDULE_LOCK = "create_student_assignment_whole_schedule_lock"
    RELEASE_WHOLE_SCHEDULE_LOCK = "release_student_assignment_whole_schedule_lock"
    VIEW_WHOLE_SCHEDULE_LOCK = "view_student_assignment_whole_schedule_lock"
    CREATE_SECTION_ROSTER_FREEZE = "create_student_assignment_section_roster_freeze"
    RELEASE_SECTION_ROSTER_FREEZE = "release_student_assignment_section_roster_freeze"
    VIEW_SECTION_ROSTER_FREEZE = "view_student_assignment_section_roster_freeze"
    CREATE_COURSE_ROSTER_FREEZE = "create_student_assignment_course_roster_freeze"
    RELEASE_COURSE_ROSTER_FREEZE = "release_student_assignment_course_roster_freeze"
    VIEW_COURSE_ROSTER_FREEZE = "view_student_assignment_course_roster_freeze"
    CREATE_STUDENT_GROUP_LOCK = "create_student_assignment_student_group_lock"
    RELEASE_STUDENT_GROUP_LOCK = "release_student_assignment_student_group_lock"
    VIEW_STUDENT_GROUP_LOCK = "view_student_assignment_student_group_lock"
    CREATE_STUDENT_TEACHER_LOCK = "create_student_assignment_student_teacher_lock"
    RELEASE_STUDENT_TEACHER_LOCK = "release_student_assignment_student_teacher_lock"
    VIEW_STUDENT_TEACHER_LOCK = "view_student_assignment_student_teacher_lock"


class StudentAssignmentLockActionPolicy(BaseActionPolicy):
    """Counselors/directors change locks; planning roles may inspect them."""

    write_actions = {
        StudentAssignmentLockAction.CREATE_EXACT_SECTION_LOCK,
        StudentAssignmentLockAction.RELEASE_EXACT_SECTION_LOCK,
        StudentAssignmentLockAction.CREATE_WHOLE_SCHEDULE_LOCK,
        StudentAssignmentLockAction.RELEASE_WHOLE_SCHEDULE_LOCK,
        StudentAssignmentLockAction.CREATE_SECTION_ROSTER_FREEZE,
        StudentAssignmentLockAction.RELEASE_SECTION_ROSTER_FREEZE,
        StudentAssignmentLockAction.CREATE_COURSE_ROSTER_FREEZE,
        StudentAssignmentLockAction.RELEASE_COURSE_ROSTER_FREEZE,
        StudentAssignmentLockAction.CREATE_STUDENT_GROUP_LOCK,
        StudentAssignmentLockAction.RELEASE_STUDENT_GROUP_LOCK,
        StudentAssignmentLockAction.CREATE_STUDENT_TEACHER_LOCK,
        StudentAssignmentLockAction.RELEASE_STUDENT_TEACHER_LOCK,
    }
    query_actions = {
        StudentAssignmentLockAction.VIEW_EXACT_SECTION_LOCK,
        StudentAssignmentLockAction.VIEW_WHOLE_SCHEDULE_LOCK,
        StudentAssignmentLockAction.VIEW_SECTION_ROSTER_FREEZE,
        StudentAssignmentLockAction.VIEW_COURSE_ROSTER_FREEZE,
        StudentAssignmentLockAction.VIEW_STUDENT_GROUP_LOCK,
        StudentAssignmentLockAction.VIEW_STUDENT_TEACHER_LOCK,
    }
    allowed_actions = write_actions | query_actions
    rules = {
        RoleChoices.COUNSELOR: ActionRule(execute=ActionScope.ALLOWED),
        RoleChoices.STAFF: ActionRule(execute=ActionScope.ALLOWED),
        RoleChoices.DIRECTOR: ActionRule(execute=ActionScope.ALLOWED),
    }

    @classmethod
    def is_action_allowed(cls, user, action=None, context=None):
        if action in cls.query_actions:
            # Staff can monitor planning decisions, but they cannot create or
            # release a counselor-owned student-assignment decision.
            return True
        if action in cls.write_actions:
            return get_user_role(user) in {RoleChoices.COUNSELOR, RoleChoices.DIRECTOR}
        return False
