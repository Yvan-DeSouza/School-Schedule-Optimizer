"""Django admin registration for planning audit and placement records."""

from django.contrib import admin

from backend.apps.scheduling.models import (
    SectionPlanningApproval,
    SectionPlanningApprovalCourse,
    SectionPlanningReconciliation,
    SectionPlanningReconciliationAction,
    SectionPlanningReconciliationCourse,
    SectionPlanningRun,
    SectionSchedule,
    TimeSlot,
)


# Admin is for development/operations inspection; public mutations remain behind
# the policy-protected APIs and transactional services.
admin.site.register([
    TimeSlot,
    SectionSchedule,
    SectionPlanningRun,
    SectionPlanningApproval,
    SectionPlanningApprovalCourse,
    SectionPlanningReconciliation,
    SectionPlanningReconciliationCourse,
    SectionPlanningReconciliationAction,
])
