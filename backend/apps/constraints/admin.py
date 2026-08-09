"""Django admin registration for normalized constraint records."""

from django.contrib import admin

from backend.apps.constraints.models.base import (
    CounselorConstraintPreference,
    HardConstraint,
    Qualification,
    SoftConstraint,
)
from backend.apps.constraints.models.course import (
    CourseConflict,
    CourseConflictAdjustment,
    CourseConflictMatrix,
    CourseQualificationRequirement,
    CourseRoomRequirement,
)
from backend.apps.constraints.models.teacher import (
    TeacherAvailability,
    TeacherCoursePreference,
    TeacherCurrentCourse,
    TeacherQualification,
)


# Default ModelAdmin behavior is sufficient for development inspection; public
# API authorization remains independent of Django admin access.
admin.site.register([
    HardConstraint,
    SoftConstraint,
    CounselorConstraintPreference,
    Qualification,
    TeacherQualification,
    TeacherCoursePreference,
    TeacherAvailability,
    TeacherCurrentCourse,
    CourseRoomRequirement,
    CourseQualificationRequirement,
    CourseConflict,
    CourseConflictMatrix,
    CourseConflictAdjustment,
])
