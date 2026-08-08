"""Django application configuration for planning and placement records."""

from django.apps import AppConfig


class SchedulingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "backend.apps.scheduling"
