"""Router-generated endpoints for academic years and rooms."""

from rest_framework.routers import DefaultRouter

from backend.apps.common.views import AcademicYearViewSet, RoomViewSet

router = DefaultRouter()
router.register("academic-years", AcademicYearViewSet, basename="academic-year")
router.register("rooms", RoomViewSet, basename="room")

urlpatterns = router.urls
