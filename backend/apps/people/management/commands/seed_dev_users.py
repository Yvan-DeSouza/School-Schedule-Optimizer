from django.contrib.auth.models import User
from django.core.management.base import BaseCommand

from backend.apps.common.models import AcademicYear
from backend.apps.people.models import (
    Counselor,
    RoleChoices,
    Student,
    Teacher,
    UserRoleProfile,
)
import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

DEFAULT_PASSWORD = os.getenv("DB_PASSWORD", "password123")  # Default password for seeded users


class Command(BaseCommand):
    help = "Create local development users for each supported role."

    def handle(self, *args, **options):
        academic_year, _ = AcademicYear.objects.get_or_create(name="2026-2027")

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

        self._create_role_user("staff", "staff@example.com", RoleChoices.STAFF, is_staff=True)
        self._create_role_user(
            "director",
            "director@example.com",
            RoleChoices.DIRECTOR,
            is_staff=True,
        )
        self._create_role_user("unknown", "unknown@example.com", RoleChoices.UNKNOWN)

        self.stdout.write(self.style.SUCCESS("Development users are ready."))
        self.stdout.write(f"Default password for all seeded users: {DEFAULT_PASSWORD}")

    def _create_user(self, username, email, is_staff=False, is_superuser=False):
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
        user.set_password(DEFAULT_PASSWORD)
        user.save()
        return user

    def _create_role_user(self, username, email, role, is_staff=False):
        user = self._create_user(username, email, is_staff=is_staff)
        UserRoleProfile.objects.update_or_create(
            user=user,
            defaults={"role": role},
        )
        return user
