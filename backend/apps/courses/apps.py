"""Django application configuration for courses and student demand."""

from django.apps import AppConfig


class CoursesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "backend.apps.courses"
