"""Reviewed capacity planning for shared online-supervision sessions."""

from __future__ import annotations

from dataclasses import asdict
from hashlib import sha256
import json
from math import ceil

from django.db import transaction

from backend.apps.common.constants import (
    COURSE_ALLOWED_SEMESTER_1_ONLY,
    COURSE_ALLOWED_SEMESTER_2_ONLY,
    COURSE_OFFERING_STATUS_OFFERED,
    SECTION_PLANNING_RUN_STATUS_COMPLETE,
)
from backend.apps.common.exceptions import DomainConflictError, DomainValidationError
from backend.apps.courses.constants import COURSE_DELIVERY_KIND_ONLINE
from backend.apps.courses.models import CourseOffering
from backend.apps.courses.services.offerings import ensure_academic_year_offerings
from backend.apps.scheduling.codes import ONLINE_SUPERVISION_CONFIGURATION_INVALID, ONLINE_SUPERVISION_PLAN_CONTEXT_CHANGED
from backend.apps.scheduling.models import (
    OnlineSupervisionConfiguration,
    OnlineSupervisionPlanApproval,
    OnlineSupervisionPlanApprovalSession,
    OnlineSupervisionPlanRun,
    OnlineSupervisionSession,
)
from backend.apps.scheduling.services.demand_forecasting import predicted_primary_demand_by_course


def _fingerprint(payload):
    """Hash canonical JSON so approval can reject changed planning context."""

    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return sha256(encoded.encode("utf-8")).hexdigest()


def _session_rows(*, academic_year):
    """Derive narrow, inspectable annual session slots from expected demand.

    Semester-restricted online demand receives dedicated slots.  Either-term
    demand receives flexible slots; placement later chooses their semester and
    A-D block together with normal sections.  This prevents an online course
    from being represented as a fake instructional section just to reserve a
    supervision seat.
    """

    try:
        configuration = OnlineSupervisionConfiguration.objects.select_related("capacity_profile").get(
            academic_year=academic_year
        )
    except OnlineSupervisionConfiguration.DoesNotExist as error:
        raise DomainValidationError({
            "code": ONLINE_SUPERVISION_CONFIGURATION_INVALID,
            "detail": "Create an online supervision capacity configuration before planning online sessions.",
        }) from error
    profile = configuration.capacity_profile
    if profile.target <= 0 or profile.hard_max <= 0 or profile.target > profile.hard_max:
        raise DomainValidationError({
            "code": ONLINE_SUPERVISION_CONFIGURATION_INVALID,
            "detail": "The online supervision capacity profile must have a valid target no greater than its hard maximum.",
        })
    ensure_academic_year_offerings(academic_year)
    demand_by_course = predicted_primary_demand_by_course(academic_year)
    offerings = list(CourseOffering.objects.filter(
        academic_year=academic_year,
        status=COURSE_OFFERING_STATUS_OFFERED,
        course__delivery_kind=COURSE_DELIVERY_KIND_ONLINE,
    ).select_related("course").order_by("course__course_code", "id"))
    demand_by_allowed_semester = {1: 0.0, 2: 0.0, "either": 0.0}
    source_courses = []
    for offering in offerings:
        demand = max(0.0, float(demand_by_course.get(offering.course_id, 0.0)))
        allowed = offering.course.allowed_semester
        bucket = (
            1 if allowed == COURSE_ALLOWED_SEMESTER_1_ONLY
            else 2 if allowed == COURSE_ALLOWED_SEMESTER_2_ONLY
            else "either"
        )
        demand_by_allowed_semester[bucket] += demand
        source_courses.append({
            "offering_id": offering.id,
            "course_id": offering.course_id,
            "course_code": offering.course.course_code,
            "allowed_semester": allowed,
            "predicted_primary_demand": demand,
            # Half-semester online learning still consumes a full-semester
            # supervision seat in the school's policy, so duration is shown
            # for review but intentionally does not fractionally reduce demand.
            "duration": offering.course.duration,
        })
    rows = []
    for allowed_semesters, demand in (
        ((1,), demand_by_allowed_semester[1]),
        ((2,), demand_by_allowed_semester[2]),
        ((1, 2), demand_by_allowed_semester["either"]),
    ):
        for _ in range(ceil(demand / profile.target)):
            rows.append({
                "annual_index": len(rows) + 1,
                "allowed_semesters": list(allowed_semesters),
                "capacity_max": profile.hard_max,
                "target_capacity": profile.target,
            })
    return configuration, source_courses, rows


