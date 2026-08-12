"""Immutable semester/A-D placement runs and transactional approval writes."""

from dataclasses import asdict

from django.db import transaction

from scheduling_engine.diagnostics import (
    FIXED_CONTEXT_CHANGED_SINCE_RUN,
    PLACEMENT_INPUT_CHANGED_SINCE_RUN,
)
from scheduling_engine.section_placement import solve_section_placement

from backend.apps.common.exceptions import DomainConflictError, DomainValidationError
from backend.apps.scheduling.constants import (
    SECTION_PLACEMENT_INPUT_ANNUAL_TOTAL,
    SECTION_PLACEMENT_RUN_STATUS_COMPLETE,
    SECTION_PLACEMENT_RUN_STATUS_FAILED,
    SECTION_PLACEMENT_RUN_STATUS_INFEASIBLE,
    SECTION_PLACEMENT_RUN_STATUS_PARTIAL,
)
from backend.apps.scheduling.models import (
    AnnualPlacementLock,
    SectionBudgetApproval,
    SectionPlacementApproval,
    SectionPlacementApprovalAssignment,
    SectionPlacementRun,
    SectionSchedule,
    OnlineSupervisionSession,
)
from backend.apps.scheduling.services.engine_adapter import (
    load_section_placement_input,
    placement_input_fingerprint,
)


class SectionPlacementValidationError(DomainValidationError):
    """The placement request or frozen candidate is not approval-ready."""


class SectionPlacementConflictError(DomainConflictError):
    """Current input drifted, so an old recommendation cannot be applied."""


def _result_payload(result):
    """Turn only JSON-safe timing evidence into the persisted review payload."""

    return asdict(result)


def create_section_placement_run(*, academic_year_id, input_mode, budget_approval=None, created_by):
    """Create one synchronous timing recommendation; never write schedules here."""

    data, matrix, roster = load_section_placement_input(
        academic_year_id=academic_year_id,
        input_mode=input_mode,
        budget_approval=budget_approval,
    )
    result = solve_section_placement(data)
    status = {
        "complete": SECTION_PLACEMENT_RUN_STATUS_COMPLETE,
        "partial": SECTION_PLACEMENT_RUN_STATUS_PARTIAL,
        "infeasible": SECTION_PLACEMENT_RUN_STATUS_INFEASIBLE,
    }.get(result.status, SECTION_PLACEMENT_RUN_STATUS_FAILED)
    snapshot = asdict(data)
    snapshot["fingerprint"] = placement_input_fingerprint(snapshot)
    snapshot["matrix_revision"] = matrix.revision
    snapshot["roster_id"] = roster.id
    return SectionPlacementRun.objects.create(
        academic_year_id=academic_year_id,
        input_mode=input_mode,
        budget_approval=budget_approval,
        conflict_matrix=matrix,
        teacher_roster=roster,
        created_by=created_by,
        status=status,
        input_snapshot=snapshot,
        result=_result_payload(result),
        solver_metadata={
            "engine": "ortools-cp-sat",
            "time_limit_seconds": data.time_limit_seconds,
            "rooms_included": False,
            "teacher_assignments_persisted": False,
        },
    )


def _require_complete_unapproved(run):
    if run.status != SECTION_PLACEMENT_RUN_STATUS_COMPLETE or run.result.get("status") != "complete":
        raise SectionPlacementValidationError({"detail": "Only a complete placement result can be approved."})
    if hasattr(run, "approval"):
        raise SectionPlacementConflictError({"detail": "This placement run has already been approved."})


def _current_input_for_run(run):
    """Reload source facts and reject drift instead of silently recomputing.

    Approval must mean “accept this reviewed candidate.” Re-solving at write time
    could place a section differently than the counselor just reviewed.
    """

    data, matrix, roster = load_section_placement_input(
        academic_year_id=run.academic_year_id,
        input_mode=run.input_mode,
        budget_approval=run.budget_approval,
        conflict_matrix=run.conflict_matrix,
    )
    snapshot = asdict(data)
    fingerprint = placement_input_fingerprint(snapshot)
    if fingerprint != run.input_snapshot.get("fingerprint"):
        raise SectionPlacementConflictError({
            "code": PLACEMENT_INPUT_CHANGED_SINCE_RUN,
            "detail": "Placement input changed since this run; create and review a new run.",
        })
    if matrix.revision != run.input_snapshot.get("matrix_revision") or roster.id != run.input_snapshot.get("roster_id"):
        raise SectionPlacementConflictError({
            "code": FIXED_CONTEXT_CHANGED_SINCE_RUN,
            "detail": "The conflict matrix or confirmed roster changed since this run; create a new run.",
        })
    return data


