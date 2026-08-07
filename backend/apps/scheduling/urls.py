from django.urls import path
from rest_framework.routers import DefaultRouter

from backend.apps.scheduling.views import SectionCountRecommendationView, TimeSlotViewSet

router = DefaultRouter()
router.register("timeslots", TimeSlotViewSet, basename="timeslot")

urlpatterns = [
    path(
        "planning/section-count-recommendations/",
        SectionCountRecommendationView.as_view(),
        name="section-count-recommendations",
    ),
] + router.urls
