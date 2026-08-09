"""HTTP contract and configuration coverage for student assignment."""

import pytest

from backend.apps.common.constants import (
    COURSE_CATEGORY_MATH,
    COURSE_REQUEST_TYPE_PRIMARY,
    GRADE_LEVEL_12,
    SCHEDULE_BLOCK_A,
)
from backend.apps.courses.models import Course, CourseRequest, Section
from backend.apps.courses.services.offerings import ensure_academic_year_offerings
from backend.apps.scheduling.models import SectionSchedule, TimeSlot


@pytest.mark.django_db
def test_student_roster_omits_contact_and_birth_date_fields(
    authenticated_client, counselor_user, student_user,
):
    response = authenticated_client(counselor_user).get("/api/students/")

    assert response.status_code == 200
    row = response.data["results"][0]
    assert set(row) == {
        "id", "student_number", "first_name", "last_name", "grade_level", "academic_year",
    }
    assert "email" not in row
    assert "date_of_birth" not in row
    assert authenticated_client(student_user).get("/api/students/").status_code == 403


@pytest.mark.django_db
def test_prerequisite_and_soft_sequence_configuration_reject_cycles(
    authenticated_client, counselor_user, course,
):
    second = Course.objects.create(
        name="Linear Algebra", grade_level=GRADE_LEVEL_12,
        course_code="MHF4U", category=COURSE_CATEGORY_MATH,
        capacity_min=10, capacity_max=30,
    )
    client = authenticated_client(counselor_user)

    assert client.post("/api/course-prerequisites/", {
        "course": second.id, "prerequisite": course.id,
    }, format="json").status_code == 201
    assert client.post("/api/course-prerequisites/", {
        "course": course.id, "prerequisite": second.id,
    }, format="json").status_code == 400
    assert client.post("/api/course-sequence-preferences/", {
        "earlier_course": course.id, "later_course": second.id, "is_active": True,
    }, format="json").status_code == 201
    assert client.post("/api/course-sequence-preferences/", {
        "earlier_course": second.id, "later_course": course.id, "is_active": True,
    }, format="json").status_code == 400


@pytest.mark.django_db
def test_counselor_can_create_and_review_sections_only_student_run(
    authenticated_client, academic_year, course, counselor_user, student_user,
):
    offering = ensure_academic_year_offerings(academic_year, actor=counselor_user)[0]
    section = Section.objects.create(
        course=course, delivery_group=offering.delivery_group,
        section_number="S1-01", academic_year=academic_year, semester=1,
        capacity_min=10, capacity_max=30,
    )
    slot = TimeSlot.objects.create(academic_year=academic_year, semester=1, block=SCHEDULE_BLOCK_A)
    SectionSchedule.objects.create(section=section, timeslot=slot)
    CourseRequest.objects.create(
        student=student_user.student_profile, academic_year=academic_year,
        course=course, request_type=COURSE_REQUEST_TYPE_PRIMARY,
    )
    payload = {
        "academic_year": academic_year.id,
        "staffing_mode": "sections_only",
        "soft_constraint_importance": {
            "section_utilization_balance": "important",
            "student_semester_balance": "important",
            "course_sequence_preferences": "really_important",
        },
    }

    client = authenticated_client(counselor_user)
    response = client.post("/api/planning/student-assignment-runs/", payload, format="json")

    assert response.status_code == 201
    assert response.data["status"] == "complete"
    review = client.get(f"/api/planning/student-assignment-runs/{response.data['id']}/review/")
    assert review.status_code == 200
    assert review.data["approval_allowed"] is True
