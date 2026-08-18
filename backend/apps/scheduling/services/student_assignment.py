"""Immutable student-assignment runs, review, preview, and approval workflows."""

from dataclasses import asdict, replace
from collections import defaultdict

from django.db import transaction

from scheduling_engine.dto import StudentAssignmentScopeDTO
from scheduling_engine.diagnostics import (
    STUDENT_ASSIGNMENT_LOCKED_ENROLLMENT_BLOCKS_REQUEST,
    STUDENT_ASSIGNMENT_INPUT_CHANGED_SINCE_RUN,
    STUDENT_ASSIGNMENT_STAFFING_CONTEXT_CHANGED_SINCE_RUN,
    STUDENT_ASSIGNMENT_UNRESOLVED_REQUIRED_REQUEST,
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
    STUDENT_ASSIGNMENT_RUN_STATUS_FAILED,
    STUDENT_ASSIGNMENT_RUN_STATUS_INFEASIBLE,
    STUDENT_ASSIGNMENT_RUN_STATUS_PARTIAL,
    STUDENT_ASSIGNMENT_DEFAULT_MAX_PRIORITY_REQUESTS,
    STUDENT_ASSIGNMENT_SCHEDULE_PRESERVATION_NONE,
    STUDENT_ASSIGNMENT_RUN_SCOPE_FULL,
    STUDENT_ASSIGNMENT_RUN_SCOPE_SCOPED,
)
from backend.apps.scheduling.codes import STUDENT_ASSIGNMENT_WHAT_IF_LOCK_NOT_ACTIVE
from backend.apps.scheduling.models import (
    StudentAssignmentApproval,
    StudentAssignmentApprovalEnrollment,
    StudentAssignmentLock,
    StudentAssignmentRun,
    StudentSpecialCommitmentLock,
)
from backend.apps.scheduling.services.engine_adapter import (
    load_student_assignment_input,
    placement_input_fingerprint,
)
from backend.apps.scheduling.services.review_explanations import (
    EXPLANATION_FACTOR_CATEGORY_COUNSELOR_LOCK,
    EXPLANATION_FACTOR_CATEGORY_FIXED_CONTEXT,
    EXPLANATION_FACTOR_CATEGORY_HARD_CONSTRAINT,
    EXPLANATION_FACTOR_CATEGORY_INSUFFICIENT_EVIDENCE,
    EXPLANATION_FACTOR_CATEGORY_REVIEW_CONDITION,
    build_student_assignment_lock_impacts,
    build_student_assignment_review_summary,
    explanation_factor,
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
    def canonical(value):
        if isinstance(value, dict):
            return tuple(sorted((key, canonical(item)) for key, item in value.items()))
        if isinstance(value, (list, tuple)):
            return tuple(canonical(item) for item in value)
        return value

    return tuple(sorted(
        (
            canonical(item) for item in snapshot.get("student_assignment_locks", ())
            if _relevant_lock_ids(item, snapshot)
        ),
        key=lambda item: dict(item)["lock_id"],
    ))


def _snapshot(data, staffing_context):
    value = asdict(data)
    value["staffing_context"] = staffing_context
    value["fingerprint"] = placement_input_fingerprint(value)
    return value


def _importance_from_snapshot(snapshot):
    return {
        "section_utilization_balance": snapshot["section_utilization_balance_importance"],
        "student_semester_balance": snapshot["student_semester_balance_importance"],
        "course_sequence_preferences": snapshot["course_sequence_preferences_importance"],
        # Older immutable runs predate these optional preferences. Treat them
        # as disabled rather than making a historical review unreadable.
        "difficulty_balance": snapshot.get("difficulty_balance_importance", "not_important"),
        "course_category_diversity": snapshot.get("course_category_diversity_importance", "not_important"),
    }


def _selected_lock_ids_from_snapshot(snapshot):
    """Return the exact lock selection used by the immutable candidate."""

    return tuple(sorted(
        int(item.get("lock_id"))
        for item in snapshot.get("student_assignment_locks", ())
        if item.get("lock_id") is not None
    ))


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
    schedule_preservation_level=STUDENT_ASSIGNMENT_SCHEDULE_PRESERVATION_NONE,
    selected_lock_ids=None,
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
        selected_lock_ids=selected_lock_ids,
    )
    result = solve_student_assignment(data)
    status = {
        "complete": STUDENT_ASSIGNMENT_RUN_STATUS_COMPLETE,
        "partial": STUDENT_ASSIGNMENT_RUN_STATUS_PARTIAL,
        "infeasible": STUDENT_ASSIGNMENT_RUN_STATUS_INFEASIBLE,
    }.get(result.status, STUDENT_ASSIGNMENT_RUN_STATUS_FAILED)
    snapshot = _snapshot(data, staffing_context)
    # The DTO's lock list is the resolved, immutable record of what the run
    # honored.  Keeping this explicit makes a future configurable lock policy
    # auditable without changing the meaning of old runs.
    snapshot["selected_lock_ids"] = [item["lock_id"] for item in snapshot["student_assignment_locks"]]
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
    selected_lock_ids = _selected_lock_ids_from_snapshot(snapshot)
    try:
        data, staffing_context = load_student_assignment_input(
            academic_year_id=run.academic_year_id,
            staffing_mode=run.staffing_mode,
            provisional_teacher_assignment_run=run.provisional_teacher_assignment_run,
            soft_constraint_importance=_importance_from_snapshot(snapshot),
            scope=_snapshot_scope(snapshot),
            priority_request_ids=tuple(snapshot.get("priority_request_ids", ())),
            priority_request_limit=snapshot.get(
                "priority_request_limit",
                STUDENT_ASSIGNMENT_DEFAULT_MAX_PRIORITY_REQUESTS,
            ),
            schedule_preservation_level=snapshot.get("schedule_preservation_level", "none"),
            selected_lock_ids=selected_lock_ids,
        )
    except ValueError as error:
        # A selected lock being released is a workflow conflict, not an
        # unstructured adapter failure; clients need the stable rerun code.
        if "Selected student-assignment locks are not active" in str(error):
            raise StudentAssignmentConflictError({
                "code": STUDENT_ASSIGNMENT_RERUN_CONTEXT_CHANGED,
                "detail": "A selected student-assignment lock was released; create and review a new run.",
            }) from error
        raise
    current = _snapshot(data, staffing_context)
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
    # A lock added after a run was created may not be in the run's selected
    # snapshot.  It still invalidates approval when it affects the resolved
    # scope; silently ignoring it would let approval bypass a counselor's new
    # protection decision.
    current_active_locks = StudentAssignmentLock.objects.filter(
        academic_year_id=run.academic_year_id,
        is_active=True,
    ).prefetch_related("members").order_by("id")
    selected_ids = set(selected_lock_ids)
    for lock in current_active_locks:
        lock_value = {
            "lock_id": lock.id,
            "lock_type": lock.lock_type,
            "student_id": lock.student_id,
            "section_id": lock.section_id,
            "course_id": lock.course_id,
            "teacher_id": lock.teacher_id,
            "member_student_ids": tuple(lock.members.values_list("student_id", flat=True)),
        }
        if int(lock.id) not in selected_ids and _relevant_lock_ids(lock_value, snapshot):
            raise StudentAssignmentConflictError({
                "code": STUDENT_ASSIGNMENT_RERUN_CONTEXT_CHANGED,
                "detail": "A new student-assignment lock affects this run; create and review a new run.",
            })
    if current["fingerprint"] == snapshot.get("fingerprint"):
        return data, staffing_context
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
        data, _staffing_context = _current_input_for_run(run)
    except ValueError as error:
        raise StudentAssignmentConflictError({"detail": str(error)}) from error
    return _build_student_assignment_review(run, data=data)