def preview_section_placement_approval(run):
    """Return current approval readiness without changing operational state."""

    _require_complete_unapproved(run)
    try:
        _current_input_for_run(run)
    except ValueError as error:
        raise SectionPlacementConflictError({"detail": str(error)}) from error
    return {
        "approval_allowed": True,
        "assignment_count": len(run.result.get("assignments", [])),
        "assignments": run.result.get("assignments", []),
        "diagnostics": run.result.get("diagnostics", []),
        "staffing_summary": run.result.get("staffing_summary", {}),
        "rooms_included": False,
        "teacher_assignments_included": False,
    }


def _number_allocator(group_id, academic_year_id):
    """Reserve durable section labels before annual materialization.

    Annual mode rejects active rows for its delivery groups, but retired audit
    rows still reserve their labels. Reusing a number would make audit history
    ambiguous, so the allocator examines every historical group section.
    """

    from backend.apps.courses.models import Section

    used = set(Section.objects.filter(
        delivery_group_id=group_id, academic_year_id=academic_year_id,
    ).values_list("section_number", flat=True))
    sequences = {1: 1, 2: 1}
    for number in used:
        if number.startswith("S") and "-" in number:
            try:
                semester, sequence = number[1:].split("-", 1)
                sequences[int(semester)] = max(sequences[int(semester)], int(sequence) + 1)
            except (ValueError, KeyError):
                continue

    def allocate(semester):
        sequence = sequences[semester]
        candidate = f"S{semester}-{sequence:02d}"
        while candidate in used:
            sequence += 1
            candidate = f"S{semester}-{sequence:02d}"
        sequences[semester] = sequence + 1
        used.add(candidate)
        return candidate

    return allocate


