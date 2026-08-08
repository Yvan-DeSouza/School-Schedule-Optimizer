import pytest
from rest_framework.test import APIClient

from backend.apps.common.constants import COURSE_REQUEST_TYPE_PRIMARY, GRADE_LEVEL_10, SEMESTER_FALL
from backend.apps.courses.models import CourseRequest, Section
from backend.apps.scheduling.models import SectionPlanningRun, TeacherPlanningCapacity


@pytest.mark.django_db
def test_section_planning_run_is_role_protected_immutable_and_read_only(
    authenticated_client, academic_year, course, student_user, teacher_user, counselor_user,
):
    course.grade_level = GRADE_LEVEL_10
    course.save(update_fields=["grade_level"])
    CourseRequest.objects.create(
        student=student_user.student_profile,
        academic_year=academic_year,
        course=course,
        request_type=COURSE_REQUEST_TYPE_PRIMARY,
    )
    TeacherPlanningCapacity.objects.create(
        teacher=teacher_user.teacher_profile,
        academic_year=academic_year,
        semester=SEMESTER_FALL,
        maximum_sections=1,
    )
    url = "/api/planning/section-count-runs/"
    assert APIClient().post(url, {"academic_year": academic_year.id}, format="json").status_code == 401
    assert authenticated_client(teacher_user).post(url, {"academic_year": academic_year.id}, format="json").status_code == 403

    before_sections = Section.objects.count()
    response = authenticated_client(counselor_user).post(url, {"academic_year": academic_year.id}, format="json")
    assert response.status_code == 201
    assert response.data["status"] == "complete"
    assert response.data["result"]["courses"][0]["warnings"] == ["below_hard_min_review_required"]
    assert Section.objects.count() == before_sections

    run = SectionPlanningRun.objects.get(pk=response.data["id"])
    assert authenticated_client(counselor_user).get(f"{url}{run.id}/").status_code == 200
    with pytest.raises(Exception):
        run.save()
