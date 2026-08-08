"""Planning-role teacher-directory routes."""

from rest_framework.routers import DefaultRouter

from backend.apps.people.views import TeacherViewSet


router = DefaultRouter()
router.register("teachers", TeacherViewSet, basename="teacher")

urlpatterns = router.urls
