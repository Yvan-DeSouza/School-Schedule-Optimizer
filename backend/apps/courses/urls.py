"""Routes for catalog, sections, student requests, and raw demand summary."""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from backend.apps.courses.views import (
    CombinationSuggestionView,
    CombineOfferingsView,
    CourseCombinationRuleViewSet,
    CourseCategoryRelationshipViewSet,
    CourseOfferingViewSet,
    CoursePrerequisiteViewSet,
    CourseRequestViewSet,
    CourseSequencePreferenceViewSet,
    CourseViewSet,
    DeliveryGroupViewSet,
    DemandSummaryView,
    SectionViewSet,
)

router = DefaultRouter()
# Standard CRUD viewsets share one router; demand summary is a named action view.
router.register("courses", CourseViewSet, basename="course")
router.register(
    "planning/course-category-relationships",
    CourseCategoryRelationshipViewSet,
    basename="course-category-relationship",
)
router.register("sections", SectionViewSet, basename="section")
router.register("course-requests", CourseRequestViewSet, basename="course-request")
router.register("course-prerequisites", CoursePrerequisiteViewSet, basename="course-prerequisite")
router.register("course-sequence-preferences", CourseSequencePreferenceViewSet, basename="course-sequence-preference")
router.register("planning/course-offerings", CourseOfferingViewSet, basename="course-offering")
router.register("planning/combination-rules", CourseCombinationRuleViewSet, basename="combination-rule")
router.register("planning/delivery-groups", DeliveryGroupViewSet, basename="delivery-group")

urlpatterns = [
    path("demand/summary/", DemandSummaryView.as_view(), name="demand-summary"),
    path("planning/combination-suggestions/", CombinationSuggestionView.as_view(), name="combination-suggestions"),
    path("planning/combine-offerings/", CombineOfferingsView.as_view(), name="combine-offerings"),
    path("", include(router.urls)),
]
