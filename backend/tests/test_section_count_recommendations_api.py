import pytest
from rest_framework.test import APIClient

from backend.apps.common.models import AcademicYear, HistoricalCourseDemand
from backend.apps.constraints.models import CourseConflict
from backend.apps.courses.models import CourseRequest, Section


@pytest.mark.django_db
def test_section_count_recommendations_permissions_and_read_only_behavior(
    authenticated_client, academic_year, course, student_user, teacher_user,
    counselor_user, staff_user, director_user, unknown_user,
):
    prior_year = AcademicYear.objects.create(name="2025-2026")
    HistoricalCourseDemand.objects.create(course=course, academic_year=prior_year, requests=100, final_enrollment=90)
    CourseRequest.objects.create(student=student_user.student_profile, academic_year=academic_year, course=course, request_type="primary")
    url = f"/api/planning/section-count-recommendations/?academic_year={academic_year.id}"

    assert APIClient().get(url).status_code == 401
    for user in (student_user, teacher_user, unknown_user):
        assert authenticated_client(user).get(url).status_code == 403
    before_sections, before_conflicts = Section.objects.count(), CourseConflict.objects.count()
    for user in (counselor_user, staff_user, director_user):
        response = authenticated_client(user).get(url)
        assert response.status_code == 200
        assert response.data[0]["course_code"] == course.course_code
        assert response.data[0]["recommended_section_count"] == 1
        assert response.data[0]["used_fallback_ratio"] is False
    assert Section.objects.count() == before_sections
    assert CourseConflict.objects.count() == before_conflicts


@pytest.mark.django_db
def test_section_count_recommendations_validate_academic_year(authenticated_client, counselor_user):
    client = authenticated_client(counselor_user)
    assert client.get("/api/planning/section-count-recommendations/").status_code == 400
    assert client.get("/api/planning/section-count-recommendations/?academic_year=nope").status_code == 400
    assert client.get("/api/planning/section-count-recommendations/?academic_year=99999").status_code == 404
