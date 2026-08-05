from django.contrib import admin

from backend.apps.common.models import AcademicYear, HistoricalCourseDemand, Room


admin.site.register([AcademicYear, Room, HistoricalCourseDemand])
