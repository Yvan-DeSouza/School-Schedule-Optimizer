"""Routes for catalog, sections, student requests, and raw demand summary."""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from backend.apps.courses.views import CourseRequestViewSet, CourseViewSet, DemandSummaryView, SectionViewSet

router = DefaultRouter()
# Standard CRUD viewsets share one router; demand summary is a named action view.
router.register("courses", CourseViewSet, basename="course")
router.register("sections", SectionViewSet, basename="section")
router.register("course-requests", CourseRequestViewSet, basename="course-request")

urlpatterns = [
    path("demand/summary/", DemandSummaryView.as_view(), name="demand-summary"),
    path("", include(router.urls)),
]
