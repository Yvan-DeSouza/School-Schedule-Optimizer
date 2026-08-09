"""Immutable student-assignment runs and transactional enrollment approval."""

from dataclasses import asdict

from django.db import transaction

from scheduling_engine.diagnostics import (
    STUDENT_ASSIGNMENT_INPUT_CHANGED_SINCE_RUN,
    STUDENT_ASSIGNMENT_STAFFING_CONTEXT_CHANGED_SINCE_RUN,
)
from scheduling_engine.student_assignment import solve_student_assignment

from backend.apps.common.exceptions import DomainConflictError, DomainValidationError
from backend.apps.scheduling.constants import (
    STUDENT_ASSIGNMENT_RUN_STATUS_COMPLETE,
    STUDENT_ASSIGNMENT_RUN_STATUS_FAILED,
    STUDENT_ASSIGNMENT_RUN_STATUS_INFEASIBLE,
    STUDENT_ASSIGNMENT_RUN_STATUS_PARTIAL,
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


def _snapshot(data, staffing_context):
    value = asdict(data)
    value["staffing_context"] = staffing_context
    value["fingerprint"] = placement_input_fingerprint(value)
    return value


def create_student_assignment_run(
    *, academic_year, staffing_mode, provisional_teacher_assignment_run=None,
    soft_constraint_importance, created_by,
):
    """Solve once against a detached target-year snapshot without writes."""

    data, staffing_context = load_student_assignment_input(
        academic_year_id=academic_year,
        staffing_mode=staffing_mode,
        provisional_teacher_assignment_run=provisional_teacher_assignment_run,
        soft_constraint_importance=soft_constraint_importance,
    )
    result = solve_student_assignment(data)
    status = {
        "complete": STUDENT_ASSIGNMENT_RUN_STATUS_COMPLETE,
        "partial": STUDENT_ASSIGNMENT_RUN_STATUS_PARTIAL,
        "infeasible": STUDENT_ASSIGNMENT_RUN_STATUS_INFEASIBLE,
    }.get(result.status, STUDENT_ASSIGNMENT_RUN_STATUS_FAILED)
    snapshot = _snapshot(data, staffing_context)
    return StudentAssignmentRun.objects.create(
        academic_year_id=academic_year,
        staffing_mode=staffing_mode,
        provisional_teacher_assignment_run=provisional_teacher_assignment_run,
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

    data, staffing_context = load_student_assignment_input(
        academic_year_id=run.academic_year_id,
        staffing_mode=run.staffing_mode,
        provisional_teacher_assignment_run=run.provisional_teacher_assignment_run,
        soft_constraint_importance={
            "section_utilization_balance": run.input_snapshot["section_utilization_balance_importance"],
            "student_semester_balance": run.input_snapshot["student_semester_balance_importance"],
            "course_sequence_preferences": run.input_snapshot["course_sequence_preferences_importance"],
        },
    )
    current = _snapshot(data, staffing_context)
    if current["fingerprint"] == run.input_snapshot.get("fingerprint"):
        return data, staffing_context
    stored_context = run.input_snapshot.get("staffing_context")
    if staffing_context != stored_context:
        raise StudentAssignmentConflictError({
            "code": STUDENT_ASSIGNMENT_STAFFING_CONTEXT_CHANGED_SINCE_RUN,
            "detail": "The declared staffing context changed since this run; create and review a new run.",
        })
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
    list(Enrollment.objects.select_for_update().filter(section_id__in=active_section_ids).order_by("section_id", "id"))
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
            course_request=request,
            assignment_basis=item["assignment_basis"],
            backup_resolution_snapshot=item.get("backup_resolution_snapshot") or {},
        )
    return approval
