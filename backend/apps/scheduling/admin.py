from django.contrib import admin

from backend.apps.scheduling.models import SectionSchedule, TimeSlot


admin.site.register([TimeSlot, SectionSchedule])
