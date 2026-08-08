"""Django admin registration for domain and explicit role profiles."""

from django.contrib import admin

from backend.apps.people.models import (
    Counselor,
    Student,
    Teacher,
    TeacherStatusDecision,
    UserRoleProfile,
)


admin.site.register([Student, Teacher, TeacherStatusDecision, Counselor, UserRoleProfile])
