from backend.apps.access.action_policies.base import BaseActionPolicy
from backend.apps.access.rules import ActionRule
from backend.apps.access.scopes import ActionScope
from backend.apps.people.models import RoleChoices


class DemandPlanningAction:
    VIEW_DEMAND_SUMMARY = "view_demand_summary"
    RECOMMEND_COURSE_CLOSURES = "recommend_course_closures"
    RECOMMEND_SECTION_COUNTS = "recommend_section_counts"


class DemandPlanningActionPolicy(BaseActionPolicy):
    allowed_actions = {
        DemandPlanningAction.VIEW_DEMAND_SUMMARY,
        DemandPlanningAction.RECOMMEND_COURSE_CLOSURES,
        DemandPlanningAction.RECOMMEND_SECTION_COUNTS,
    }
    rules = {
        RoleChoices.COUNSELOR: ActionRule(execute=ActionScope.ALLOWED),
        RoleChoices.STAFF: ActionRule(execute=ActionScope.ALLOWED),
        RoleChoices.DIRECTOR: ActionRule(execute=ActionScope.ALLOWED),
    }

    @classmethod
    def is_action_allowed(cls, user, action=None, context=None):
        return action in cls.allowed_actions
