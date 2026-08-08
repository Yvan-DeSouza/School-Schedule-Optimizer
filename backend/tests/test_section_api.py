"""Section visibility, assignment qualification validation, and filtering."""

import pytest

from backend.apps.common.constants import (
    QUALIFICATION_DIVISION_SENIOR,
    QUALIFICATION_REVIEW_VERIFIED,
    QUALIFICATION_SUBJECT_MATHEMATICS,
    SEMESTER_FALL,
    SEMESTER_WINTER,
)
from backend.apps.constraints.models import CourseQualificationRequirement, Qualification, TeacherQualification
from backend.apps.courses.models import Section


@pytest.mark.django_db
def test_section_visibility_writes_and_filters(authenticated_client, course, academic_year, teacher_user, second_teacher_user, counselor_user, student_user):
    mine = Section.objects.create(course=course, section_number="01", academic_year=academic_year, semester=SEMESTER_FALL, teacher=teacher_user.teacher_profile, capacity_min=10, capacity_max=30)
    other = Section.objects.create(course=course, section_number="02", academic_year=academic_year, semester=SEMESTER_WINTER, teacher=second_teacher_user.teacher_profile, capacity_min=10, capacity_max=30)
    teacher_client = authenticated_client(teacher_user)
    assert teacher_client.get("/api/sections/").data["count"] == 1
    assert teacher_client.get(f"/api/sections/{other.id}/").status_code == 404
    assert teacher_client.patch(f"/api/sections/{mine.id}/", {"semester": 2}, format="json").status_code == 403
    assert authenticated_client(student_user).get("/api/sections/").status_code == 403
    admin = authenticated_client(counselor_user)
    payload = {"course": course.id, "section_number": "03", "academic_year": academic_year.id, "semester": SEMESTER_FALL, "teacher": teacher_user.teacher_profile.id, "capacity_min": 10, "capacity_max": 30, "is_locked": False}
    assert admin.post("/api/sections/", payload, format="json").status_code == 400
    qualification = Qualification.objects.create(
        code="mathematics-senior",
        name="Mathematics - Senior",
        subject_code=QUALIFICATION_SUBJECT_MATHEMATICS,
        division=QUALIFICATION_DIVISION_SENIOR,
    )
    CourseQualificationRequirement.objects.create(course=course, qualification=qualification)
    TeacherQualification.objects.create(
        teacher=teacher_user.teacher_profile,
        qualification=qualification,
        review_status=QUALIFICATION_REVIEW_VERIFIED,
    )
    assert admin.post("/api/sections/", payload, format="json").status_code == 201
    assert admin.post("/api/sections/", payload, format="json").status_code == 400
    assert admin.get(f"/api/sections/?teacher={teacher_user.teacher_profile.id}").data["count"] == 2
