"""Reusable qualification rules shared by APIs and future scheduling work."""

from rest_framework.exceptions import ValidationError

from backend.apps.common.constants import (
    QUALIFICATION_ENFORCEMENT_REQUIRED,
    QUALIFICATION_REVIEW_VERIFIED,
    STATUTORY_TEACHABLE_MIN_GRADE,
)
from backend.apps.constraints.models import CourseQualificationRequirement, TeacherQualification


def course_requires_statutory_qualification(course):
    """Return whether a course needs a legally required teachable credential."""
    # Keep the grade threshold centralized in common constants; do not duplicate
    # specific grade numbers in serializers, models, or solvers.
    return course.grade_level >= STATUTORY_TEACHABLE_MIN_GRADE


def required_qualification_ids_for_course(course):
    """Return the hard qualification ids for a course, never its preferences."""
    # Preferred mappings may guide later assignment objectives but cannot satisfy
    # or create a legal eligibility requirement.
    return set(
        CourseQualificationRequirement.objects.filter(
            course=course,
            enforcement=QUALIFICATION_ENFORCEMENT_REQUIRED,
        ).values_list("qualification_id", flat=True)
    )


def teacher_meets_course_qualification_requirements(teacher, course):
    """Apply the legal Grade 11-12 rule and report missing credential ids.

    Grades 7-10 deliberately return eligible even when preferred qualifications
    exist. Grade 11-12 fail closed if planning data has no hard requirement.
    """
    if teacher is None or not course_requires_statutory_qualification(course):
        # Unassigned sections and legally flexible Grade 7-10 courses pass this
        # hard-rule check. Preferences are handled elsewhere.
        return True, set()

    required_ids = required_qualification_ids_for_course(course)
    if not required_ids:
        # Senior courses fail closed when administrators have not configured a
        # normalized required teachable.
        return False, set()
    # Compare normalized IDs only; raw Aspen strings are provenance, not rules.
    held_ids = set(
        TeacherQualification.objects.filter(
            teacher=teacher,
            review_status=QUALIFICATION_REVIEW_VERIFIED,
        ).values_list("qualification_id", flat=True)
    )
    return required_ids <= held_ids, required_ids - held_ids


def validate_teacher_course_assignment(course, teacher, field_name="teacher"):
    """Raise a field-specific API error when a teacher cannot teach a course."""
    if teacher is not None and teacher.is_archived:
        raise ValidationError({field_name: "An archived teacher cannot receive a new assignment."})
    if teacher is None or not course_requires_statutory_qualification(course):
        return

    eligible, missing = teacher_meets_course_qualification_requirements(teacher, course)
    if eligible:
        return
    if not missing:
        # Empty missing set plus ineligible means configuration is absent, not
        # that the selected teacher lacks a particular known credential.
        raise ValidationError({
            field_name: (
                "This Grade 11-12 course has no required senior teachable qualification "
                "configured, so a teacher cannot be assigned yet."
            )
        })
    raise ValidationError({
        field_name: "This teacher lacks a required qualification for the Grade 11-12 course."
    })


def validate_teacher_delivery_group_assignment(delivery_group, teacher, field_name="teacher"):
    """Require one teacher to satisfy every course in a physical delivery."""

    if teacher is None:
        return
    courses = [
        offering.course
        for offering in delivery_group.offerings.select_related("course").all()
    ]
    if not courses:
        raise ValidationError({field_name: "The delivery group has no active course offering."})
    for course in courses:
        try:
            validate_teacher_course_assignment(course, teacher, field_name=field_name)
        except ValidationError as error:
            raise ValidationError({
                field_name: (
                    f"This teacher cannot cover every member of the physical class; "
                    f"{course.course_code} failed its qualification rule."
                )
            }) from error


def validate_locked_teacher_qualifications(section, teacher):
    """Validate a section lock using the same rule as direct assignments."""
    # Central delegation prevents Section PATCH and SectionLock PATCH from
    # developing subtly different legal behavior.
    if section.delivery_group_id:
        validate_teacher_delivery_group_assignment(
            section.delivery_group,
            teacher,
            field_name="locked_teacher",
        )
    else:
        validate_teacher_course_assignment(section.course, teacher, field_name="locked_teacher")
