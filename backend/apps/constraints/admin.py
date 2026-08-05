from django.contrib import admin

from backend.apps.constraints.models.base import (
    CounselorConstraintPreference,
    HardConstraint,
    Qualification,
    SoftConstraint,
)
from backend.apps.constraints.models.course import (
    CourseConflict,
    CourseQualificationRequirement,
    CourseRoomRequirement,
)
from backend.apps.constraints.models.teacher import (
    TeacherAvailability,
    TeacherCoursePreference,
    TeacherCurrentCourse,
    TeacherQualification,
)


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
])
