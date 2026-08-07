import pytest
import os
from dotenv import load_dotenv
from django.contrib.auth.models import User
from rest_framework.test import APIClient

from backend.apps.common.models import AcademicYear
from backend.apps.courses.models import Course
from backend.apps.people.models import Counselor, RoleChoices, Student, Teacher, UserRoleProfile

load_dotenv()


def test_user_password():
    password = os.getenv("TEST_USER_PASSWORD")
    if not password:
        raise RuntimeError("TEST_USER_PASSWORD must be set in .env before running Django tests.")
    return password


@pytest.fixture
def academic_year():
    return AcademicYear.objects.create(name="2026-2027")


@pytest.fixture
def course():
    return Course.objects.create(name="Calculus", grade_level=12, course_code="MCV4U", category="math", capacity_min=10, capacity_max=30)


def make_user(username, role, academic_year=None):
    user = User.objects.create_user(username=username, password=test_user_password(), is_staff=role in (RoleChoices.STAFF, RoleChoices.DIRECTOR))
    if role == RoleChoices.STUDENT:
        Student.objects.create(user=user, student_number=f"S-{username}", email=f"{username}@example.com", first_name=username, last_name="Student", date_of_birth="2009-01-01", grade_level=12, academic_year=academic_year)
    elif role == RoleChoices.TEACHER:
        Teacher.objects.create(user=user, first_name=username, last_name="Teacher", email=f"{username}@example.com", department="Math")
    elif role == RoleChoices.COUNSELOR:
        Counselor.objects.create(user=user, first_name=username, last_name="Counselor", email=f"{username}@example.com")
    else:
        UserRoleProfile.objects.create(user=user, role=role)
    return user


@pytest.fixture
def student_user(academic_year): return make_user("student", RoleChoices.STUDENT, academic_year)
@pytest.fixture
def second_student_user(academic_year): return make_user("student2", RoleChoices.STUDENT, academic_year)
@pytest.fixture
def teacher_user(): return make_user("teacher", RoleChoices.TEACHER)
@pytest.fixture
def second_teacher_user(): return make_user("teacher2", RoleChoices.TEACHER)
@pytest.fixture
def counselor_user(): return make_user("counselor", RoleChoices.COUNSELOR)
@pytest.fixture
def staff_user(): return make_user("staff", RoleChoices.STAFF)
@pytest.fixture
def director_user(): return make_user("director", RoleChoices.DIRECTOR)
@pytest.fixture
def unknown_user(): return make_user("unknown", RoleChoices.UNKNOWN)


@pytest.fixture
def api_client(): return APIClient()


@pytest.fixture
def authenticated_client():
    def client_for(user):
        client = APIClient()
        client.force_authenticate(user=user)
        return client
    return client_for
