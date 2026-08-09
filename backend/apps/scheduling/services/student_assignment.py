"""Immutable student-assignment runs and transactional enrollment approval."""

from dataclasses import asdict

from django.db import transaction

from scheduling_engine.dto import StudentAssignmentScopeDTO
from scheduling_engine.diagnostics import (
    STUDENT_ASSIGNMENT_INPUT_CHANGED_SINCE_RUN,
    STUDENT_ASSIGNMENT_STAFFING_CONTEXT_CHANGED_SINCE_RUN,
)
from scheduling_engine.student_assignment import solve_student_assignment

from backend.apps.common.exceptions import DomainConflictError, DomainValidationError
from backend.apps.courses.constants import ENROLLMENT_LIFECYCLE_ACTIVE, ENROLLMENT_LIFECYCLE_HISTORICAL
from backend.apps.courses.models import CourseOffering, CourseRequest, Enrollment, Section
from backend.apps.people.models import Student
from backend.apps.scheduling.codes import (
    STUDENT_ASSIGNMENT_RERUN_CONTEXT_CHANGED,
    STUDENT_ASSIGNMENT_RERUN_SCOPE_INVALID,
)
from backend.apps.scheduling.constants import (
    STUDENT_ASSIGNMENT_RUN_STATUS_COMPLETE,
    STUDENT_ASSIGNMENT_RUN_STATUS_COMPLETE,
    STUDENT_ASSIGNMENT_RUN_STATUS_FAILED,
    STUDENT_ASSIGNMENT_RUN_STATUS_INFEASIBLE,
    STUDENT_ASSIGNMENT_RUN_STATUS_PARTIAL,
    STUDENT_ASSIGNMENT_DEFAULT_MAX_PRIORITY_REQUESTS,
    STUDENT_ASSIGNMENT_RUN_SCOPE_FULL,
    STUDENT_ASSIGNMENT_RUN_SCOPE_SCOPED,
)
from backend.apps.scheduling.models import (
    StudentAssignmentApproval,
    StudentAssignmentApprovalEnrollment,
    StudentAssignmentRun,
)
from backend.apps.scheduling.services.engine_adapter import (
    load_student_assignment_input,
    placement_input_fingerprint,
)


class StudentAssignmentValidationError(DomainValidationError):
    """A student-assignment candidate is invalid or cannot be approved."""


class StudentAssignmentConflictError(DomainConflictError):
    """Current facts changed after a counselor reviewed a student run."""


def _scope_error(detail):
    return StudentAssignmentValidationError({
        "code": STUDENT_ASSIGNMENT_RERUN_SCOPE_INVALID,
        "detail": detail,
    })


def _resolve_scope(
    *, academic_year_id, scope_type, source_approval=None,
    student_ids=(), course_ids=(), section_ids=(),
):
    """Validate and freeze a partial rerun boundary before solving."""

    try:
        student_ids = tuple(sorted({int(value) for value in student_ids}))
        course_ids = tuple(sorted({int(value) for value in course_ids}))
        section_ids = tuple(sorted({int(value) for value in section_ids}))
    except (TypeError, ValueError) as error:
        raise _scope_error("Scope IDs must be positive integer identifiers.") from error
    if any(value <= 0 for value in (*student_ids, *course_ids, *section_ids)):
        raise _scope_error("Scope IDs must be positive integer identifiers.")

    if scope_type == STUDENT_ASSIGNMENT_RUN_SCOPE_FULL:
        if source_approval is not None or any((student_ids, course_ids, section_ids)):
            raise _scope_error("A full run cannot include a source approval or partial scope IDs.")
        return StudentAssignmentScopeDTO(scope_type=STUDENT_ASSIGNMENT_RUN_SCOPE_FULL)
    if scope_type != STUDENT_ASSIGNMENT_RUN_SCOPE_SCOPED:
        raise _scope_error("Student-assignment scope_type must be full or scoped.")
    if source_approval is None:
        raise _scope_error("A scoped rerun requires an accepted source approval.")
    if source_approval.student_assignment_run.academic_year_id != int(academic_year_id):
        raise _scope_error("The scoped rerun source approval belongs to another academic year.")
    if not any((student_ids, course_ids, section_ids)):
        raise _scope_error("A scoped rerun requires at least one student, course, or section ID.")

    valid_student_ids = set(Student.objects.filter(
        academic_year_id=academic_year_id,
    ).values_list("id", flat=True))
    valid_section_ids = set(Section.objects.filter(
        academic_year_id=academic_year_id,
        lifecycle_status="active",
    ).values_list("id", flat=True))
    valid_course_ids = set(CourseRequest.objects.filter(
        academic_year_id=academic_year_id,
    ).values_list("course_id", flat=True)) | set(CourseOffering.objects.filter(
        academic_year_id=academic_year_id,
    ).values_list("course_id", flat=True))
    if not set(student_ids) <= valid_student_ids:
        raise _scope_error("The scoped rerun contains a student outside the selected academic year.")
    if not set(section_ids) <= valid_section_ids:
        raise _scope_error("The scoped rerun may reference only active sections in the selected academic year.")
    if not set(course_ids) <= valid_course_ids:
        raise _scope_error("The scoped rerun contains a course without target-year planning context.")
    return StudentAssignmentScopeDTO(
        scope_type=STUDENT_ASSIGNMENT_RUN_SCOPE_SCOPED,
        student_ids=student_ids,
        course_ids=course_ids,
        section_ids=section_ids,
    )