def _current_snapshot(*, academic_year):
    configuration, courses, sessions = _session_rows(academic_year=academic_year)
    snapshot = {
        "academic_year_id": academic_year.id,
        "configuration": {
            "id": configuration.id,
            "capacity_profile_id": configuration.capacity_profile_id,
            "capacity_max": configuration.capacity_profile.hard_max,
            "target_capacity": configuration.capacity_profile.target,
        },
        "online_courses": courses,
        "recommended_sessions": sessions,
    }
    snapshot["fingerprint"] = _fingerprint(snapshot)
    return configuration, snapshot


def create_online_supervision_plan_run(*, academic_year, created_by):
    """Create an immutable, demand-based online-supervision capacity candidate."""

    if OnlineSupervisionSession.objects.filter(academic_year=academic_year, lifecycle_status="active").exists():
        raise DomainConflictError({
            "detail": "Active online supervision sessions already exist; retire them through a future reviewed reconciliation before planning replacements."
        })
    configuration, snapshot = _current_snapshot(academic_year=academic_year)
    return OnlineSupervisionPlanRun.objects.create(
        academic_year=academic_year,
        configuration=configuration,
        created_by=created_by,
        status=SECTION_PLANNING_RUN_STATUS_COMPLETE,
        input_snapshot=snapshot,
        result={
            "status": "complete",
            "sessions": snapshot["recommended_sessions"],
            "online_course_count": len(snapshot["online_courses"]),
        },
        solver_metadata={
            "planning_method": "capacity_profile_demand_rounding",
            "named_supervisors_assigned": False,
            "placement_completed": False,
        },
    )


@transaction.atomic
def approve_online_supervision_plan_run(run, *, approved_by, reason):
    """Materialize unplaced supervision resources from one unchanged candidate."""

    reason = reason.strip() if isinstance(reason, str) else ""
    if not reason:
        raise DomainValidationError({"reason": "An approval reason is required."})
    run = OnlineSupervisionPlanRun.objects.select_for_update().select_related("academic_year").get(pk=run.pk)
    if run.status != SECTION_PLANNING_RUN_STATUS_COMPLETE or run.result.get("status") != "complete":
        raise DomainValidationError({"detail": "Only a complete online supervision plan can be approved."})
    if hasattr(run, "approval"):
        raise DomainConflictError({"detail": "This online supervision plan has already been approved."})
    # Lock the relevant configuration and offerings before comparing a detached
    # snapshot: approval accepts the reviewed rows; it never silently replans.
    list(OnlineSupervisionConfiguration.objects.select_for_update().filter(academic_year=run.academic_year))
    list(CourseOffering.objects.select_for_update().filter(academic_year=run.academic_year).order_by("id"))
    _configuration, current = _current_snapshot(academic_year=run.academic_year)
    if current["fingerprint"] != run.input_snapshot.get("fingerprint"):
        raise DomainConflictError({
            "code": ONLINE_SUPERVISION_PLAN_CONTEXT_CHANGED,
            "detail": "Online course demand or capacity configuration changed since this plan was reviewed.",
        })
    approval = OnlineSupervisionPlanApproval.objects.create(
        plan_run=run,
        approved_by=approved_by,
        reason=reason,
    )
    for row in run.result.get("sessions", ()):
        approved_session = OnlineSupervisionPlanApprovalSession.objects.create(
            approval=approval,
            annual_index=int(row["annual_index"]),
            allowed_semesters=list(row["allowed_semesters"]),
            capacity_max=int(row["capacity_max"]),
            target_capacity=int(row["target_capacity"]),
        )
        # No time and no teacher are filled here. Those independent decisions
        # belong to their established review stages and are not implied by a
        # capacity recommendation.
        OnlineSupervisionSession.objects.create(
            academic_year=run.academic_year,
            session_number=f"ONLINE-{approved_session.annual_index:02d}",
            capacity_max=approved_session.capacity_max,
            target_capacity=approved_session.target_capacity,
            plan_approval_session=approved_session,
        )
    return approval