@transaction.atomic
def approve_section_placement_run(run, *, approved_by, reason):
    """Materialize approved timing only after deterministic stale-state checks."""

    if not isinstance(reason, str) or not reason.strip():
        raise SectionPlacementValidationError({"reason": "An approval reason is required."})
    # ``budget_approval`` is nullable. PostgreSQL cannot lock the nullable side
    # of a select_related outer join, so lock the run row alone and let related
    # objects be read explicitly during the subsequent stale-state validation.
    run = SectionPlacementRun.objects.select_for_update().get(pk=run.pk)
    _require_complete_unapproved(run)
    try:
        _current_input_for_run(run)
    except ValueError as error:
        raise SectionPlacementConflictError({"detail": str(error)}) from error
    assignments = list(run.result.get("assignments", []))
    if not assignments:
        raise SectionPlacementValidationError({"detail": "A complete placement run has no timing assignments."})

    from backend.apps.control.models import SectionLock
    from backend.apps.courses.models import DeliveryGroup, Section

    # Lock decision rows in a stable order before checking absent schedules. The
    # same ordering is used for both modes to keep concurrent approval behavior
    # deterministic on PostgreSQL.
    section_ids = sorted(int(item["section_id"]) for item in assignments if item.get("section_id"))
    online_session_ids = sorted(
        int(item["online_supervision_session_id"])
        for item in assignments
        if item.get("online_supervision_session_id") is not None
    )
    sections = {
        section.id: section
        # A physical section may use either nullable delivery identity.  Lock
        # only the Section row: PostgreSQL cannot lock the nullable side of the
        # outer joins needed to read both identities, and neither related row
        # is mutated by this timing approval.
        for section in Section.objects.select_for_update(of=("self",)).filter(
            id__in=section_ids
        ).select_related("delivery_group", "course")
    }
    if SectionSchedule.objects.select_for_update().filter(section_id__in=section_ids).exists():
        raise SectionPlacementConflictError({"detail": "A section gained timing context since the placement run."})
    online_sessions = {
        session.id: session
        # ``plan_approval_session`` is nullable for legacy/session lifecycle
        # context.  Approval changes only the session's accepted time, so do
        # not ask PostgreSQL to lock that nullable outer-join side.
        for session in OnlineSupervisionSession.objects.select_for_update(of=("self",)).filter(
            id__in=online_session_ids,
            academic_year_id=run.academic_year_id,
            lifecycle_status="active",
        ).select_related("plan_approval_session")
    }
    if len(online_sessions) != len(online_session_ids):
        raise SectionPlacementConflictError({
            "detail": "An online supervision session from the reviewed run no longer exists or is inactive."
        })
    if any(session.timeslot_id for session in online_sessions.values()):
        raise SectionPlacementConflictError({
            "detail": "An online supervision session gained timing context since the placement run."
        })
    approval = SectionPlacementApproval.objects.create(
        placement_run=run, approved_by=approved_by, reason=reason.strip(),
    )
    groups = {
        group.id: group
        for group in DeliveryGroup.objects.select_for_update().filter(
            id__in={
                int(item["delivery_group_id"])
                for item in assignments
                if item.get("online_supervision_session_id") is None
            },
        ).select_related("capacity_profile").prefetch_related("offerings__course")
    }
    allocators = {}
    for item in assignments:
        online_session_id = item.get("online_supervision_session_id")
        if online_session_id is not None:
            session = online_sessions[int(online_session_id)]
            allowed = set(session.plan_approval_session.allowed_semesters)
            timeslot_id = int(item["timeslot_id"])
            from backend.apps.scheduling.models import TimeSlot

            timeslot = TimeSlot.objects.get(pk=timeslot_id)
            if timeslot.academic_year_id != run.academic_year_id or timeslot.semester not in allowed:
                raise SectionPlacementConflictError({
                    "detail": "The reviewed online supervision timing is no longer legal for its approved slot."
                })
            # This direct link supplies immutable placement provenance without
            # inventing a SectionPlacementApprovalAssignment for a non-section.
            session.timeslot_id = timeslot_id
            session.placement_approval = approval
            session.full_clean()
            session.save(update_fields=["timeslot", "placement_approval"])
            continue
        section_id = item.get("section_id")
        if section_id:
            section = sections.get(int(section_id))
            if section is None:
                raise SectionPlacementConflictError({"detail": "A fixed-semester section no longer exists."})
            if section.semester != int(item["semester"]):
                raise SectionPlacementConflictError({"detail": "A fixed-semester section changed semesters since the run."})
        else:
            if run.input_mode != SECTION_PLACEMENT_INPUT_ANNUAL_TOTAL:
                raise SectionPlacementValidationError({"detail": "Only annual placement may materialize a new section."})
            group = groups.get(int(item["delivery_group_id"]))
            if group is None:
                raise SectionPlacementConflictError({"detail": "A delivery group from the placement run no longer exists."})
            allocator = allocators.setdefault(group.id, _number_allocator(group.id, run.academic_year_id))
            members = list(group.offerings.all())
            section = Section.objects.create(
                course=members[0].course if len(members) == 1 else None,
                delivery_group=group,
                section_number=allocator(int(item["semester"])),
                academic_year_id=run.academic_year_id,
                semester=int(item["semester"]),
                capacity_min=group.capacity_profile.hard_min,
                capacity_max=group.capacity_profile.hard_max,
                annual_placement_approval=approval,
            )
            annual_index = item.get("annual_index")
            lock = AnnualPlacementLock.objects.select_for_update().filter(
                academic_year_id=run.academic_year_id, delivery_group=group,
                annual_index=annual_index,
            ).first()
            if lock:
                # The lock is copied to the real Section so later placement and
                # assignment stages see normal fixed context after materialization.
                SectionLock.objects.create(section=section, locked_timeslot_id=lock.locked_timeslot_id)
                section.is_locked = True
                section.save(update_fields=["is_locked"])
                lock.materialized_section = section
                lock.save(update_fields=["materialized_section", "updated_at"])
        line = SectionPlacementApprovalAssignment.objects.create(
            approval=approval, section=section, timeslot_id=item["timeslot_id"],
            annual_index=item.get("annual_index"),
        )
        # Rooms remain null by design. A timeslot-only accepted schedule is fixed
        # lifecycle context, but it is not a hidden room recommendation.
        SectionSchedule.objects.create(
            section=section, timeslot_id=item["timeslot_id"], room=None,
            placement_approval_assignment=line,
        )
    return approval