def _snapshot_scope(snapshot):
    value = snapshot.get("scope") or {}
    return StudentAssignmentScopeDTO(
        scope_type=value.get("scope_type", STUDENT_ASSIGNMENT_RUN_SCOPE_FULL),
        student_ids=tuple(int(item) for item in value.get("student_ids", ())),
        course_ids=tuple(int(item) for item in value.get("course_ids", ())),
        section_ids=tuple(int(item) for item in value.get("section_ids", ())),
    )


def _relevant_lock_ids(lock, snapshot):
    """Return whether a lock can change an assignment inside this run's scope."""

    scope = _snapshot_scope(snapshot)
    if scope.scope_type == STUDENT_ASSIGNMENT_RUN_SCOPE_FULL:
        return True
    requests = [item for item in snapshot.get("requests", ()) if item.get("is_in_scope", True)]
    students = set(scope.student_ids) | {int(item["student_id"]) for item in requests}
    courses = set(scope.course_ids) | {int(item["course_id"]) for item in requests}
    offering_ids = {int(item["course_offering_id"]) for item in requests}
    sections = {
        int(item["section_id"])
        for item in snapshot.get("sections", ())
        if offering_ids.intersection(int(offering_id) for offering_id in item.get("member_course_offering_ids", ()))
    } | set(scope.section_ids)
    lock_type = lock.get("lock_type")
    if lock_type == "whole_student_schedule":
        return lock.get("student_id") in students
    if lock_type == "section_roster":
        return lock.get("section_id") in sections
    if lock_type == "course_roster":
        return lock.get("course_id") in courses
    if lock_type in {"exact_student_section", "student_teacher_course"}:
        return (
            (lock.get("student_id") in students and lock.get("course_id") in courses)
            or lock.get("section_id") in sections
        )
    if lock_type == "student_group_same_section":
        return bool(set(lock.get("member_student_ids", ())) & students) and lock.get("course_id") in courses
    return False


def _relevant_lock_context(snapshot):
    return tuple(sorted(
        (
            item for item in snapshot.get("student_assignment_locks", ())
            if _relevant_lock_ids(item, snapshot)
        ),
        key=lambda item: int(item["lock_id"]),
    ))


def _snapshot(data, staffing_context):
    value = asdict(data)
    value["staffing_context"] = staffing_context
    value["fingerprint"] = placement_input_fingerprint(value)
    return value


def _snapshot_fingerprint_without_locks(snapshot):
    """Compare ordinary input drift separately from unrelated lock changes."""

    comparable = dict(snapshot)
    comparable.pop("fingerprint", None)
    comparable.pop("student_assignment_locks", None)
    return placement_input_fingerprint(comparable)


