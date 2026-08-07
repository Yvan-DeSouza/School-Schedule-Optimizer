import pytest
from rest_framework.test import APIClient

from backend.apps.common.models import AcademicYear, Room
from backend.apps.constraints.models import TeacherAvailability
from backend.apps.courses.models import Section
from backend.apps.scheduling.models import SectionSchedule, TimeSlot


@pytest.mark.django_db
def test_reference_data_access_matrix_and_room_filters(
    authenticated_client, student_user, teacher_user, counselor_user, staff_user,
    director_user, unknown_user,
):
    Room.objects.create(name="101", room_type="classroom", capacity=30)
    assert APIClient().get("/api/rooms/").status_code == 401
    assert authenticated_client(unknown_user).get("/api/rooms/").status_code == 403
    for user in (student_user, teacher_user, counselor_user, staff_user, director_user):
        assert authenticated_client(user).get("/api/rooms/").status_code == 200

    payload = {"name": "Lab 1", "room_type": "science_lab", "capacity": 25, "is_specialized": True}
    assert authenticated_client(counselor_user).post("/api/rooms/", payload, format="json").status_code == 403
    assert authenticated_client(staff_user).post("/api/rooms/", payload, format="json").status_code == 201
    assert authenticated_client(director_user).get("/api/rooms/?room_type=science_lab").data["count"] == 1


@pytest.mark.django_db
def test_timeslot_validation_filters_and_fixed_rotation(authenticated_client, academic_year, staff_user):
    client = authenticated_client(staff_user)
    payload = {"academic_year": academic_year.id, "semester": 1, "block": "A", "is_available": True}
    created = client.post("/api/timeslots/", payload, format="json")
    assert created.status_code == 201
    assert created.data["rotation"] == [
        {"rotation_day": 1, "period": 1}, {"rotation_day": 2, "period": 3},
        {"rotation_day": 3, "period": 2}, {"rotation_day": 4, "period": 4},
    ]
    assert client.post("/api/timeslots/", payload, format="json").status_code == 400
    assert client.get(f"/api/timeslots/?academic_year={academic_year.id}&block=A").data["count"] == 1


@pytest.mark.django_db
def test_reference_deletes_are_blocked_only_when_referenced(
    authenticated_client, academic_year, course, staff_user, teacher_user,
):
    client = authenticated_client(staff_user)
    empty_year = AcademicYear.objects.create(name="2027-2028")
    assert client.delete(f"/api/academic-years/{empty_year.id}/").status_code == 204
    section = Section.objects.create(course=course, section_number="01", academic_year=academic_year, semester=1, capacity_min=10, capacity_max=30)
    assert client.delete(f"/api/academic-years/{academic_year.id}/").status_code == 400

    room = Room.objects.create(name="101", room_type="classroom", capacity=30)
    SectionSchedule.objects.create(section=section, room=room)
    assert client.delete(f"/api/rooms/{room.id}/").status_code == 400

    timeslot = TimeSlot.objects.create(academic_year=academic_year, semester=1, block="A")
    TeacherAvailability.objects.create(teacher=teacher_user.teacher_profile, timeslot=timeslot)
    assert client.delete(f"/api/timeslots/{timeslot.id}/").status_code == 400
