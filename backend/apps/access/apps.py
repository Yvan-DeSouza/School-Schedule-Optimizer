"""Django application configuration for authorization policies."""

from django.apps import AppConfig


class AccessConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "backend.apps.access"
