"""Routes for planning configuration, immutable runs, and timeslots."""

from django.urls import path
from rest_framework.routers import DefaultRouter

from backend.apps.scheduling.views import (
    CapacityProfileViewSet,
    CourseCapacityPolicyView,
    CoursePriorityProfileViewSet,
    SectionCountRecommendationView,
    SectionPlanningRunViewSet,
    SectionPlacementRunViewSet,
    SectionBudgetRunViewSet,
    StaffingPlanRunViewSet,
    TeacherPlanningCapacityViewSet,
    TeacherPlanningAnnualCapacityViewSet,
    TeacherCourseAssignmentRuleViewSet,
    TeacherTimePreferenceViewSet,
    TeacherAssignmentRunViewSet,
    TeacherPlanningRosterViewSet,
    TimeSlotViewSet,
    AnnualPlacementLockViewSet,
)

router = DefaultRouter()
# ViewSet @actions automatically add run review/preview/approve detail routes.
router.register("timeslots", TimeSlotViewSet, basename="timeslot")
router.register("planning/capacity-profiles", CapacityProfileViewSet, basename="capacity-profile")
router.register("planning/course-priority-profiles", CoursePriorityProfileViewSet, basename="course-priority-profile")
router.register("planning/teacher-capacities", TeacherPlanningCapacityViewSet, basename="teacher-planning-capacity")
router.register("planning/teacher-annual-capacities", TeacherPlanningAnnualCapacityViewSet, basename="teacher-planning-annual-capacity")
router.register("planning/teacher-course-assignment-rules", TeacherCourseAssignmentRuleViewSet, basename="teacher-course-assignment-rule")
router.register("planning/teacher-time-preferences", TeacherTimePreferenceViewSet, basename="teacher-time-preference")
router.register("planning/teacher-rosters", TeacherPlanningRosterViewSet, basename="teacher-planning-roster")
router.register("planning/annual-placement-locks", AnnualPlacementLockViewSet, basename="annual-placement-lock")
router.register("planning/section-count-runs", SectionPlanningRunViewSet, basename="section-planning-run")
router.register("planning/section-placement-runs", SectionPlacementRunViewSet, basename="section-placement-run")
router.register("planning/teacher-assignment-runs", TeacherAssignmentRunViewSet, basename="teacher-assignment-run")
router.register("planning/section-budget-runs", SectionBudgetRunViewSet, basename="section-budget-run")
router.register("planning/staffing-runs", StaffingPlanRunViewSet, basename="staffing-plan-run")

urlpatterns = [
    # Preserve the older heuristic endpoint beside the newer CP-SAT run API.
    path(
        "planning/section-count-recommendations/",
        SectionCountRecommendationView.as_view(),
        name="section-count-recommendations",
    ),
    path("courses/<int:course_id>/capacity-policy/", CourseCapacityPolicyView.as_view(), name="course-capacity-policy"),
] + router.urls