def create_student_assignment_run(
    *, academic_year, staffing_mode, provisional_teacher_assignment_run=None,
    soft_constraint_importance, created_by,
    scope_type=STUDENT_ASSIGNMENT_RUN_SCOPE_FULL, source_approval=None,
    scope_student_ids=(), scope_course_ids=(), scope_section_ids=(),
    priority_request_ids=(), priority_request_limit=STUDENT_ASSIGNMENT_DEFAULT_MAX_PRIORITY_REQUESTS,
    schedule_preservation_level="none",
):
    """Solve once against a detached target-year snapshot without writes."""

    academic_year_id = academic_year.id if hasattr(academic_year, "id") else int(academic_year)
    scope = _resolve_scope(
        academic_year_id=academic_year_id,
        scope_type=scope_type,
        source_approval=source_approval,
        student_ids=scope_student_ids,
        course_ids=scope_course_ids,
        section_ids=scope_section_ids,
    )
    data, staffing_context = load_student_assignment_input(
        academic_year_id=academic_year_id,
        staffing_mode=staffing_mode,
        provisional_teacher_assignment_run=provisional_teacher_assignment_run,
        soft_constraint_importance=soft_constraint_importance,
        scope=scope,
        priority_request_ids=priority_request_ids,
        priority_request_limit=priority_request_limit,
        schedule_preservation_level=schedule_preservation_level,
    )
    result = solve_student_assignment(data)
    status = {
        "complete": STUDENT_ASSIGNMENT_RUN_STATUS_COMPLETE,
        "partial": STUDENT_ASSIGNMENT_RUN_STATUS_PARTIAL,
        "infeasible": STUDENT_ASSIGNMENT_RUN_STATUS_INFEASIBLE,
    }.get(result.status, STUDENT_ASSIGNMENT_RUN_STATUS_FAILED)
    snapshot = _snapshot(data, staffing_context)
    return StudentAssignmentRun.objects.create(
        academic_year_id=academic_year_id,
        staffing_mode=staffing_mode,
        provisional_teacher_assignment_run=provisional_teacher_assignment_run,
        source_approval=source_approval,
        scope_type=scope.scope_type,
        created_by=created_by,
        status=status,
        input_snapshot=snapshot,
        result=asdict(result),
        solver_metadata={
            "engine": "ortools-cp-sat",
            "time_limit_seconds": data.time_limit_seconds,
            "rooms_included": False,
            "teacher_assignments_changed": False,
            "existing_enrollments_mutated": False,
        },
    )


def _require_complete_unapproved(run):
    if run.status != STUDENT_ASSIGNMENT_RUN_STATUS_COMPLETE or run.result.get("status") != "complete":
        raise StudentAssignmentValidationError({
            "detail": "Only a complete student assignment result can be approved."
        })
    if hasattr(run, "approval"):
        raise StudentAssignmentConflictError({"detail": "This student assignment run has already been approved."})


def _current_input_for_run(run):
    """Reload once and reject data/staffing drift; never re-solve on approval."""

    snapshot = run.input_snapshot
    data, staffing_context = load_student_assignment_input(
        academic_year_id=run.academic_year_id,
        staffing_mode=run.staffing_mode,
        provisional_teacher_assignment_run=run.provisional_teacher_assignment_run,
        soft_constraint_importance={
            "section_utilization_balance": snapshot["section_utilization_balance_importance"],
            "student_semester_balance": snapshot["student_semester_balance_importance"],
            "course_sequence_preferences": snapshot["course_sequence_preferences_importance"],
        },
        scope=_snapshot_scope(snapshot),
        priority_request_ids=tuple(snapshot.get("priority_request_ids", ())),
        priority_request_limit=snapshot.get(
            "priority_request_limit",
            STUDENT_ASSIGNMENT_DEFAULT_MAX_PRIORITY_REQUESTS,
        ),
        schedule_preservation_level=snapshot.get("schedule_preservation_level", "none"),
    )
    current = _snapshot(data, staffing_context)
    if current["fingerprint"] == snapshot.get("fingerprint"):
        return data, staffing_context
    stored_context = snapshot.get("staffing_context")
    if staffing_context != stored_context:
        raise StudentAssignmentConflictError({
            "code": STUDENT_ASSIGNMENT_STAFFING_CONTEXT_CHANGED_SINCE_RUN,
            "detail": "The declared staffing context changed since this run; create and review a new run.",
        })
    if _relevant_lock_context(current) != _relevant_lock_context(snapshot):
        raise StudentAssignmentConflictError({
            "code": STUDENT_ASSIGNMENT_RERUN_CONTEXT_CHANGED,
            "detail": "A student-assignment lock affecting this run changed; create and review a new run.",
        })
    if _snapshot_fingerprint_without_locks(current) == _snapshot_fingerprint_without_locks(snapshot):
        # A lock unrelated to the approved scope may change during a long
        # planning cycle without invalidating this run's candidate.
        return data, staffing_context
    raise StudentAssignmentConflictError({
        "code": STUDENT_ASSIGNMENT_INPUT_CHANGED_SINCE_RUN,
        "detail": "Student-assignment input changed since this run; create and review a new run.",
    })


