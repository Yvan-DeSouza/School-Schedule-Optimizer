from backend.apps.access.action_policies.base import BaseActionPolicy
from backend.apps.access.rules import ActionRule
from backend.apps.access.scopes import ActionScope
from backend.apps.people.models import RoleChoices


class DemandPlanningAction:
    VIEW_DEMAND_SUMMARY = "view_demand_summary"
    RECOMMEND_COURSE_CLOSURES = "recommend_course_closures"
    RECOMMEND_SECTION_COUNTS = "recommend_section_counts"
    RUN_SECTION_PLANNING = "run_section_planning"
    APPROVE_SECTION_PLAN = "approve_section_plan"
    MANAGE_PLANNING_CONFIGURATION = "manage_planning_configuration"


class DemandPlanningActionPolicy(BaseActionPolicy):
    allowed_actions = {
        DemandPlanningAction.VIEW_DEMAND_SUMMARY,
        DemandPlanningAction.RECOMMEND_COURSE_CLOSURES,
        DemandPlanningAction.RECOMMEND_SECTION_COUNTS,
        DemandPlanningAction.RUN_SECTION_PLANNING,
        DemandPlanningAction.APPROVE_SECTION_PLAN,
        DemandPlanningAction.MANAGE_PLANNING_CONFIGURATION,
    }
    rules = {
        RoleChoices.COUNSELOR: ActionRule(execute=ActionScope.ALLOWED),
        RoleChoices.STAFF: ActionRule(execute=ActionScope.ALLOWED),
        RoleChoices.DIRECTOR: ActionRule(execute=ActionScope.ALLOWED),
    }

    @classmethod
    def is_action_allowed(cls, user, action=None, context=None):
        return action in cls.allowed_actions
