"""Course catalog authorization, default planning policy, and filtering tests."""

import pytest

from backend.apps.common.constants import COURSE_CATEGORY_MATH, COURSE_CATEGORY_SCIENCE, GRADE_LEVEL_12

@pytest.mark.django_db
def test_course_access_and_pagination(api_client, authenticated_client, course, student_user, teacher_user, counselor_user, unknown_user):
    assert api_client.get("/api/courses/").status_code == 401
    assert authenticated_client(unknown_user).get("/api/courses/").status_code == 403
    for user in (student_user, teacher_user):
        response = authenticated_client(user).get("/api/courses/")
        assert response.status_code == 200
        assert response.data["count"] == 1
    payload = {"name": "Physics", "grade_level": GRADE_LEVEL_12, "course_code": "SPH4U", "category": COURSE_CATEGORY_SCIENCE, "capacity_min": 10, "capacity_max": 30, "is_online": False}
    assert authenticated_client(counselor_user).post("/api/courses/", payload, format="json").status_code == 201
    assert authenticated_client(student_user).post("/api/courses/", payload, format="json").status_code == 403


@pytest.mark.django_db
def test_course_validation_and_filtering(authenticated_client, course, counselor_user):
    client = authenticated_client(counselor_user)
    duplicate = {"name": "Again", "grade_level": GRADE_LEVEL_12, "course_code": course.course_code, "category": COURSE_CATEGORY_MATH, "capacity_min": 1, "capacity_max": 2}
    assert client.post("/api/courses/", duplicate, format="json").status_code == 400
    invalid = {**duplicate, "course_code": "NEW", "capacity_min": 30, "capacity_max": 10}
    assert client.post("/api/courses/", invalid, format="json").status_code == 400
    assert client.get(f"/api/courses/?category={COURSE_CATEGORY_MATH}").data["count"] == 1
