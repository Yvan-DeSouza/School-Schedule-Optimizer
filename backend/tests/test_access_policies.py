from types import SimpleNamespace

import pytest
from django.contrib.auth.models import AnonymousUser, User

from backend.apps.access.base import BaseAccessPolicy
from backend.apps.access.permissions import PolicyPermission
from backend.apps.access.policies import CoursePolicy, CourseRequestPolicy, SectionPolicy
from backend.apps.access.rules import AccessRule
from backend.apps.access.scopes import ReadScope, WriteScope
from backend.apps.common.models import AcademicYear
from backend.apps.courses.models import Course, CourseRequest, Section
from backend.apps.people.models import Counselor, RoleChoices, Student, Teacher, UserRoleProfile


PASSWORD = "password123"


@pytest.fixture
def academic_year():
    return AcademicYear.objects.create(name="2026-2027")


@pytest.fixture
def course():
    return Course.objects.create(
        name="Calculus and Vectors",
        grade_level=12,
        course_code="MCV4U",
        category="math",
        capacity_min=10,
        capacity_max=30,
    )


@pytest.fixture
def second_course():
    return Course.objects.create(
        name="Physics",
        grade_level=12,
        course_code="SPH4U",
        category="science",
        capacity_min=10,
        capacity_max=30,
    )


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
        grade_level=12,
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
        grade_level=12,
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
def second_teacher_user():
    user = User.objects.create_user(
        username="teacher2",
        email="teacher2@example.com",
        password=PASSWORD,
    )
    Teacher.objects.create(
        user=user,
        first_name="Taylor",
        last_name="Teacher",
        email="teacher2@example.com",
        department="Science",
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
def section(course, academic_year, teacher_user):
    return Section.objects.create(
        course=course,
        section_number="01",
        academic_year=academic_year,
        semester=1,
        teacher=teacher_user.teacher_profile,
        capacity_min=10,
        capacity_max=30,
    )


@pytest.fixture
def second_section(second_course, academic_year, second_teacher_user):
    return Section.objects.create(
        course=second_course,
        section_number="01",
        academic_year=academic_year,
        semester=1,
        teacher=second_teacher_user.teacher_profile,
        capacity_min=10,
        capacity_max=30,
    )


@pytest.fixture
def course_request(student_user, course, academic_year):
    return CourseRequest.objects.create(
        student=student_user.student_profile,
        course=course,
        academic_year=academic_year,
        request_type="primary",
    )


@pytest.fixture
def second_course_request(second_student_user, second_course, academic_year):
    return CourseRequest.objects.create(
        student=second_student_user.student_profile,
        course=second_course,
        academic_year=academic_year,
        request_type="primary",
    )


def request(method, user):
    return SimpleNamespace(method=method, user=user, data={})


def ids(queryset):
    return set(queryset.values_list("id", flat=True))


def test_access_rule_defaults_to_no_access():
    rule = AccessRule()

    assert rule.read == ReadScope.NONE
    assert rule.write == WriteScope.NONE


def test_access_rule_keeps_read_and_write_scopes_separate():
    rule = AccessRule(read=ReadScope.ALL, write=WriteScope.OWN)

    assert rule.read == ReadScope.ALL
    assert rule.write == WriteScope.OWN


@pytest.mark.parametrize("kwargs", [{"read": ["all", "own"]}, {"write": ["all", "own"]}])
def test_access_rule_rejects_invalid_multi_scope_values(kwargs):
    with pytest.raises(ValueError):
        AccessRule(**kwargs)


@pytest.mark.django_db
def test_base_policy_fails_closed_for_own_and_assigned_scopes(student_user, course):
    class OwnPolicy(BaseAccessPolicy):
        rules = {RoleChoices.STUDENT: AccessRule(read=ReadScope.OWN, write=WriteScope.OWN)}

    class AssignedPolicy(BaseAccessPolicy):
        rules = {
            RoleChoices.STUDENT: AccessRule(
                read=ReadScope.ASSIGNED,
                write=WriteScope.ASSIGNED,
            )
        }

    assert ids(OwnPolicy.filter_read_queryset(student_user, Course.objects.all())) == set()
    assert not OwnPolicy.can_read_object(student_user, course)
    assert not OwnPolicy.can_write_object(student_user, course)

    assert ids(AssignedPolicy.filter_read_queryset(student_user, Course.objects.all())) == set()
    assert not AssignedPolicy.can_read_object(student_user, course)
    assert not AssignedPolicy.can_write_object(student_user, course)


@pytest.mark.django_db
def test_course_policy_role_access(
    course,
    student_user,
    teacher_user,
    counselor_user,
    staff_user,
    director_user,
    unknown_user,
):
    queryset = Course.objects.all()

    assert ids(CoursePolicy.filter_read_queryset(student_user, queryset)) == {course.id}
    assert ids(CoursePolicy.filter_read_queryset(teacher_user, queryset)) == {course.id}
    assert not CoursePolicy.can_create(student_user)
    assert not CoursePolicy.can_create(teacher_user)

    for user in [counselor_user, staff_user, director_user]:
        assert ids(CoursePolicy.filter_read_queryset(user, queryset)) == {course.id}
        assert CoursePolicy.can_create(user)
        assert CoursePolicy.can_write_object(user, course)

    assert ids(CoursePolicy.filter_read_queryset(unknown_user, queryset)) == set()
    assert not CoursePolicy.can_create(unknown_user)
    assert not CoursePolicy.can_write_object(unknown_user, course)


@pytest.mark.django_db
def test_section_policy_teacher_reads_only_assigned_sections(
    section,
    second_section,
    teacher_user,
):
    queryset = Section.objects.all()

    assert ids(SectionPolicy.filter_read_queryset(teacher_user, queryset)) == {section.id}
    assert SectionPolicy.can_read_object(teacher_user, section)
    assert not SectionPolicy.can_read_object(teacher_user, second_section)
    assert not SectionPolicy.can_create(teacher_user)
    assert not SectionPolicy.can_write_object(teacher_user, section)


@pytest.mark.django_db
def test_section_policy_admin_roles_read_and_write_all(
    section,
    second_section,
    counselor_user,
    staff_user,
    director_user,
):
    queryset = Section.objects.all()

    for user in [counselor_user, staff_user, director_user]:
        assert ids(SectionPolicy.filter_read_queryset(user, queryset)) == {
            section.id,
            second_section.id,
        }
        assert SectionPolicy.can_create(user)
        assert SectionPolicy.can_write_object(user, section)
        assert SectionPolicy.can_write_object(user, second_section)


@pytest.mark.django_db
def test_section_policy_denies_students_and_unknown_users(
    section,
    student_user,
    unknown_user,
):
    queryset = Section.objects.all()

    for user in [student_user, unknown_user]:
        assert ids(SectionPolicy.filter_read_queryset(user, queryset)) == set()
        assert not SectionPolicy.can_read_object(user, section)
        assert not SectionPolicy.can_create(user)
        assert not SectionPolicy.can_write_object(user, section)


@pytest.mark.django_db
def test_course_request_policy_student_reads_and_writes_own_requests_only(
    course_request,
    second_course_request,
    student_user,
):
    queryset = CourseRequest.objects.all()

    assert ids(CourseRequestPolicy.filter_read_queryset(student_user, queryset)) == {
        course_request.id
    }
    assert CourseRequestPolicy.can_read_object(student_user, course_request)
    assert not CourseRequestPolicy.can_read_object(student_user, second_course_request)
    assert CourseRequestPolicy.can_create(student_user)
    assert CourseRequestPolicy.can_write_object(student_user, course_request)
    assert not CourseRequestPolicy.can_write_object(student_user, second_course_request)


@pytest.mark.django_db
def test_course_request_policy_denies_teachers_and_unknown_users(
    course_request,
    teacher_user,
    unknown_user,
):
    queryset = CourseRequest.objects.all()

    for user in [teacher_user, unknown_user]:
        assert ids(CourseRequestPolicy.filter_read_queryset(user, queryset)) == set()
        assert not CourseRequestPolicy.can_read_object(user, course_request)
        assert not CourseRequestPolicy.can_create(user)
        assert not CourseRequestPolicy.can_write_object(user, course_request)


@pytest.mark.django_db
def test_course_request_policy_admin_roles_read_and_write_all(
    course_request,
    second_course_request,
    counselor_user,
    staff_user,
    director_user,
):
    queryset = CourseRequest.objects.all()

    for user in [counselor_user, staff_user, director_user]:
        assert ids(CourseRequestPolicy.filter_read_queryset(user, queryset)) == {
            course_request.id,
            second_course_request.id,
        }
        assert CourseRequestPolicy.can_create(user)
        assert CourseRequestPolicy.can_write_object(user, course_request)
        assert CourseRequestPolicy.can_write_object(user, second_course_request)


@pytest.mark.django_db
def test_policy_permission_denies_view_without_policy(student_user):
    permission = PolicyPermission()
    view = SimpleNamespace()

    assert not permission.has_permission(request("GET", student_user), view)


@pytest.mark.django_db
def test_policy_permission_allows_safe_methods_when_read_access_exists(student_user):
    permission = PolicyPermission()
    view = SimpleNamespace(policy_class=CoursePolicy)

    assert permission.has_permission(request("GET", student_user), view)
    assert permission.has_permission(request("HEAD", student_user), view)
    assert permission.has_permission(request("OPTIONS", student_user), view)


@pytest.mark.django_db
def test_policy_permission_denies_safe_methods_when_read_access_is_missing(
    student_user,
):
    permission = PolicyPermission()
    view = SimpleNamespace(policy_class=SectionPolicy)

    assert not permission.has_permission(request("GET", student_user), view)


@pytest.mark.django_db
def test_policy_permission_allows_unsafe_methods_when_write_access_exists(counselor_user):
    permission = PolicyPermission()
    view = SimpleNamespace(policy_class=CoursePolicy)

    assert permission.has_permission(request("POST", counselor_user), view)
    assert permission.has_permission(request("PATCH", counselor_user), view)
    assert permission.has_permission(request("PUT", counselor_user), view)
    assert permission.has_permission(request("DELETE", counselor_user), view)


@pytest.mark.django_db
def test_policy_permission_denies_unsafe_methods_when_write_access_is_missing(
    student_user,
):
    permission = PolicyPermission()
    view = SimpleNamespace(policy_class=CoursePolicy)

    assert not permission.has_permission(request("POST", student_user), view)
    assert not permission.has_permission(request("PATCH", student_user), view)


@pytest.mark.django_db
def test_policy_permission_checks_object_read_write_and_delete(
    course_request,
    second_course_request,
    student_user,
):
    permission = PolicyPermission()
    view = SimpleNamespace(policy_class=CourseRequestPolicy)

    assert permission.has_object_permission(request("GET", student_user), view, course_request)
    assert not permission.has_object_permission(
        request("GET", student_user),
        view,
        second_course_request,
    )
    assert permission.has_object_permission(request("PATCH", student_user), view, course_request)
    assert not permission.has_object_permission(
        request("PATCH", student_user),
        view,
        second_course_request,
    )
    assert permission.has_object_permission(request("DELETE", student_user), view, course_request)
    assert not permission.has_object_permission(
        request("DELETE", student_user),
        view,
        second_course_request,
    )


def test_policy_permission_denies_anonymous_user():
    permission = PolicyPermission()
    view = SimpleNamespace(policy_class=CoursePolicy)

    assert not permission.has_permission(request("GET", AnonymousUser()), view)
