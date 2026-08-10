"""Django admin registration for course-domain records."""

from django.contrib import admin

from backend.apps.courses.models import (
    Course,
    CourseCategoryRelationship,
    CourseCombinationRule,
    CourseCombinationRuleMember,
    CourseOffering,
    CourseOfferingDecision,
    CoursePrerequisite,
    CourseSequencePreference,
    CourseRequest,
    Enrollment,
    DeliveryGroup,
    Section,
)


@admin.register(Section)
class SectionAdmin(admin.ModelAdmin):
    """Make active/retired status and planning provenance visible to operators."""

    list_display = (
        "course",
        "section_number",
        "academic_year",
        "semester",
        "lifecycle_status",
        "teacher",
        "is_locked",
    )
    list_filter = ("academic_year", "semester", "lifecycle_status", "is_locked")
    search_fields = ("course__course_code", "section_number")


admin.site.register([
    Course,
    CourseCategoryRelationship,
    Enrollment,
    CourseRequest,
    CoursePrerequisite,
    CourseSequencePreference,
    CourseOffering,
    CourseOfferingDecision,
    DeliveryGroup,
    CourseCombinationRule,
    CourseCombinationRuleMember,
])