def _assignment_key(item):
    """Return one physical destination identity without faking an online Section."""

    section_id = item.get("section_id")
    if section_id is not None:
        return int(item["request_id"]), ("section", int(section_id))
    return int(item["request_id"]), (
        "online_supervision",
        int(item["online_supervision_session_id"]),
    )


def _soft_priority_effects(data, result):
    """Use bounded counterfactual solves to report influence, not enablement."""

    result_value = asdict(result) if hasattr(result, "__dataclass_fields__") else result
    base_assignments = {_assignment_key(item) for item in result_value.get("assignments", ())}
    controls = (
        ("course_sequence_preferences", "course_sequence_preferences_importance"),
        ("section_utilization_balance", "section_utilization_balance_importance"),
        ("student_semester_balance", "student_semester_balance_importance"),
        ("difficulty_balance", "difficulty_balance_importance"),
        ("course_category_diversity", "course_category_diversity_importance"),
    )
    effects = {}
    for name, field_name in controls:
        importance = getattr(data, field_name)
        # Review should remain responsive even when the original solve used
        # the full engine budget.  This comparison is explanatory evidence,
        # not a replacement recommendation; the immutable run result remains
        # authoritative if the bounded counterfactual cannot finish.
        disabled = replace(
            data,
            time_limit_seconds=min(data.time_limit_seconds, 0.5),
            **{field_name: "not_important"},
        )
        counterfactual = solve_student_assignment(
            disabled,
            use_hard_feasibility_bootstrap=False,
        )
        counterfactual_assignments = {_assignment_key(item) for item in asdict(counterfactual).get("assignments", ())}
        effects[name] = {
            "importance": importance,
            "influenced": importance != "not_important" and counterfactual_assignments != base_assignments,
        }
    return effects


