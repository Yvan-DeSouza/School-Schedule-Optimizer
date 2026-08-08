"""Django admin registration for course-domain records."""

from django.contrib import admin

from backend.apps.courses.models import (
    Course,
    CoursePrerequisite,
    CourseRequest,
    Enrollment,
    Section,
)


admin.site.register([
    Course,
    Section,
    Enrollment,
    CourseRequest,
    CoursePrerequisite,
])
