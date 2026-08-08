"""JWT authentication, role resolution, legacy permissions, and seed command."""

from types import SimpleNamespace

import pytest
from django.contrib.auth.models import User
from django.core.management import call_command
from rest_framework.test import APIClient

from backend.apps.common.constants import (
    COURSE_CATEGORY_MATH,
    COURSE_REQUEST_TYPE_PRIMARY,
    GRADE_LEVEL_12,
)
from backend.apps.common.models import AcademicYear
from backend.apps.courses.models import Course, CourseRequest
from backend.apps.people.models import Counselor, RoleChoices, Student, Teacher, UserRoleProfile
from backend.apps.people.permissions import (
    IsCounselor,
    IsDirector,
    IsOwnerOrCounselor,
    IsStaff,
    IsStudent,
    IsTeacher,
)
from backend.apps.people.roles import get_user_profile_id, get_user_role

import os
from dotenv import load_dotenv
# Load environment variables from .env file
load_dotenv()

PASSWORD = os.getenv("TEST_USER_PASSWORD")
if not PASSWORD:
    raise RuntimeError("TEST_USER_PASSWORD must be set in .env before running Django tests.")


# Auth/domain-role fixtures ---------------------------------------------------


@pytest.fixture
def academic_year():
    return AcademicYear.objects.create(name="2026-2027")


@pytest.fixture
def student_user(academic_year):
    user = User.objects.create_user(
        username="student",
        email="student@example.com",
        password=PASSWORD,
    )
    Student.objects.create(
        user=user,
        student_number="S1001",
        email="student@example.com",
        first_name="Sam",
        last_name="Student",
        date_of_birth="2009-01-01",
        grade_level=GRADE_LEVEL_12,
        academic_year=academic_year,
    )
    return user


@pytest.fixture
def second_student_user(academic_year):
    user = User.objects.create_user(
        username="student2",
        email="student2@example.com",
        password=PASSWORD,
    )
    Student.objects.create(
        user=user,
        student_number="S1002",
        email="student2@example.com",
        first_name="Sally",
        last_name="Student",
        date_of_birth="2009-02-01",
        grade_level=GRADE_LEVEL_12,
        academic_year=academic_year,
    )
    return user


@pytest.fixture
def teacher_user():
    user = User.objects.create_user(
        username="teacher",
        email="teacher@example.com",
        password=PASSWORD,
    )
    Teacher.objects.create(
        user=user,
        first_name="Terry",
        last_name="Teacher",
        email="teacher@example.com",
        department="Mathematics",
    )
    return user


@pytest.fixture
def counselor_user():
    user = User.objects.create_user(
        username="counselor",
        email="counselor@example.com",
        password=PASSWORD,
    )
    Counselor.objects.create(
        user=user,
        first_name="Casey",
        last_name="Counselor",
        email="counselor@example.com",
    )
    return user


@pytest.fixture
def staff_user():
    user = User.objects.create_user(
        username="staff",
        email="staff@example.com",
        password=PASSWORD,
        is_staff=True,
    )
    UserRoleProfile.objects.create(user=user, role=RoleChoices.STAFF)
    return user


@pytest.fixture
def director_user():
    user = User.objects.create_user(
        username="director",
        email="director@example.com",
        password=PASSWORD,
        is_staff=True,
    )
    UserRoleProfile.objects.create(user=user, role=RoleChoices.DIRECTOR)
    return user


@pytest.fixture
def unknown_user():
    user = User.objects.create_user(
        username="unknown",
        email="unknown@example.com",
        password=PASSWORD,
    )
    UserRoleProfile.objects.create(user=user, role=RoleChoices.UNKNOWN)
    return user


@pytest.fixture
def superuser():
    return User.objects.create_superuser(
        username="root",
        email="root@example.com",
        password=PASSWORD,
    )


def request_for(user):
    return SimpleNamespace(user=user)


@pytest.mark.django_db
def test_login_succeeds_with_valid_credentials(student_user):
    # Authentication and self-identification endpoints -----------------------
    response = APIClient().post(
        "/api/auth/login/",
        {"username": "student", "password": PASSWORD},
        format="json",
    )

    assert response.status_code == 200
    assert "access" in response.data
    assert "refresh" in response.data


@pytest.mark.django_db
def test_login_fails_with_invalid_credentials(student_user):
    response = APIClient().post(
        "/api/auth/login/",
        {"username": "student", "password": f"{PASSWORD}-invalid"},
        format="json",
    )

    assert response.status_code == 401


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("fixture_name", "expected_role"),
    [
        ("student_user", RoleChoices.STUDENT),
        ("teacher_user", RoleChoices.TEACHER),
        ("counselor_user", RoleChoices.COUNSELOR),
        ("staff_user", RoleChoices.STAFF),
        ("director_user", RoleChoices.DIRECTOR),
        ("unknown_user", RoleChoices.UNKNOWN),
    ],
)
def test_me_returns_current_user_role(request, fixture_name, expected_role):
    user = request.getfixturevalue(fixture_name)
    client = APIClient()
    client.force_authenticate(user=user)

    response = client.get("/api/me/")

    assert response.status_code == 200
    assert response.data["username"] == user.username
    assert response.data["role"] == expected_role
    assert response.data["profile_id"] == get_user_profile_id(user)


