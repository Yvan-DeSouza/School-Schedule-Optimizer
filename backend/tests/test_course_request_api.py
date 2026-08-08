"""Student ownership, planner proxy entry, duplicates, and request filtering."""

import pytest

from backend.apps.common.constants import (
    COURSE_CATEGORY_SCIENCE,
    COURSE_REQUEST_TYPE_ALTERNATE,
    COURSE_REQUEST_TYPE_PRIMARY,
    GRADE_LEVEL_12,
)
from backend.apps.courses.models import Course, CourseRequest


@pytest.mark.django_db
def test_student_course_request_ownership(authenticated_client, course, academic_year, student_user, second_student_user, counselor_user, teacher_user):
    second_course = Course.objects.create(name="Physics", grade_level=GRADE_LEVEL_12, course_code="SPH4U", category=COURSE_CATEGORY_SCIENCE, capacity_min=10, capacity_max=30)
    client = authenticated_client(student_user)
    own = {"academic_year": academic_year.id, "course": course.id, "request_type": COURSE_REQUEST_TYPE_PRIMARY, "is_mandatory": False}
    response = client.post("/api/course-requests/", own, format="json")
    assert response.status_code == 201 and response.data["student"] == student_user.student_profile.id
    spoofed = {"academic_year": academic_year.id, "course": second_course.id, "request_type": COURSE_REQUEST_TYPE_ALTERNATE, "student": second_student_user.student_profile.id}
    assert client.post("/api/course-requests/", spoofed, format="json").status_code == 400
    other = CourseRequest.objects.create(student=second_student_user.student_profile, academic_year=academic_year, course=second_course, request_type=COURSE_REQUEST_TYPE_PRIMARY)
    assert client.get("/api/course-requests/").data["count"] == 1
    assert client.get(f"/api/course-requests/{other.id}/").status_code == 404
    assert authenticated_client(teacher_user).get("/api/course-requests/").status_code == 403
    assert authenticated_client(counselor_user).get("/api/course-requests/").data["count"] == 2
    assert client.post("/api/course-requests/", own, format="json").status_code == 400


@pytest.mark.django_db
def test_student_may_have_only_one_backup_per_academic_year(
    authenticated_client, academic_year, student_user,
):
    first = Course.objects.create(
        name="Physics", grade_level=GRADE_LEVEL_12, course_code="SPH4U",
        category=COURSE_CATEGORY_SCIENCE, capacity_min=10, capacity_max=30,
    )
    second = Course.objects.create(
        name="Chemistry", grade_level=GRADE_LEVEL_12, course_code="SCH4U",
        category=COURSE_CATEGORY_SCIENCE, capacity_min=10, capacity_max=30,
    )
    client = authenticated_client(student_user)

    assert client.post(
        "/api/course-requests/",
        {
            "academic_year": academic_year.id,
            "course": first.id,
            "request_type": COURSE_REQUEST_TYPE_ALTERNATE,
        },
        format="json",
    ).status_code == 201
    response = client.post(
        "/api/course-requests/",
        {
            "academic_year": academic_year.id,
            "course": second.id,
            "request_type": COURSE_REQUEST_TYPE_ALTERNATE,
        },
        format="json",
    )

    assert response.status_code == 400
    assert "one backup" in str(response.data).lower()
