"""Django application configuration for manual scheduling control."""

from django.apps import AppConfig


class ControlConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "backend.apps.control"