def _candidate_evidence_factor(fact, *, review_item_codes=()):
    """Map engine facts into the shared review vocabulary without narrating.

    The engine emits stable diagnostic codes and opaque IDs only.  This Django
    service owns the counselor-facing category label, including the honest
    ``insufficient_evidence`` outcome for an unselected alternative that has
    no recorded final hard blocker.
    """

    if fact.get("kind") == "fixed_context":
        return explanation_factor(
            category=EXPLANATION_FACTOR_CATEGORY_FIXED_CONTEXT,
            key="student_assignment_fixed_context",
            facts={},
        )
    if fact.get("kind") == "insufficient_evidence":
        return explanation_factor(
            category=EXPLANATION_FACTOR_CATEGORY_INSUFFICIENT_EVIDENCE,
            key="candidate_not_selected_without_recorded_hard_block",
            facts={},
        )
    code = fact.get("code")
    if not code:
        return explanation_factor(
            category=EXPLANATION_FACTOR_CATEGORY_INSUFFICIENT_EVIDENCE,
            key="candidate_evidence_not_available",
            facts={},
        )
    if fact.get("blocking_lock_id") is not None or code in {
        "student_assignment_locked_enrollment_blocks_request",
        "student_assignment_special_commitment_lock_blocks_request",
    }:
        category = EXPLANATION_FACTOR_CATEGORY_COUNSELOR_LOCK
    elif code in set(review_item_codes):
        category = EXPLANATION_FACTOR_CATEGORY_REVIEW_CONDITION
    else:
        category = EXPLANATION_FACTOR_CATEGORY_HARD_CONSTRAINT
    return explanation_factor(
        category=category,
        key=code,
        facts={
            key: value
            for key, value in fact.items()
            if key not in {"code", "phase", "kind"}
        },
    )


def _candidate_ledger_explanation(entry):
    """Attach category-labelled why/alternative facts to one stored ledger row."""

    review_item_codes = entry.get("review_item_codes", ())
    alternatives = []
    for candidate in entry.get("alternatives", ()):
        reasons = list(candidate.get("static_rejections", ())) + list(
            candidate.get("final_rejections", ())
        )
        alternatives.append({
            **candidate,
            "why": [
                _candidate_evidence_factor(
                    reason,
                    review_item_codes=review_item_codes,
                )
                for reason in reasons
            ] or [
                _candidate_evidence_factor(
                    {"kind": "insufficient_evidence"},
                    review_item_codes=review_item_codes,
                )
            ],
        })
    return {
        "why": [
            _candidate_evidence_factor(
                item,
                review_item_codes=review_item_codes,
            )
            for item in entry.get("selection_factors", ())
        ] or [
            _candidate_evidence_factor(
                {"kind": "insufficient_evidence"},
                review_item_codes=review_item_codes,
            )
        ],
        "alternatives": alternatives,
    }


