"""Routes for shared constraints, teacher-owned data, and section locks."""

from django.urls import path
from rest_framework.routers import DefaultRouter

from backend.apps.constraints.views import (
    CounselorConstraintPreferenceViewSet, CourseConflictViewSet,
    CourseQualificationRequirementViewSet, CourseRoomRequirementViewSet,
    HardConstraintViewSet, QualificationViewSet, SectionLockView, SoftConstraintViewSet,
    TeacherAvailabilityViewSet, TeacherCoursePreferenceViewSet, TeacherCurrentCourseViewSet,
    TeacherQualificationViewSet,
)

router = DefaultRouter()
# School-wide resources use standard router-generated CRUD routes.
router.register("qualifications", QualificationViewSet, basename="qualification")
router.register("constraints/hard", HardConstraintViewSet, basename="hard-constraint")
router.register("constraints/soft", SoftConstraintViewSet, basename="soft-constraint")
router.register("constraints/preferences", CounselorConstraintPreferenceViewSet, basename="constraint-preference")
router.register("course-conflicts", CourseConflictViewSet, basename="course-conflict")
router.register("course-room-requirements", CourseRoomRequirementViewSet, basename="course-room-requirement")
router.register("course-qualification-requirements", CourseQualificationRequirementViewSet, basename="course-qualification-requirement")

# Teacher-owned records use explicit nested URLs so the parent teacher identity
# can drive ownership policy and serializer context.
teacher_resources = [
    ("qualifications", TeacherQualificationViewSet, "teacher-qualification"),
    ("preferences", TeacherCoursePreferenceViewSet, "teacher-preference"),
    ("current-courses", TeacherCurrentCourseViewSet, "teacher-current-course"),
    ("availability", TeacherAvailabilityViewSet, "teacher-availability"),
]

urlpatterns = [
    path("sections/<int:section_id>/lock/", SectionLockView.as_view(), name="section-lock"),
]
for prefix, viewset, basename in teacher_resources:
    # Bind only the intended CRUD actions rather than mounting a second router
    # with ambiguous parent lookup behavior.
    urlpatterns += [
        path(f"teachers/<int:teacher_id>/{prefix}/", viewset.as_view({"get": "list", "post": "create"}), name=f"{basename}-list"),
        path(f"teachers/<int:teacher_id>/{prefix}/<int:pk>/", viewset.as_view({"get": "retrieve", "patch": "partial_update", "put": "update", "delete": "destroy"}), name=f"{basename}-detail"),
    ]
urlpatterns += router.urls
