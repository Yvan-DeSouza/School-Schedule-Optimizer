"""Annual counselor conflict-matrix API contracts."""

import pytest

from backend.apps.common.constants import COURSE_CATEGORY_SCIENCE, GRADE_LEVEL_12
from backend.apps.constraints.models import CourseConflict, CourseConflictAdjustment
from backend.apps.courses.models import Course, CourseRequest
from backend.apps.courses.services.offerings import ensure_academic_year_offerings


@pytest.mark.django_db
def test_matrix_setup_and_adjustment_are_year_scoped_and_audited(
    authenticated_client, academic_year, counselor_user, course, student_user, second_student_user,
):
    physics = Course.objects.create(
        name="Physics", course_code="SPH4U", grade_level=GRADE_LEVEL_12,
        category=COURSE_CATEGORY_SCIENCE, capacity_min=10, capacity_max=30,
    )
    ensure_academic_year_offerings(academic_year, actor=counselor_user)
    # Only primary requests form this matrix. One student requests both, while
    # the other requests only Physics, giving a 50% overlap score.
    CourseRequest.objects.create(student=student_user.student_profile, academic_year=academic_year, course=course, request_type="primary")
    CourseRequest.objects.create(student=student_user.student_profile, academic_year=academic_year, course=physics, request_type="primary")
    CourseRequest.objects.create(student=second_student_user.student_profile, academic_year=academic_year, course=physics, request_type="primary")
    client = authenticated_client(counselor_user)
    response = client.post("/api/planning/course-conflict-matrices/", {
        "academic_year": academic_year.id,
        "initialization_mode": "fresh_current_demand",
    }, format="json")
    assert response.status_code == 201
    matrix_id = response.data["id"]
    conflict = CourseConflict.objects.get(matrix_id=matrix_id, course_a_id=course.id, course_b_id=physics.id)
    assert float(conflict.computed_weight) == 50
    adjusted = client.post(
        f"/api/planning/course-conflict-matrices/{matrix_id}/conflicts/{conflict.id}/adjust/",
        {"weight": "75.00", "reason": "Department sequencing experience."}, format="json",
    )
    assert adjusted.status_code == 200
    conflict.refresh_from_db()
    assert conflict.is_overridden and float(conflict.weight) == 75
    assert CourseConflictAdjustment.objects.filter(conflict=conflict).count() == 1
    # Matrix rows are inspection-only; adjustments are the sole mutation path.
    assert client.patch(f"/api/course-conflicts/{conflict.id}/", {"weight": 10}, format="json").status_code == 400
