"""API contracts for teacher-owned constraints, shared rules, and section locks."""

import pytest

from backend.apps.common.constants import (
    COURSE_CATEGORY_MATH,
    GRADE_LEVEL_10,
    QUALIFICATION_DIVISION_INTERMEDIATE,
    QUALIFICATION_DIVISION_SENIOR,
    QUALIFICATION_ENFORCEMENT_PREFERRED,
    QUALIFICATION_ENFORCEMENT_REQUIRED,
    QUALIFICATION_SOURCE_ASPEN,
    QUALIFICATION_SUBJECT_CHEMISTRY,
    QUALIFICATION_SUBJECT_MATHEMATICS,
    SCHEDULE_BLOCK_A,
    SEMESTER_FALL,
)
from backend.apps.constraints.models import CourseQualificationRequirement, Qualification, TeacherQualification
from backend.apps.courses.models import Course, Section
from backend.apps.scheduling.models import TimeSlot


@pytest.mark.django_db
def test_teacher_manages_only_own_constraints(authenticated_client, teacher_user, second_teacher_user, counselor_user, course):
    # Nested URL identity and policy scope both prevent cross-teacher writes.
    qualification = Qualification.objects.create(
        code="mathematics-intermediate",
        name="Mathematics - Intermediate",
        subject_code=QUALIFICATION_SUBJECT_MATHEMATICS,
        division=QUALIFICATION_DIVISION_INTERMEDIATE,
    )
    mine = authenticated_client(teacher_user)
    url = f"/api/teachers/{teacher_user.teacher_profile.id}/qualifications/"
    created = mine.post(
        url,
        {
            "qualification": qualification.id,
            "source_system": QUALIFICATION_SOURCE_ASPEN,
            "source_record_id": "aspen-credential-42",
            "source_text": "Cycles moyen et intermédiaire, Mathématiques",
            "awarded_date_text": "October 2010",
        },
        format="json",
    )
    assert created.status_code == 201
    assert created.data["source_system"] == QUALIFICATION_SOURCE_ASPEN
    assert mine.get(url).data["count"] == 1
    other_url = f"/api/teachers/{second_teacher_user.teacher_profile.id}/qualifications/"
    assert mine.post(other_url, {"qualification": qualification.id}, format="json").status_code == 403
    assert mine.get(other_url).data["count"] == 0
    assert authenticated_client(counselor_user).post(other_url, {"qualification": qualification.id}, format="json").status_code == 201


@pytest.mark.django_db
def test_shared_constraints_and_course_conflict_validation(authenticated_client, student_user, counselor_user, course):
    client = authenticated_client(counselor_user)
    assert authenticated_client(student_user).get("/api/qualifications/").status_code == 403
    qualification_payload = {
        "code": "chemistry-senior",
        "name": "Chemistry - Senior",
        "subject_code": QUALIFICATION_SUBJECT_CHEMISTRY,
        "division": QUALIFICATION_DIVISION_SENIOR,
    }
    assert client.post("/api/qualifications/", qualification_payload, format="json").status_code == 201
    invalid = {"course_a": course.id, "course_b": course.id, "weight": -1}
    assert client.post("/api/course-conflicts/", invalid, format="json").status_code == 400


@pytest.mark.django_db
def test_section_lock_requires_qualified_teacher_and_can_be_cleared(authenticated_client, course, academic_year, teacher_user, counselor_user):
    # Lock validation shares the direct Section assignment qualification service.
    qualification = Qualification.objects.create(
        code="mathematics-senior",
        name="Mathematics - Senior",
        subject_code=QUALIFICATION_SUBJECT_MATHEMATICS,
        division=QUALIFICATION_DIVISION_SENIOR,
    )
    CourseQualificationRequirement.objects.create(course=course, qualification=qualification)
    section = Section.objects.create(course=course, section_number="01", academic_year=academic_year, semester=SEMESTER_FALL, capacity_min=10, capacity_max=30)
    timeslot = TimeSlot.objects.create(block=SCHEDULE_BLOCK_A, academic_year=academic_year, semester=SEMESTER_FALL)
    url = f"/api/sections/{section.id}/lock/"
    client = authenticated_client(counselor_user)
    assert client.patch(url, {"locked_teacher": teacher_user.teacher_profile.id}, format="json").status_code == 400
    TeacherQualification.objects.create(teacher=teacher_user.teacher_profile, qualification=qualification)
    created = client.patch(url, {"locked_teacher": teacher_user.teacher_profile.id, "locked_timeslot": timeslot.id}, format="json")
    assert created.status_code == 201
    cleared = client.patch(url, {"locked_teacher": None}, format="json")
    assert cleared.status_code == 200
    assert cleared.data["locked_teacher"] is None and cleared.data["locked_timeslot"] == timeslot.id


@pytest.mark.django_db
def test_grade_ten_qualification_is_a_preference_not_a_lock_requirement(
    authenticated_client, academic_year, teacher_user, counselor_user,
):
    course = Course.objects.create(
        name="Principles of Mathematics",
        grade_level=GRADE_LEVEL_10,
        course_code="MPM2D",
        category=COURSE_CATEGORY_MATH,
        capacity_min=10,
        capacity_max=30,
    )
    qualification = Qualification.objects.create(
        code="mathematics-intermediate",
        name="Mathematics - Intermediate",
        subject_code=QUALIFICATION_SUBJECT_MATHEMATICS,
        division=QUALIFICATION_DIVISION_INTERMEDIATE,
    )
    section = Section.objects.create(
        course=course,
        section_number="01",
        academic_year=academic_year,
        semester=SEMESTER_FALL,
        capacity_min=10,
        capacity_max=30,
    )
    client = authenticated_client(counselor_user)
    rule_url = "/api/course-qualification-requirements/"
    required_payload = {
        "course": course.id,
        "qualification": qualification.id,
        "enforcement": QUALIFICATION_ENFORCEMENT_REQUIRED,
    }
    assert client.post(rule_url, required_payload, format="json").status_code == 400
    preferred_payload = {"course": course.id, "qualification": qualification.id, "enforcement": QUALIFICATION_ENFORCEMENT_PREFERRED}
    assert client.post(rule_url, preferred_payload, format="json").status_code == 201
    assert client.patch(
        f"/api/sections/{section.id}/lock/",
        {"locked_teacher": teacher_user.teacher_profile.id},
        format="json",
    ).status_code == 201
