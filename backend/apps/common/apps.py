"""Django application configuration for shared reference data."""

from django.apps import AppConfig


class CommonConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "backend.apps.common"
