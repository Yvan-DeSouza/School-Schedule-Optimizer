"""Django admin registration for stored translation entries."""

from django.contrib import admin

from backend.apps.translations.models import Translation


admin.site.register(Translation)
