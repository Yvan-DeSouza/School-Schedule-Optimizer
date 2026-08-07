from rest_framework.routers import DefaultRouter

from backend.apps.scheduling.views import TimeSlotViewSet

router = DefaultRouter()
router.register("timeslots", TimeSlotViewSet, basename="timeslot")

urlpatterns = router.urls