@pytest.mark.django_db
def test_me_rejects_anonymous_requests():
    response = APIClient().get("/api/me/")

    assert response.status_code == 401


@pytest.mark.django_db
def test_role_detection_prefers_domain_profiles(student_user, unknown_user):
    assert get_user_role(student_user) == RoleChoices.STUDENT
    assert get_user_role(unknown_user) == RoleChoices.UNKNOWN


@pytest.mark.django_db
def test_counselor_permission_allows_counselors_and_directors_only(
    counselor_user,
    director_user,
    student_user,
    teacher_user,
):
    # Role and ownership permission classes ----------------------------------
    permission = IsCounselor()

    assert permission.has_permission(request_for(counselor_user), None)
    assert permission.has_permission(request_for(director_user), None)
    assert not permission.has_permission(request_for(student_user), None)
    assert not permission.has_permission(request_for(teacher_user), None)


@pytest.mark.django_db
def test_student_permission_allows_students_only(student_user, teacher_user, counselor_user):
    permission = IsStudent()

    assert permission.has_permission(request_for(student_user), None)
    assert not permission.has_permission(request_for(teacher_user), None)
    assert not permission.has_permission(request_for(counselor_user), None)


@pytest.mark.django_db
def test_teacher_permission_allows_teachers_only(teacher_user, student_user, counselor_user):
    permission = IsTeacher()

    assert permission.has_permission(request_for(teacher_user), None)
    assert not permission.has_permission(request_for(student_user), None)
    assert not permission.has_permission(request_for(counselor_user), None)


@pytest.mark.django_db
def test_staff_and_director_permissions(staff_user, director_user, counselor_user, student_user):
    staff_permission = IsStaff()
    director_permission = IsDirector()

    assert staff_permission.has_permission(request_for(staff_user), None)
    assert staff_permission.has_permission(request_for(director_user), None)
    assert not staff_permission.has_permission(request_for(counselor_user), None)
    assert not staff_permission.has_permission(request_for(student_user), None)

    assert director_permission.has_permission(request_for(director_user), None)
    assert not director_permission.has_permission(request_for(staff_user), None)


@pytest.mark.django_db
def test_owner_or_counselor_blocks_student_from_another_students_object(
    student_user,
    second_student_user,
    counselor_user,
    staff_user,
    director_user,
    academic_year,
):
    course = Course.objects.create(
        name="Calculus and Vectors",
        grade_level=GRADE_LEVEL_12,
        course_code="MCV4U",
        category=COURSE_CATEGORY_MATH,
        capacity_min=10,
        capacity_max=30,
    )
    course_request = CourseRequest.objects.create(
        student=student_user.student_profile,
        course=course,
        academic_year=academic_year,
        request_type=COURSE_REQUEST_TYPE_PRIMARY,
    )
    permission = IsOwnerOrCounselor()

    assert permission.has_object_permission(request_for(student_user), None, course_request)
    assert not permission.has_object_permission(request_for(second_student_user), None, course_request)
    assert permission.has_object_permission(request_for(counselor_user), None, course_request)
    assert permission.has_object_permission(request_for(staff_user), None, course_request)
    assert permission.has_object_permission(request_for(director_user), None, course_request)


@pytest.mark.django_db
def test_superuser_bypasses_role_permissions(superuser, student_user):
    assert IsCounselor().has_permission(request_for(superuser), None)
    assert IsTeacher().has_permission(request_for(superuser), None)
    assert IsStudent().has_permission(request_for(superuser), None)
    assert IsOwnerOrCounselor().has_object_permission(
        request_for(superuser),
        None,
        student_user.student_profile,
    )


@pytest.mark.django_db
def test_seed_dev_users_creates_all_supported_roles():
    # Local-development provisioning -----------------------------------------
    call_command("seed_dev_users")

    assert get_user_role(User.objects.get(username="student")) == RoleChoices.STUDENT
    assert get_user_role(User.objects.get(username="teacher")) == RoleChoices.TEACHER
    assert get_user_role(User.objects.get(username="counselor")) == RoleChoices.COUNSELOR
    assert get_user_role(User.objects.get(username="staff")) == RoleChoices.STAFF
    assert get_user_role(User.objects.get(username="director")) == RoleChoices.DIRECTOR
    assert get_user_role(User.objects.get(username="unknown")) == RoleChoices.UNKNOWN
