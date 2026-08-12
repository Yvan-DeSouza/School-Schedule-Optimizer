"""Immutable named-teacher runs and transactional approval write-back."""

from dataclasses import asdict

from django.db import transaction

from scheduling_engine.diagnostics import (
    TEACHER_ASSIGNMENT_FIXED_CONTEXT_CHANGED_SINCE_RUN,
    TEACHER_ASSIGNMENT_INPUT_CHANGED_SINCE_RUN,
)
from scheduling_engine.teacher_assignment import solve_teacher_assignment

from backend.apps.common.exceptions import DomainConflictError, DomainValidationError
from backend.apps.scheduling.constants import (
    TEACHER_ASSIGNMENT_RUN_STATUS_COMPLETE,
    TEACHER_ASSIGNMENT_RUN_STATUS_FAILED,
    TEACHER_ASSIGNMENT_RUN_STATUS_INFEASIBLE,
    TEACHER_ASSIGNMENT_RUN_STATUS_PARTIAL,
)
from backend.apps.scheduling.models import (
    TeacherAssignmentApproval, TeacherAssignmentApprovalAssignment,
    TeacherAssignmentApprovalOnlineSupervision,
    TeacherAssignmentRun,
)
from backend.apps.scheduling.services.engine_adapter import (
    load_teacher_assignment_input, placement_input_fingerprint,
)


class TeacherAssignmentValidationError(DomainValidationError):
    """A teacher-assignment candidate is invalid or not approval-ready."""


class TeacherAssignmentConflictError(DomainConflictError):
    """Current planning facts drifted after a counselor reviewed a run."""


def create_teacher_assignment_run(*, academic_year_id, created_by):
    """Create a synchronous named-teacher recommendation without operational writes."""

    data, roster = load_teacher_assignment_input(academic_year_id=academic_year_id)
    result = solve_teacher_assignment(data)
    status = {
        "complete": TEACHER_ASSIGNMENT_RUN_STATUS_COMPLETE,
        "partial": TEACHER_ASSIGNMENT_RUN_STATUS_PARTIAL,
        "infeasible": TEACHER_ASSIGNMENT_RUN_STATUS_INFEASIBLE,
    }.get(result.status, TEACHER_ASSIGNMENT_RUN_STATUS_FAILED)
    snapshot = asdict(data)
    snapshot["fingerprint"] = placement_input_fingerprint(snapshot)
    snapshot["roster_id"] = roster.id
    return TeacherAssignmentRun.objects.create(
        academic_year_id=academic_year_id,
        teacher_roster=roster,
        created_by=created_by,
        status=status,
        input_snapshot=snapshot,
        result=asdict(result),
        solver_metadata={
            "engine": "ortools-cp-sat",
            "time_limit_seconds": data.time_limit_seconds,
            "rooms_included": False,
            "students_included": False,
            "placement_changes_allowed": False,
        },
    )


def _require_complete_unapproved(run):
    if run.status != TEACHER_ASSIGNMENT_RUN_STATUS_COMPLETE or run.result.get("status") != "complete":
        raise TeacherAssignmentValidationError({"detail": "Only a complete teacher assignment result can be approved."})
    if hasattr(run, "approval"):
        raise TeacherAssignmentConflictError({"detail": "This teacher assignment run has already been approved."})


def _current_input_for_run(run):
    """Reload and fingerprint all staffing facts instead of re-solving on approval."""

    data, roster = load_teacher_assignment_input(academic_year_id=run.academic_year_id)
    snapshot = asdict(data)
    if placement_input_fingerprint(snapshot) != run.input_snapshot.get("fingerprint"):
        raise TeacherAssignmentConflictError({
            "code": TEACHER_ASSIGNMENT_INPUT_CHANGED_SINCE_RUN,
            "detail": "Teacher-assignment input changed since this run; create and review a new run.",
        })
    if roster.id != run.input_snapshot.get("roster_id"):
        raise TeacherAssignmentConflictError({
            "code": TEACHER_ASSIGNMENT_FIXED_CONTEXT_CHANGED_SINCE_RUN,
            "detail": "The confirmed teacher roster changed since this run; create a new run.",
        })
    return data


