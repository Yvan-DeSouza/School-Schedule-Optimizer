from django.contrib import admin

from backend.apps.control.models import ManualOverride, SectionLock


admin.site.register([ManualOverride, SectionLock])
