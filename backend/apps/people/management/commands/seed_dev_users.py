"""Idempotently create local users representing every authorization role.

Passwords come only from ``DEV_USER_PASSWORD`` and are never printed. This
command is for local development data, not production account provisioning.
"""

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError

from backend.apps.common.models import AcademicYear
from backend.apps.people.models import (
    Counselor,
    RoleChoices,
    Student,
    Teacher,
    UserRoleProfile,
)
import os


def get_dev_user_password():
    """Read the required local seed password without exposing its value."""

    password = os.getenv("DEV_USER_PASSWORD")
    if not password:
        raise CommandError("DEV_USER_PASSWORD must be set in the environment or .env file.")
    return password


class Command(BaseCommand):
    help = "Create local development users for each supported role."

    def handle(self, *args, **options):
        # Store password only for this command invocation and reuse/get existing
        # domain records so rerunning the command is safe.
        self.password = get_dev_user_password()
        academic_year, _ = AcademicYear.objects.get_or_create(name="2026-2027")

        # Domain-profile users derive their roles from their linked profile.
        counselor_user = self._create_user("counselor", "counselor@example.com")
        Counselor.objects.update_or_create(
            email="counselor@example.com",
            defaults={
                "user": counselor_user,
                "first_name": "Casey",
                "last_name": "Counselor",
            },
        )

        teacher_user = self._create_user("teacher", "teacher@example.com")
        Teacher.objects.update_or_create(
            email="teacher@example.com",
            defaults={
                "user": teacher_user,
                "first_name": "Terry",
                "last_name": "Teacher",
                "department": "Mathematics",
            },
        )

        student_user = self._create_user("student", "student@example.com")
        Student.objects.update_or_create(
            student_number="S0001",
            defaults={
                "user": student_user,
                "email": "student@example.com",
                "first_name": "Sam",
                "last_name": "Student",
                "date_of_birth": "2009-01-01",
                "grade_level": 12,
                "academic_year": academic_year,
            },
        )

        # Staff/director/unknown lack dedicated domain models and therefore use
        # UserRoleProfile records.
        self._create_role_user("staff", "staff@example.com", RoleChoices.STAFF, is_staff=True)
        self._create_role_user(
            "director",
            "director@example.com",
            RoleChoices.DIRECTOR,
            is_staff=True,
        )
        self._create_role_user("unknown", "unknown@example.com", RoleChoices.UNKNOWN)

        self.stdout.write(self.style.SUCCESS("Development users are ready. Their password is the configured DEV_USER_PASSWORD value."))

    def _create_user(self, username, email, is_staff=False, is_superuser=False):
        """Create/update one auth user and always refresh its configured password."""

        user, created = User.objects.get_or_create(
            username=username,
            defaults={
                "email": email,
                "is_staff": is_staff,
                "is_superuser": is_superuser,
            },
        )
        user.email = email
        user.is_staff = is_staff
        user.is_superuser = is_superuser
        # set_password hashes the secret; never assign the raw value directly.
        user.set_password(self.password)
        user.save()
        return user

    def _create_role_user(self, username, email, role, is_staff=False):
        """Create an auth user plus explicit non-domain role profile."""

        user = self._create_user(username, email, is_staff=is_staff)
        UserRoleProfile.objects.update_or_create(
            user=user,
            defaults={"role": role},
        )
        return user
