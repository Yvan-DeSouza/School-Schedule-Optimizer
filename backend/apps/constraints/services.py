"""Reusable qualification rules shared by APIs and future scheduling work."""

from rest_framework.exceptions import ValidationError

from backend.apps.common.constants import (
    QUALIFICATION_ENFORCEMENT_REQUIRED,
    STATUTORY_TEACHABLE_MIN_GRADE,
)
from backend.apps.constraints.models import CourseQualificationRequirement, TeacherQualification


def course_requires_statutory_qualification(course):
    """Return whether a course needs a legally required teachable credential."""
    return course.grade_level >= STATUTORY_TEACHABLE_MIN_GRADE


def required_qualification_ids_for_course(course):
    """Return the hard qualification ids for a course, never its preferences."""
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
        return True, set()

    required_ids = required_qualification_ids_for_course(course)
    if not required_ids:
        return False, set()
    held_ids = set(
        TeacherQualification.objects.filter(teacher=teacher).values_list("qualification_id", flat=True)
    )
    return required_ids <= held_ids, required_ids - held_ids


def validate_teacher_course_assignment(course, teacher, field_name="teacher"):
    """Raise a field-specific API error when a teacher cannot teach a course."""
    if teacher is None or not course_requires_statutory_qualification(course):
        return

    eligible, missing = teacher_meets_course_qualification_requirements(teacher, course)
    if eligible:
        return
    if not missing:
        raise ValidationError({
            field_name: (
                "This Grade 11-12 course has no required senior teachable qualification "
                "configured, so a teacher cannot be assigned yet."
            )
        })
    raise ValidationError({
        field_name: "This teacher lacks a required qualification for the Grade 11-12 course."
    })


def validate_locked_teacher_qualifications(section, teacher):
    """Validate a section lock using the same rule as direct assignments."""
    validate_teacher_course_assignment(section.course, teacher, field_name="locked_teacher")
