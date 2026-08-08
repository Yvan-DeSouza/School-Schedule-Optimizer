from django.contrib import admin

from backend.apps.scheduling.models import (
    SectionPlanningApproval,
    SectionPlanningApprovalCourse,
    SectionPlanningRun,
    SectionSchedule,
    TimeSlot,
)


admin.site.register([
    TimeSlot,
    SectionSchedule,
    SectionPlanningRun,
    SectionPlanningApproval,
    SectionPlanningApprovalCourse,
])