def _build_student_assignment_review(run, *, data):
    """Build a stable counselor review shape from the immutable stored result."""

    result = run.result
    assignments = list(result.get("assignments", ()))
    commitment_assignments = list(result.get("commitment_assignments", ()))
    unmet = list(result.get("unmet_requests", ()))
    candidate_ledger = list(result.get("candidate_ledger", ()))
    new_assignments = [item for item in assignments if item.get("previous_enrollment_id") is None]
    changed_assignments = [
        item for item in assignments
        if item.get("previous_enrollment_id") is not None
        and (
            item.get("previous_section_id") != item.get("section_id")
            or item.get("previous_online_supervision_session_id")
            != item.get("online_supervision_session_id")
        )
    ]
    unchanged_assignments = [
        item for item in assignments
        if item.get("previous_enrollment_id") is not None
        and item.get("previous_section_id") == item.get("section_id")
        and item.get("previous_online_supervision_session_id")
        == item.get("online_supervision_session_id")
    ]
    protected_assignments = [
        item for item in unmet
        if item.get("blocking_lock_id") is not None
        or item.get("diagnostic_code") == STUDENT_ASSIGNMENT_LOCKED_ENROLLMENT_BLOCKS_REQUEST
    ]
    # A locked active enrollment is intentionally omitted from the engine's
    # decision variables.  It still belongs in the counselor-facing protected
    # category so review explains why the existing placement was preserved.
    protected_assignments.extend(
        {
            "student_id": row.student_id,
            "course_id": row.course_id,
            "section_id": row.section_id,
            "enrollment_id": row.enrollment_id,
            "blocking_lock_ids": list(row.lock_ids),
            "reason_code": STUDENT_ASSIGNMENT_LOCKED_ENROLLMENT_BLOCKS_REQUEST,
        }
        for row in data.fixed_enrollments
        if row.is_active and row.is_locked and row.lock_ids
    )
    difficulty_by_course = {item.course_id: item.effective_difficulty for item in data.course_difficulties}
    credit_by_course_request = {
        item.request_id: item.credit_value for item in data.requests
    }
    focus_student_ids = {
        item.student_id
        for item in data.schedule_commitment_requests
        if item.commitment_type == "focus"
    } | {
        item.student_id
        for item in data.fixed_schedule_commitments
        if item.is_active and not item.is_historical and item.commitment_kind == "focus"
    }
    difficulty_totals = defaultdict(lambda: {"semester_1_difficulty": 0, "semester_2_difficulty": 0})
    for assignment in assignments:
        if int(assignment["student_id"]) in focus_student_ids:
            continue
        key = "semester_1_difficulty" if assignment["semester"] == 1 else "semester_2_difficulty"
        difficulty_totals[int(assignment["student_id"])][key] += round(
            difficulty_by_course.get(int(assignment["course_id"]), 0)
            * credit_by_course_request.get(int(assignment["request_id"]), 1.0)
        )
    for row in data.fixed_enrollments:
        if (
            row.is_active
            and not row.is_historical
            and row.student_id not in focus_student_ids
        ):
            key = "semester_1_difficulty" if row.semester == 1 else "semester_2_difficulty"
            difficulty_totals[row.student_id][key] += round(
                difficulty_by_course.get(row.course_id, 0) * row.credit_value
            )
    difficulty_balance = [
        {"student_id": student_id, **totals, "difficulty_imbalance": abs(totals["semester_1_difficulty"] - totals["semester_2_difficulty"])}
        for student_id, totals in sorted(difficulty_totals.items())
    ]
    student_ids_for = lambda rows: len({int(item["student_id"]) for item in rows if item.get("student_id") is not None})
    review = {
        "approval_allowed": True,
        "assignment_count": len(assignments),
        "assignments": assignments,
        "commitment_assignments": commitment_assignments,
        "special_commitment_review_items": result.get("review_items", []),
        "unmet_requests": unmet,
        # Keep the immutable engine evidence available to the run review. The
        # per-student endpoint below presents the same data with the shared
        # why/alternatives vocabulary rather than re-solving the schedule.
        "candidate_ledger": candidate_ledger,
        "diagnostics": result.get("diagnostics", []),
        "objective_components": result.get("objective_components", {}),
        "optimization_facts": result.get("optimization_facts", {}),
        "course_difficulty_facts": [asdict(item) for item in data.course_difficulties],
        "student_difficulty_balance": difficulty_balance,
        "sequence_outcomes": result.get("sequence_outcomes", []),
        "lock_costs": result.get("lock_costs", []),
        "seat_contention": result.get("seat_contention", []),
        "section_balance_facts": result.get("section_balance_facts", []),
        "new_assignments": new_assignments,
        "changed_assignments": changed_assignments,
        "protected_assignments": protected_assignments,
        "unchanged_assignment_count": len(unchanged_assignments),
        "moved_assignment_count": len(changed_assignments),
        "unchanged_student_count": student_ids_for(unchanged_assignments),
        "moved_student_count": student_ids_for(changed_assignments),
        "soft_priorities": _soft_priority_effects(data, result),
        "staffing_context": run.input_snapshot.get("staffing_context", {}),
    }
    # Level 1 lock impact is deliberately derived from the reviewed snapshot
    # and current active lock records only. It reports the lock's direct target
    # and frozen occupancy; it does not pretend to predict downstream changes
    # without a separate, explicitly requested what-if solve.
    ordinary_lock_details = {
        item["id"]: {"reason": item["reason"]}
        for item in StudentAssignmentLock.objects.filter(
            id__in=[lock.lock_id for lock in data.student_assignment_locks],
            is_active=True,
        ).values("id", "reason")
    }
    special_lock_details = {
        item["id"]: {"reason": item["reason"]}
        for item in StudentSpecialCommitmentLock.objects.filter(
            id__in=[lock.lock_id for lock in data.special_commitment_locks],
            is_active=True,
        ).values("id", "reason")
    }
    review["lock_impacts"] = build_student_assignment_lock_impacts(
        data=data,
        result=result,
        ordinary_lock_details=ordinary_lock_details,
        special_lock_details=special_lock_details,
    )
    review["review_summary"] = build_student_assignment_review_summary(run, data, review)
    return review