def preview_teacher_assignment_approval(run):
    """Return current approval readiness and named review assignments without writes."""

    _require_complete_unapproved(run)
    try:
        _current_input_for_run(run)
    except ValueError as error:
        raise TeacherAssignmentConflictError({"detail": str(error)}) from error
    return {
        "approval_allowed": True,
        "assignment_count": len(run.result.get("assignments", [])),
        "assignments": run.result.get("assignments", []),
        "diagnostics": run.result.get("diagnostics", []),
        "objective_components": run.result.get("objective_components", {}),
        "rooms_included": False,
        "students_included": False,
    }


@transaction.atomic
def approve_teacher_assignment_run(run, *, approved_by, reason):
    """Atomically accept an unchanged complete candidate and set Section.teacher.

    A fresh solver result cannot replace the reviewed one inside this transaction:
    approval means accepting one audited recommendation, not requesting another.
    """

    if not isinstance(reason, str) or not reason.strip():
        raise TeacherAssignmentValidationError({"reason": "An approval reason is required."})
    run = TeacherAssignmentRun.objects.select_for_update().get(pk=run.pk)
    _require_complete_unapproved(run)
    assignments = list(run.result.get("assignments", []))
    section_ids = sorted(
        int(item["section_id"])
        for item in assignments
        if item.get("section_id") is not None
    )
    online_session_ids = sorted(
        int(item["online_supervision_session_id"])
        for item in assignments
        if item.get("online_supervision_session_id") is not None
    )

    from backend.apps.control.models import SectionLock
    from backend.apps.courses.models import Section
    from backend.apps.scheduling.models import (
        SectionSchedule, TeacherPlanningAnnualCapacity, TeacherPlanningCapacity,
        OnlineSupervisionSession,
    )

    # Deterministic lock order reduces concurrent approval races.  Absence of a
    # teacher is checked after locking so an external/manual assignment cannot
    # be overwritten between review and write-back.
    sections = {
        item.id: item
        for item in Section.objects.select_for_update().filter(id__in=section_ids).order_by("id")
    }
    list(SectionSchedule.objects.select_for_update().filter(section_id__in=section_ids).order_by("section_id"))
    list(SectionLock.objects.select_for_update().filter(section_id__in=section_ids).order_by("section_id"))
    online_sessions = {
        item.id: item
        for item in OnlineSupervisionSession.objects.select_for_update().filter(
            id__in=online_session_ids,
            academic_year_id=run.academic_year_id,
            lifecycle_status="active",
        ).order_by("id")
    }
    teacher_ids = sorted(int(item["teacher_id"]) for item in assignments)
    list(TeacherPlanningCapacity.objects.select_for_update().filter(
        academic_year_id=run.academic_year_id, teacher_id__in=teacher_ids,
    ).order_by("teacher_id", "semester"))
    list(TeacherPlanningAnnualCapacity.objects.select_for_update().filter(
        academic_year_id=run.academic_year_id, teacher_id__in=teacher_ids,
    ).order_by("teacher_id"))
    try:
        _current_input_for_run(run)
    except ValueError as error:
        raise TeacherAssignmentConflictError({"detail": str(error)}) from error
    if set(section_ids) != set(sections) or set(online_session_ids) != set(online_sessions):
        raise TeacherAssignmentConflictError({"detail": "A staffing unit from the teacher assignment run no longer exists."})
    if any(section.teacher_id is not None for section in sections.values()) or any(
        session.supervisor_id is not None for session in online_sessions.values()
    ):
        raise TeacherAssignmentConflictError({"detail": "A staffing unit gained a named teacher since the run."})

    approval = TeacherAssignmentApproval.objects.create(
        teacher_assignment_run=run, approved_by=approved_by, reason=reason.strip(),
    )
    for item in assignments:
        teacher_id = int(item["teacher_id"])
        if item.get("online_supervision_session_id") is not None:
            session = online_sessions[int(item["online_supervision_session_id"])]
            TeacherAssignmentApprovalOnlineSupervision.objects.create(
                approval=approval,
                online_supervision_session=session,
                teacher_id=teacher_id,
            )
            session.supervisor_id = teacher_id
            session.save(update_fields=["supervisor"])
            continue
        section = sections[int(item["section_id"])]
        TeacherAssignmentApprovalAssignment.objects.create(
            approval=approval, section=section, teacher_id=teacher_id,
        )
        section.teacher_id = teacher_id
        section.save(update_fields=["teacher"])
    return approval
