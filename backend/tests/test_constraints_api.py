import pytest

from backend.apps.constraints.models import CourseQualificationRequirement, Qualification, TeacherQualification
from backend.apps.courses.models import Section
from backend.apps.scheduling.models import TimeSlot


@pytest.mark.django_db
def test_teacher_manages_only_own_constraints(authenticated_client, teacher_user, second_teacher_user, counselor_user, course):
    qualification = Qualification.objects.create(name="Mathematics")
    mine = authenticated_client(teacher_user)
    url = f"/api/teachers/{teacher_user.teacher_profile.id}/qualifications/"
    assert mine.post(url, {"qualification": qualification.id}, format="json").status_code == 201
    assert mine.get(url).data["count"] == 1
    other_url = f"/api/teachers/{second_teacher_user.teacher_profile.id}/qualifications/"
    assert mine.post(other_url, {"qualification": qualification.id}, format="json").status_code == 403
    assert mine.get(other_url).data["count"] == 0
    assert authenticated_client(counselor_user).post(other_url, {"qualification": qualification.id}, format="json").status_code == 201


@pytest.mark.django_db
def test_shared_constraints_and_course_conflict_validation(authenticated_client, student_user, counselor_user, course):
    client = authenticated_client(counselor_user)
    assert authenticated_client(student_user).get("/api/qualifications/").status_code == 403
    assert client.post("/api/qualifications/", {"name": "Science"}, format="json").status_code == 201
    invalid = {"course_a": course.id, "course_b": course.id, "weight": -1}
    assert client.post("/api/course-conflicts/", invalid, format="json").status_code == 400


@pytest.mark.django_db
def test_section_lock_requires_qualified_teacher_and_can_be_cleared(authenticated_client, course, academic_year, teacher_user, counselor_user):
    qualification = Qualification.objects.create(name="Mathematics")
    CourseQualificationRequirement.objects.create(course=course, qualification=qualification)
    section = Section.objects.create(course=course, section_number="01", academic_year=academic_year, semester=1, capacity_min=10, capacity_max=30)
    timeslot = TimeSlot.objects.create(day="Monday", period=1, academic_year=academic_year, semester=1)
    url = f"/api/sections/{section.id}/lock/"
    client = authenticated_client(counselor_user)
    assert client.patch(url, {"locked_teacher": teacher_user.teacher_profile.id}, format="json").status_code == 400
    TeacherQualification.objects.create(teacher=teacher_user.teacher_profile, qualification=qualification)
    created = client.patch(url, {"locked_teacher": teacher_user.teacher_profile.id, "locked_timeslot": timeslot.id}, format="json")
    assert created.status_code == 201
    cleared = client.patch(url, {"locked_teacher": None}, format="json")
    assert cleared.status_code == 200
    assert cleared.data["locked_teacher"] is None and cleared.data["locked_timeslot"] == timeslot.id