def student_assignment_student_explanation(run, *, student_id):
    """Return only planning-safe course outcome facts for one student."""

    data, _staffing_context = _current_input_for_run(run)
    student_id = int(student_id)
    snapshot = run.input_snapshot
    request_rows = [item for item in snapshot.get("requests", ()) if int(item["student_id"]) == student_id]
    result_assignments = {
        int(item["request_id"]): item
        for item in run.result.get("assignments", ())
        if int(item["student_id"]) == student_id
    }
    unmet = {
        int(item["request_id"]): item
        for item in run.result.get("unmet_requests", ())
        if int(item["student_id"]) == student_id
    }
    course_candidate_ledger = {
        int(item["request_id"]): item
        for item in run.result.get("candidate_ledger", ())
        if int(item["student_id"]) == student_id
        and item.get("course_id") is not None
    }
    commitment_candidate_ledger = {
        int(item["request_id"]): item
        for item in run.result.get("candidate_ledger", ())
        if int(item["student_id"]) == student_id
        and item.get("course_id") is None
    }
    sections = {int(item["section_id"]): item for item in snapshot.get("sections", ())}
    timeslot_ids = {
        int(item["timeslot_id"])
        for item in sections.values()
        if item.get("timeslot_id") is not None
    } | {
        int(timeslot_id)
        for item in run.result.get("commitment_assignments", ())
        if int(item["student_id"]) == student_id
        for timeslot_id, _segment in item.get("occupancy", ())
    }
    from backend.apps.courses.models import Course
    from backend.apps.scheduling.models import TimeSlot
    courses = {
        item.id: item
        for item in Course.objects.filter(id__in={int(row["course_id"]) for row in request_rows})
    }
    timeslots = {
        item.id: item
        for item in TimeSlot.objects.filter(id__in=timeslot_ids)
    }
    fixed_rows = [
        row for row in snapshot.get("fixed_enrollments", ())
        if int(row["student_id"]) == student_id and row.get("is_active", True)
    ]
    fixed_by_course = {int(row["course_id"]): row for row in fixed_rows}
    rows = []
    for request in request_rows:
        request_id = int(request["request_id"])
        assignment = result_assignments.get(request_id)
        course_id = int(request["course_id"])
        engine_section_id = None
        if assignment:
            if assignment.get("section_id") is not None:
                engine_section_id = int(assignment["section_id"])
            elif assignment.get("online_supervision_session_id") is not None:
                # The immutable input uses a negative, engine-only identity to
                # model shared online capacity without claiming it is a normal
                # instructional Section. Translate it only for lookup here.
                engine_section_id = -int(assignment["online_supervision_session_id"])
        section = sections.get(engine_section_id) if engine_section_id is not None else None
        if section is None and course_id in fixed_by_course:
            section = sections.get(int(fixed_by_course[course_id]["section_id"]))
        timeslot = timeslots.get(int(section["timeslot_id"])) if section else None
        unmet_row = unmet.get(request_id)
        blocker = unmet_row or fixed_by_course.get(course_id, {})
        row = {
            "request_id": request_id,
            "course_id": course_id,
            "course_code": courses.get(course_id).course_code if courses.get(course_id) else None,
            "received": bool(assignment or course_id in fixed_by_course),
            "section_id": (
                assignment.get("section_id")
                if assignment
                else section.get("section_id") if section else None
            ),
            "online_supervision_session_id": (
                assignment.get("online_supervision_session_id") if assignment else None
            ),
            "semester": section.get("semester") if section else None,
            "timeslot_id": section.get("timeslot_id") if section else None,
            "block": timeslot.block if timeslot else None,
            "reason_code": unmet_row.get("diagnostic_code") if unmet_row else None,
            "lock_or_freeze_affected": bool(
                blocker.get("blocking_lock_id") is not None or blocker.get("lock_ids")
            ),
            "blocking_lock_id": blocker.get("blocking_lock_id") or (
                blocker.get("lock_ids") or [None]
            )[0],
        }
        if not row["received"] and row["reason_code"] is None:
            row["reason_code"] = STUDENT_ASSIGNMENT_UNRESOLVED_REQUIRED_REQUEST
        candidate_evidence = course_candidate_ledger.get(request_id)
        if candidate_evidence is not None:
            row.update(_candidate_ledger_explanation(candidate_evidence))
            row["candidate_summary"] = {
                key: candidate_evidence.get(key)
                for key in (
                    "selection_state", "unresolved_reason_code",
                    "static_candidate_count", "statically_eligible_candidate_count",
                    "recorded_rejected_candidate_count",
                    "omitted_rejected_candidate_count",
                    "selected_candidate", "review_item_codes",
                )
            }
        rows.append(row)
    commitments = []
    for commitment in run.result.get("commitment_assignments", ()):
        if int(commitment["student_id"]) != student_id:
            continue
        occupancy = [
            {
                "timeslot_id": int(timeslot_id),
                "block": timeslots.get(int(timeslot_id)).block if timeslots.get(int(timeslot_id)) else None,
                "semester": timeslots.get(int(timeslot_id)).semester if timeslots.get(int(timeslot_id)) else None,
                "half_semester_segment": segment,
            }
            for timeslot_id, segment in commitment.get("occupancy", ())
        ]
        commitments.append({
            "request_id": int(commitment["request_id"]),
            "commitment_kind": commitment["commitment_kind"],
            "course_request_id": commitment.get("course_request_id"),
            "course_offering_id": commitment.get("course_offering_id"),
            "occupancy": occupancy,
            **_candidate_ledger_explanation(
                (
                    course_candidate_ledger.get(int(commitment["request_id"]))
                    if commitment.get("course_request_id") is not None
                    else commitment_candidate_ledger.get(int(commitment["request_id"]))
                ) or {"selection_factors": ({"kind": "insufficient_evidence"},)}
            ),
        })
    return {
        "run_id": run.id,
        "student_id": student_id,
        "requests": rows,
        "commitments": commitments,
        "special_commitment_review_items": [
            item for item in run.result.get("review_items", ())
            if int(item["student_id"]) == student_id
        ],
    }


