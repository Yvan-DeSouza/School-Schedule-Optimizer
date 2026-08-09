"""Planning-role teacher-directory routes."""

from rest_framework.routers import DefaultRouter

from backend.apps.people.views import StudentRosterViewSet, TeacherViewSet


router = DefaultRouter()
router.register("teachers", TeacherViewSet, basename="teacher")
router.register("students", StudentRosterViewSet, basename="student")

urlpatterns = router.urls