def preview_student_assignment_approval(run):
    """Return reviewable recommendation plus current approval readiness."""

    _require_complete_unapproved(run)
    try:
        _current_input_for_run(run)
    except ValueError as error:
        raise StudentAssignmentConflictError({"detail": str(error)}) from error
    return {
        "approval_allowed": True,
        "assignment_count": len(run.result.get("assignments", [])),
        "assignments": run.result.get("assignments", []),
        "unmet_requests": run.result.get("unmet_requests", []),
        "diagnostics": run.result.get("diagnostics", []),
        "objective_components": run.result.get("objective_components", {}),
        "sequence_outcomes": run.result.get("sequence_outcomes", []),
        "staffing_context": run.input_snapshot.get("staffing_context", {}),
    }


@transaction.atomic
def approve_student_assignment_run(run, *, approved_by, reason):
    """Accept one reviewed complete candidate and create all enrollments atomically."""

    if not isinstance(reason, str) or not reason.strip():
        raise StudentAssignmentValidationError({"reason": "An approval reason is required."})
    # Do not join the optional provisional run here: PostgreSQL cannot apply
    # FOR UPDATE to the nullable side of that outer join.  The source run, if
    # present, is locked explicitly below.
    run = StudentAssignmentRun.objects.select_for_update().get(pk=run.pk)
    _require_complete_unapproved(run)
    assignments = list(run.result.get("assignments", []))
    student_ids = sorted({int(item["student_id"]) for item in assignments})
    request_ids = sorted({int(item["request_id"]) for item in assignments})
    section_ids = sorted({int(item["section_id"]) for item in assignments})
    offering_ids = sorted({int(item["course_offering_id"]) for item in assignments})

    # The deterministic lock order protects the exact state the immutable
    # candidate relied on.  None of these locks authorizes a re-solve.
    from backend.apps.courses.models import (
        CourseOffering, CoursePrerequisite, CourseRequest, CourseSequencePreference,
        Enrollment, Section,
    )
    from backend.apps.people.models import Student
    from backend.apps.scheduling.models import SectionSchedule, TeacherAssignmentRun
    from backend.apps.scheduling.models import StudentAssignmentLock

    active_section_ids = list(Section.objects.filter(
        academic_year_id=run.academic_year_id,
        lifecycle_status="active",
    ).order_by("id").values_list("id", flat=True))
    active_request_ids = list(CourseRequest.objects.filter(
        academic_year_id=run.academic_year_id,
    ).order_by("id").values_list("id", flat=True))
    students = {
        item.id: item
        for item in Student.objects.select_for_update().filter(id__in=student_ids).order_by("id")
    }
    # Lock the whole target-year request and active-section context, not only
    # result rows: an added demand row or a changed unselected section must not
    # slip between drift validation and the enrollment transaction.
    all_requests = {
        item.id: item
        for item in CourseRequest.objects.select_for_update().filter(id__in=active_request_ids).order_by("id")
    }
    requests = {request_id: all_requests[request_id] for request_id in request_ids if request_id in all_requests}
    sections = {
        item.id: item
        for item in Section.objects.select_for_update().filter(id__in=active_section_ids).order_by("id")
    }
    all_offerings = {
        item.id: item
        for item in CourseOffering.objects.select_for_update().filter(
            academic_year_id=run.academic_year_id,
        ).order_by("id")
    }
    offerings = {offering_id: all_offerings[offering_id] for offering_id in offering_ids if offering_id in all_offerings}
    list(SectionSchedule.objects.select_for_update().filter(section_id__in=active_section_ids).order_by("section_id"))
    list(Enrollment.objects.select_for_update().filter(
        section_id__in=active_section_ids,
        lifecycle_status=ENROLLMENT_LIFECYCLE_ACTIVE,
    ).order_by("section_id", "id"))
    list(StudentAssignmentLock.objects.select_for_update().filter(
        academic_year_id=run.academic_year_id,
        is_active=True,
    ).order_by("id"))
    list(CoursePrerequisite.objects.select_for_update().order_by("id"))
    list(CourseSequencePreference.objects.select_for_update().order_by("id"))
    if run.provisional_teacher_assignment_run_id:
        TeacherAssignmentRun.objects.select_for_update().get(pk=run.provisional_teacher_assignment_run_id)
    try:
        _current_input_for_run(run)
    except ValueError as error:
        raise StudentAssignmentConflictError({"detail": str(error)}) from error
    if set(student_ids) != set(students) or set(request_ids) != set(requests) or not set(section_ids) <= set(sections) or set(offering_ids) != set(offerings):
        raise StudentAssignmentConflictError({"detail": "A student-assignment fact from the reviewed run no longer exists."})

    approval = StudentAssignmentApproval.objects.create(
        student_assignment_run=run,
        approved_by=approved_by,
        reason=reason.strip(),
    )
    for item in assignments:
        request = requests[int(item["request_id"])]
        section = sections[int(item["section_id"])]
        offering = offerings[int(item["course_offering_id"])]
        if request.student_id != int(item["student_id"]) or request.course_id != offering.course_id:
            raise StudentAssignmentConflictError({"detail": "Reviewed enrollment provenance no longer matches its request."})
        if section.delivery_group_id:
            valid = section.delivery_group.offerings.filter(pk=offering.id).exists()
        else:
            valid = section.course_id == offering.course_id
        if not valid:
            raise StudentAssignmentConflictError({"detail": "The offering no longer belongs to the selected physical section."})
        previous = None
        previous_enrollment_id = item.get("previous_enrollment_id")
        if previous_enrollment_id is not None:
            previous = Enrollment.objects.filter(
                pk=int(previous_enrollment_id),
                lifecycle_status=ENROLLMENT_LIFECYCLE_ACTIVE,
            ).first()
            if previous is None:
                raise StudentAssignmentConflictError({
                    "code": STUDENT_ASSIGNMENT_RERUN_CONTEXT_CHANGED,
                    "detail": "The active enrollment being replaced no longer exists.",
                })
            if (
                previous.student_id != request.student_id
                or previous.course_offering_id != offering.id
                or previous.section_id != int(item.get("previous_section_id"))
            ):
                raise StudentAssignmentConflictError({
                    "code": STUDENT_ASSIGNMENT_RERUN_CONTEXT_CHANGED,
                    "detail": "The reviewed replacement no longer matches its prior active enrollment.",
                })
            # Keeping the same section is not a replacement. The immutable run
            # may still document the recommendation, but no duplicate active
            # enrollment row should be written for an unchanged placement.
            if previous.section_id == section.id and previous.course_offering_id == offering.id:
                continue
            previous.lifecycle_status = ENROLLMENT_LIFECYCLE_HISTORICAL
            previous.save(update_fields=["lifecycle_status"])
        if Enrollment.objects.filter(
            student_id=request.student_id,
            course_offering_id=offering.id,
            lifecycle_status=ENROLLMENT_LIFECYCLE_ACTIVE,
        ).exists():
            raise StudentAssignmentConflictError({
                "code": STUDENT_ASSIGNMENT_RERUN_CONTEXT_CHANGED,
                "detail": "The student already has an active enrollment for the reviewed offering.",
            })
        enrollment = Enrollment(
            student_id=request.student_id,
            section=section,
            course_offering=offering,
        )
        # New solver writes must retain a precise offering-to-physical-section
        # relationship.  The database uniqueness constraint remains the final
        # concurrent-write safeguard.
        enrollment.full_clean()
        enrollment.save()
        StudentAssignmentApprovalEnrollment.objects.create(
            approval=approval,
            enrollment=enrollment,
            superseded_enrollment=previous,
            course_request=request,
            assignment_basis=item["assignment_basis"],
            backup_resolution_snapshot=item.get("backup_resolution_snapshot") or {},
        )
    return approval
