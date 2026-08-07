from rest_framework.exceptions import ValidationError

from backend.apps.constraints.models import CourseQualificationRequirement, TeacherQualification


def validate_locked_teacher_qualifications(section, teacher):
    """Ensure a manually locked teacher meets every course qualification requirement."""
    if teacher is None:
        return
    required_ids = set(
        CourseQualificationRequirement.objects.filter(course=section.course).values_list(
            "qualification_id", flat=True
        )
    )
    held_ids = set(
        TeacherQualification.objects.filter(teacher=teacher).values_list("qualification_id", flat=True)
    )
    missing = required_ids - held_ids
    if missing:
        raise ValidationError({"locked_teacher": "This teacher lacks a required qualification for the section course."})
