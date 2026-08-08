"""Small test data builders for common school-scheduling objects."""

from itertools import count

from django.contrib.auth.models import User

from backend.apps.common.constants import (
    COURSE_CATEGORY_MATH,
    GRADE_LEVEL_12,
    USER_ROLE_STAFF,
)
from backend.apps.common.models import AcademicYear
from backend.apps.courses.models import Course, DeliveryGroup, Section
from backend.apps.people.models import Counselor, RoleChoices, Student, Teacher, UserRoleProfile


_sequence = count(1)


def user(username=None, *, role=USER_ROLE_STAFF, password="test-password"):
    """Create an auth user plus the matching domain role profile."""

    username = username or f"user{next(_sequence)}"
    item = User.objects.create_user(
        username=username,
        password=password,
        is_staff=role in (RoleChoices.STAFF, RoleChoices.DIRECTOR),
    )
    if role == RoleChoices.STUDENT:
        year = academic_year()
        Student.objects.create(
            user=item,
            student_number=f"S-{username}",
            email=f"{username}@example.com",
            first_name=username,
            last_name="Student",
            date_of_birth="2009-01-01",
            grade_level=GRADE_LEVEL_12,
            academic_year=year,
        )
    elif role == RoleChoices.TEACHER:
        Teacher.objects.create(
            user=item,
            first_name=username,
            last_name="Teacher",
            email=f"{username}@example.com",
            department="General",
        )
    elif role == RoleChoices.COUNSELOR:
        Counselor.objects.create(
            user=item,
            first_name=username,
            last_name="Counselor",
            email=f"{username}@example.com",
        )
    else:
        UserRoleProfile.objects.create(user=item, role=role)
    return item


def academic_year(name=None):
    if name is None:
        value = next(_sequence)
        name = f"{2026 + value}-{2027 + value}"
    return AcademicYear.objects.create(name=name)


def course(code=None, *, name="Test Course", grade_level=GRADE_LEVEL_12, category=COURSE_CATEGORY_MATH):
    code = code or f"T{next(_sequence):03d}4U"
    return Course.objects.create(
        course_code=code,
        name=name,
        grade_level=grade_level,
        category=category,
        capacity_min=10,
        capacity_max=30,
    )


def teacher(username="teacher"):
    return user(username, role=RoleChoices.TEACHER).teacher_profile


def delivery_group(year, item_course, *, name=None):
    return DeliveryGroup.objects.create(
        academic_year=year,
        name=name or item_course.course_code,
        capacity_profile=item_course.capacity_profile,
    )


def section(year, item_course, *, semester=1, delivery=None, teacher_obj=None, **overrides):
    return Section.objects.create(
        course=item_course if delivery is None else None,
        delivery_group=delivery,
        academic_year=year,
        semester=semester,
        teacher=teacher_obj,
        capacity_min=item_course.capacity_profile.hard_min,
        capacity_max=item_course.capacity_profile.hard_max,
        section_number=overrides.pop("section_number", "S1-01"),
        **overrides,
    )