def preview_student_assignment_unlock(run, *, lock_ids):
    """Solve a hypothetical unlocked input without creating a run or writing state."""

    try:
        lock_ids = tuple(sorted({int(lock_id) for lock_id in lock_ids}))
    except (TypeError, ValueError) as error:
        raise StudentAssignmentValidationError({
            "code": STUDENT_ASSIGNMENT_WHAT_IF_LOCK_NOT_ACTIVE,
            "detail": "Lock IDs must be positive integer identifiers.",
        }) from error
    if not lock_ids or any(lock_id <= 0 for lock_id in lock_ids):
        raise StudentAssignmentValidationError({
            "code": STUDENT_ASSIGNMENT_WHAT_IF_LOCK_NOT_ACTIVE,
            "detail": "At least one active lock ID is required.",
        })
    active_ids = set(StudentAssignmentLock.objects.filter(
        academic_year_id=run.academic_year_id,
        is_active=True,
    ).values_list("id", flat=True))
    if not set(lock_ids) <= active_ids:
        raise StudentAssignmentValidationError({
            "code": STUDENT_ASSIGNMENT_WHAT_IF_LOCK_NOT_ACTIVE,
            "detail": "Every what-if lock must be active in the run's academic year.",
        })
    snapshot = run.input_snapshot
    selected_ids = set(_selected_lock_ids_from_snapshot(snapshot))
    data, _staffing_context = _current_input_for_run(run)
    remaining_ids = tuple(sorted(selected_ids - set(lock_ids)))
    unlocked_data, _ = load_student_assignment_input(
        academic_year_id=run.academic_year_id,
        staffing_mode=run.staffing_mode,
        provisional_teacher_assignment_run=run.provisional_teacher_assignment_run,
        soft_constraint_importance=_importance_from_snapshot(snapshot),
        scope=_snapshot_scope(snapshot),
        priority_request_ids=tuple(snapshot.get("priority_request_ids", ())),
        priority_request_limit=snapshot.get("priority_request_limit", STUDENT_ASSIGNMENT_DEFAULT_MAX_PRIORITY_REQUESTS),
        schedule_preservation_level=snapshot.get("schedule_preservation_level", STUDENT_ASSIGNMENT_SCHEDULE_PRESERVATION_NONE),
        selected_lock_ids=remaining_ids,
    )
    result = solve_student_assignment(unlocked_data)
    before = list(run.result.get("assignments", ()))
    after = list(asdict(result).get("assignments", ()))
    before_by_request = {int(item["request_id"]): item for item in before}
    after_by_request = {int(item["request_id"]): item for item in after}
    changed = [
        {"request_id": request_id, "before": before_by_request.get(request_id), "after": after_by_request.get(request_id)}
        for request_id in sorted(set(before_by_request) | set(after_by_request))
        if before_by_request.get(request_id) != after_by_request.get(request_id)
    ]
    return {
        "run_id": run.id,
        "removed_lock_ids": list(lock_ids),
        "before": {"assignments": before, "unmet_requests": run.result.get("unmet_requests", ())},
        "after": {"assignments": after, "unmet_requests": asdict(result).get("unmet_requests", ())},
        "changed_requests": changed,
        "diagnostics": asdict(result).get("diagnostics", ()),
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
    commitment_assignments = list(run.result.get("commitment_assignments", []))
    student_ids = sorted({
        int(item["student_id"])
        for item in assignments + commitment_assignments
    })
    request_ids = sorted({int(item["request_id"]) for item in assignments} | {
        int(item["course_request_id"])
        for item in commitment_assignments
        if item.get("course_request_id") is not None
    })
    schedule_commitment_request_ids = sorted({
        int(item["request_id"])
        for item in commitment_assignments
        if item.get("course_request_id") is None
    })
    section_ids = sorted({
        int(item["section_id"])
        for item in assignments
        if item.get("section_id") is not None
    })
    online_session_ids = sorted({
        int(item["online_supervision_session_id"])
        for item in assignments
        if item.get("online_supervision_session_id") is not None
    })
    offering_ids = sorted({int(item["course_offering_id"]) for item in assignments} | {
        int(item["course_offering_id"])
        for item in commitment_assignments
        if item.get("course_offering_id") is not None
    })

    # The deterministic lock order protects the exact state the immutable
    # candidate relied on.  None of these locks authorizes a re-solve.
    from backend.apps.courses.models import (
        CourseOffering, CoursePrerequisite, CourseRequest, CourseSequencePreference,
        Enrollment, Section,
    )
    from backend.apps.people.models import Student
    from backend.apps.scheduling.models import (
        SectionSchedule, TeacherAssignmentRun, StudentAssignmentLock,
        OnlineEnrollment, OnlineSupervisionSession,
        StudentAssignmentApprovalOnlineEnrollment,
        StudentScheduleCommitment, StudentScheduleCommitmentOccupancy,
        StudentAssignmentApprovalCommitment, StudentSpecialCommitmentLock,
    )

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
    from backend.apps.courses.models import StudentScheduleCommitmentRequest

    schedule_commitment_requests = {
        item.id: item
        for item in StudentScheduleCommitmentRequest.objects.select_for_update().filter(
            id__in=schedule_commitment_request_ids,
        ).order_by("id")
    }
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
    online_sessions = {
        item.id: item
        for item in OnlineSupervisionSession.objects.select_for_update().filter(
            id__in=online_session_ids,
            academic_year_id=run.academic_year_id,
            lifecycle_status="active",
        ).order_by("id")
    }
    list(OnlineEnrollment.objects.select_for_update().filter(
        supervision_session_id__in=online_session_ids,
        lifecycle_status="active",
    ).order_by("supervision_session_id", "id"))
    list(StudentAssignmentLock.objects.select_for_update().filter(
        academic_year_id=run.academic_year_id,
        is_active=True,
    ).order_by("id"))
    list(StudentSpecialCommitmentLock.objects.select_for_update().filter(
        academic_year_id=run.academic_year_id,
        is_active=True,
    ).order_by("id"))
    existing_commitments = list(StudentScheduleCommitment.objects.select_for_update().filter(
        academic_year_id=run.academic_year_id,
        lifecycle_status="active",
    ).prefetch_related("occupancies").order_by("id"))
    list(StudentScheduleCommitmentOccupancy.objects.select_for_update().filter(
        commitment__in=existing_commitments,
    ).order_by("commitment_id", "timeslot_id", "half_semester_segment"))
    list(CoursePrerequisite.objects.select_for_update().order_by("id"))
    list(CourseSequencePreference.objects.select_for_update().order_by("id"))
    if run.provisional_teacher_assignment_run_id:
        TeacherAssignmentRun.objects.select_for_update().get(pk=run.provisional_teacher_assignment_run_id)
    try:
        _current_input_for_run(run)
    except ValueError as error:
        raise StudentAssignmentConflictError({"detail": str(error)}) from error
    if (
        set(student_ids) != set(students)
        or set(request_ids) != set(requests)
        or not set(section_ids) <= set(sections)
        or set(online_session_ids) != set(online_sessions)
        or set(offering_ids) != set(offerings)
        or set(schedule_commitment_request_ids) != set(schedule_commitment_requests)
    ):
        raise StudentAssignmentConflictError({"detail": "A student-assignment fact from the reviewed run no longer exists."})

    approval = StudentAssignmentApproval.objects.create(
        student_assignment_run=run,
        approved_by=approved_by,
        reason=reason.strip(),
    )
    for item in assignments:
        request = requests[int(item["request_id"])]
        offering = offerings[int(item["course_offering_id"])]
        if item.get("online_supervision_session_id") is not None:
            session = online_sessions[int(item["online_supervision_session_id"])]
            if request.student_id != int(item["student_id"]) or request.course_id != offering.course_id:
                raise StudentAssignmentConflictError({"detail": "Reviewed online enrollment provenance no longer matches its request."})
            if offering.course.delivery_kind != "online" or session.timeslot_id is None:
                raise StudentAssignmentConflictError({"detail": "The reviewed online course or supervision session is no longer usable."})
            previous = None
            previous_enrollment_id = item.get("previous_enrollment_id")
            if previous_enrollment_id is not None:
                previous = OnlineEnrollment.objects.filter(
                    pk=int(previous_enrollment_id),
                    lifecycle_status="active",
                ).first()
                if previous is None or (
                    previous.student_id != request.student_id
                    or previous.course_offering_id != offering.id
                    or previous.supervision_session_id != item.get("previous_online_supervision_session_id")
                ):
                    raise StudentAssignmentConflictError({
                        "code": STUDENT_ASSIGNMENT_RERUN_CONTEXT_CHANGED,
                        "detail": "The reviewed online enrollment replacement no longer matches its prior active row.",
                    })
                if previous.supervision_session_id == session.id:
                    continue
                previous.lifecycle_status = "historical"
                previous.save(update_fields=["lifecycle_status"])
            if OnlineEnrollment.objects.filter(
                student_id=request.student_id,
                course_offering_id=offering.id,
                lifecycle_status="active",
            ).exists():
                raise StudentAssignmentConflictError({
                    "code": STUDENT_ASSIGNMENT_RERUN_CONTEXT_CHANGED,
                    "detail": "The student already has an active online enrollment for the reviewed offering.",
                })
            online_enrollment = OnlineEnrollment(
                student_id=request.student_id,
                course_offering=offering,
                supervision_session=session,
            )
            online_enrollment.full_clean()
            online_enrollment.save()
            StudentAssignmentApprovalOnlineEnrollment.objects.create(
                approval=approval,
                online_enrollment=online_enrollment,
                course_request=request,
                superseded_online_enrollment=previous,
            )
            continue
        section = sections[int(item["section_id"])]
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
    for item in commitment_assignments:
        kind = item["commitment_kind"]
        student_id = int(item["student_id"])
        course_request_id = item.get("course_request_id")
        source_schedule_request = (
            schedule_commitment_requests[int(item["request_id"])]
            if course_request_id is None else None
        )
        source_course_request = (
            requests[int(course_request_id)] if course_request_id is not None else None
        )
        offering = (
            offerings[int(item["course_offering_id"])]
            if item.get("course_offering_id") is not None else None
        )
        existing = next((
            commitment for commitment in existing_commitments
            if commitment.student_id == student_id
            and commitment.lifecycle_status == "active"
            and (
                commitment.schedule_commitment_request_id == getattr(source_schedule_request, "id", None)
                or commitment.course_request_id == getattr(source_course_request, "id", None)
            )
        ), None)
        normalized_occupancy = tuple(sorted(
            (int(timeslot_id), segment)
            for timeslot_id, segment in item["occupancy"]
        ))
        if existing is not None:
            existing_occupancy = tuple(sorted(
                (row.timeslot_id, row.half_semester_segment)
                for row in existing.occupancies.all()
            ))
            if existing_occupancy == normalized_occupancy:
                # A scoped rerun can retain an in-scope commitment without
                # duplicating a valid existing operational record.
                continue
            existing.lifecycle_status = "historical"
            existing.save(update_fields=["lifecycle_status"])
        commitment = StudentScheduleCommitment(
            student_id=student_id,
            academic_year_id=run.academic_year_id,
            commitment_kind=kind,
            schedule_commitment_request=source_schedule_request,
            course_request=source_course_request,
            course_offering=offering,
            credit_value=2 if kind == "co_op" else 0,
        )
        commitment.full_clean()
        commitment.save()
        for timeslot_id, segment in normalized_occupancy:
            occupancy = StudentScheduleCommitmentOccupancy(
                commitment=commitment,
                timeslot_id=timeslot_id,
                half_semester_segment=segment,
            )
            occupancy.full_clean()
            occupancy.save()
        StudentAssignmentApprovalCommitment.objects.create(
            approval=approval,
            commitment=commitment,
            schedule_commitment_request=source_schedule_request,
            course_request=source_course_request,
            superseded_commitment=existing,
        )
    return approval
