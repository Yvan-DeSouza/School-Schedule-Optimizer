import pytest

from backend.apps.courses.models import Course, CourseRequest
from backend.apps.courses.services.demand import get_course_demand_summary


@pytest.mark.django_db
def test_demand_service_and_endpoint(authenticated_client, api_client, course, academic_year, student_user, second_student_user, counselor_user, teacher_user):
    other = Course.objects.create(name="Physics", grade_level=12, course_code="SPH4U", category="science", capacity_min=10, capacity_max=30)
    CourseRequest.objects.create(student=student_user.student_profile, academic_year=academic_year, course=course, request_type="primary")
    CourseRequest.objects.create(student=second_student_user.student_profile, academic_year=academic_year, course=course, request_type="alternate")
    CourseRequest.objects.create(student=second_student_user.student_profile, academic_year=academic_year, course=other, request_type="primary")
    summary = get_course_demand_summary(academic_year.id)
    assert summary[0]["total_requests"] == 2 and summary[0]["primary_requests"] == 1 and summary[0]["alternate_requests"] == 1
    assert api_client.get("/api/demand/summary/").status_code == 401
    assert authenticated_client(teacher_user).get(f"/api/demand/summary/?academic_year={academic_year.id}").status_code == 403
    response = authenticated_client(counselor_user).get(f"/api/demand/summary/?academic_year={academic_year.id}")
    assert response.status_code == 200 and response.data == summary
    assert authenticated_client(counselor_user).get("/api/demand/summary/").status_code == 400
    assert authenticated_client(counselor_user).get("/api/demand/summary/?academic_year=99999").status_code == 404
