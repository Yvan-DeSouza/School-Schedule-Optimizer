"""Section lifecycle, deletion, and fixed-context helpers.

Future scheduling stages need one shared answer to: "can automation move or
replace this section?" This module owns that answer so placement, assignment,
reconciliation, and manual overrides do not drift apart.
"""

from collections import defaultdict

from backend.apps.control.models import ManualOverride, SectionLock
from backend.apps.courses.constants import ENROLLMENT_LIFECYCLE_ACTIVE
from backend.apps.courses.models import Enrollment
from backend.apps.scheduling.constants import SECTION_LIFECYCLE_RETIRED


FIXED_REASON_MANUAL_SECTION = "manual_section"
FIXED_REASON_ASSIGNED_TEACHER = "assigned_teacher"
FIXED_REASON_SECTION_FLAG_LOCKED = "section_flag_locked"
FIXED_REASON_SECTION_LOCK = "section_lock"
FIXED_REASON_SECTION_SCHEDULE = "section_schedule"
FIXED_REASON_ENROLLMENTS = "enrollments"
FIXED_REASON_ENROLLMENT_HISTORY = "enrollment_history"
FIXED_REASON_MANUAL_OVERRIDES = "manual_overrides"


def section_dependency_sets(section_ids):
    """Load dependency evidence for many sections in bounded queries."""

    from backend.apps.scheduling.models import SectionSchedule

    ids = list(section_ids)
    return {
        FIXED_REASON_SECTION_LOCK: set(
            SectionLock.objects.filter(section_id__in=ids).values_list(
                "section_id",
                flat=True,
            )
        ),
        FIXED_REASON_SECTION_SCHEDULE: set(
            SectionSchedule.objects.filter(section_id__in=ids).values_list(
                "section_id",
                flat=True,
            )
        ),
        FIXED_REASON_ENROLLMENTS: set(
            Enrollment.objects.filter(
                section_id__in=ids,
                lifecycle_status=ENROLLMENT_LIFECYCLE_ACTIVE,
            ).values_list(
                "section_id",
                flat=True,
            )
        ),
        FIXED_REASON_MANUAL_OVERRIDES: set(
            ManualOverride.objects.filter(section_id__in=ids).values_list(
                "section_id",
                flat=True,
            )
        ),
    }


def active_enrollment_students_by_section(section_ids):
    """Return active enrollment owners for cancellation diagnostics.

    Historical enrollment rows intentionally do not appear here: they explain
    what happened previously but do not prevent a section lifecycle change.
    """

    ids = list(section_ids)
    result = defaultdict(list)
    for section_id, student_id in Enrollment.objects.filter(
        section_id__in=ids,
        lifecycle_status=ENROLLMENT_LIFECYCLE_ACTIVE,
    ).values_list("section_id", "student_id"):
        result[section_id].append(student_id)
    return {section_id: tuple(sorted(student_ids)) for section_id, student_ids in result.items()}


def fixed_context_reasons(section, dependencies=None):
    """Return stable reason codes that make an active section fixed context."""

    if dependencies is None:
        dependencies = section_dependency_sets([section.id])

    reasons = []
    # A section may originate from section-count approval, staffing approval,
    # or annual placement materialization.  Only a section with none of those
    # auditable generated origins is considered manually created context.
    if (
        section.planning_approval_course_id is None
        and section.staffing_approval_offering_id is None
        and section.annual_placement_approval_id is None
    ):
        reasons.append(FIXED_REASON_MANUAL_SECTION)
    if section.teacher_id is not None:
        reasons.append(FIXED_REASON_ASSIGNED_TEACHER)
    if section.is_locked:
        reasons.append(FIXED_REASON_SECTION_FLAG_LOCKED)
    for reason in (
        FIXED_REASON_SECTION_LOCK,
        FIXED_REASON_SECTION_SCHEDULE,
        FIXED_REASON_ENROLLMENTS,
        FIXED_REASON_MANUAL_OVERRIDES,
    ):
        if section.id in dependencies[reason]:
            reasons.append(reason)
    return reasons


def is_fixed_context(section, dependencies=None):
    """Return whether downstream automation must treat this section as fixed."""

    return bool(fixed_context_reasons(section, dependencies))


def section_delete_conflicts(section):
    """Return reason codes that block hard deletion of a section."""

    conflicts = []
    if section.lifecycle_status == SECTION_LIFECYCLE_RETIRED:
        conflicts.append("retired_section")
    if section.planning_approval_course_id:
        conflicts.append("planning_generated")
    if section.staffing_approval_offering_id:
        conflicts.append("staffing_plan_generated")
    if section.annual_placement_approval_id:
        conflicts.append("annual_placement_generated")
    if section.planning_reconciliation_actions.exists():
        conflicts.append("reconciliation_audit")
    if section.teacher_id:
        conflicts.append(FIXED_REASON_ASSIGNED_TEACHER)
    if section.is_locked:
        conflicts.append(FIXED_REASON_SECTION_FLAG_LOCKED)

    dependencies = section_dependency_sets([section.id])
    conflicts.extend(
        reason
        for reason, section_ids in dependencies.items()
        if section.id in section_ids
    )
    # Historical rows no longer make a section fixed for planning, but they
    # remain audit evidence and therefore still prevent hard section deletion.
    if Enrollment.objects.filter(section_id=section.id).exists():
        conflicts.append(FIXED_REASON_ENROLLMENT_HISTORY)
    return conflicts
