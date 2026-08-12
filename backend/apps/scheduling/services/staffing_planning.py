"""Persist, review, and approve staffing-aware physical section plans."""

from __future__ import annotations

from dataclasses import asdict

from django.db import transaction

from backend.apps.common.constants import (
    COURSE_OFFERING_STATUS_CANCELLED,
    COURSE_OFFERING_STATUS_OFFERED,
    COURSE_REQUEST_TYPE_ALTERNATE,
    DELIVERY_GROUP_STATUS_ACTIVE,
    SECTION_PLANNING_RUN_STATUS_COMPLETE,
    SECTION_PLANNING_RUN_STATUS_INFEASIBLE,
    SEMESTER_FALL,
    SEMESTER_WINTER,
)
from backend.apps.courses.models import (
    CourseOffering,
    CourseRequest,
    DeliveryGroup,
    HalfSemesterCoursePair,
    HalfSemesterSectionPair,
    Section,
)
from backend.apps.courses.selectors import active_delivery_groups_for_year
from backend.apps.courses.services.offerings import (
    combined_allowed_semester,
    ensure_academic_year_offerings,
)
from backend.apps.scheduling.codes import (
    ACTIVE_DELIVERY_GROUPS_CHANGED_SINCE_RUN,
    ADJUSTED_COUNTS_NOT_STAFFING_FEASIBLE,
    EXISTING_SECTIONS_FOR_DELIVERY_GROUP,
    LINKED_BUDGET_TOTAL_MUST_BE_PRESERVED,
    STAFFING_CONFIGURATION_CHANGED_SINCE_RUN,
    STAFFING_RUN_ALREADY_APPROVED,
)
from backend.apps.scheduling.models import (
    StaffingPlanApproval,
    StaffingPlanApprovalOffering,
    StaffingPlanRun,
    StaffingRequestResolution,
    TeacherPlanningRoster,
)
from backend.apps.scheduling.services.engine_adapter import load_scheduling_input
from backend.apps.scheduling.services.section_planning import (
    PlanningApprovalConflictError,
    PlanningApprovalValidationError,
)
from backend.apps.scheduling.services.run_contracts import (
    ensure_unique_selection,
    require_text_reason,
)
from scheduling_engine.section_budget_planner import (
    plan_section_budget,
    resolve_backup_requests,
)
from scheduling_engine.staffing_planner import plan_staffing_counts


def _approved_budget_counts(approval):
    if approval is None:
        return None
    return {
        item.delivery_group_id: item.approved_annual_count
        for item in approval.offering_approvals.all()
    }


def _available_backup_courses(data, *, budget_counts=None):
    """Find offerings that already exist without relying on backup demand."""

    if budget_counts is None:
        total_capacity = sum(
            max(0, item.maximum_sections - item.reserved_sections)
            for item in data.teacher_planning_capacities
        )
        preliminary = plan_section_budget(
            data,
            section_budget=total_capacity,
            budget_type="ceiling",
        )
        if preliminary["status"] != "complete":
            return set(), preliminary
        counts = {
            item["offering_id"]: item["annual_count"]
            for item in preliminary["offerings"]
        }
    else:
        counts = budget_counts
        preliminary = None
    available = {
        course_id
        for offering in data.planning_offerings
        if counts.get(offering.id, 0) > 0
        for course_id in offering.member_course_ids
    }
    return available, preliminary


def _effective_staffing_demand(data, *, cancelled_course_ids, backup_policy, backup_overrides, budget_counts):
    available, failed_preliminary = _available_backup_courses(
        data,
        budget_counts=budget_counts,
    )
    if failed_preliminary and failed_preliminary["status"] != "complete":
        return (), [], failed_preliminary
    effective, resolutions = resolve_backup_requests(
        data,
        cancelled_course_ids=cancelled_course_ids,
        available_backup_course_ids=available,
        default_policy=backup_policy,
        course_overrides=backup_overrides,
    )
    return effective, resolutions, None


@transaction.atomic
def create_staffing_plan_run(
    *,
    academic_year,
    created_by,
    budget_approval,
    backup_policy,
    backup_overrides,
    offering_constraints,
    teacher_capacity_adjustments,
):
    """Run against a confirmed roster and persist one immutable explanation."""

    ensure_academic_year_offerings(academic_year, actor=created_by)
    if budget_approval and budget_approval.budget_run.academic_year_id != academic_year.id:
        raise ValueError("The linked budget approval belongs to a different academic year.")
    data = load_scheduling_input(academic_year.id, require_ready_roster=True)
    teacher_roster = TeacherPlanningRoster.objects.get(academic_year=academic_year)
    budget_counts = _approved_budget_counts(budget_approval)
    cancelled_course_ids = list(
        CourseOffering.objects.filter(
            academic_year=academic_year,
            status=COURSE_OFFERING_STATUS_CANCELLED,
        ).values_list("course_id", flat=True)
    )
    override_course_ids = {item["course_id"] for item in backup_overrides}
    if not override_course_ids <= set(cancelled_course_ids):
        raise ValueError("Backup-policy overrides may reference only cancelled courses.")
    effective, resolutions, preliminary_failure = _effective_staffing_demand(
        data,
        cancelled_course_ids=cancelled_course_ids,
        backup_policy=backup_policy,
        backup_overrides=backup_overrides,
        budget_counts=budget_counts,
    )
    if preliminary_failure:
        result = preliminary_failure
        result["planning_phase"] = "backup_availability_preliminary"
    else:
        result = plan_staffing_counts(
            data,
            effective_requests=effective,
            offering_constraints=offering_constraints,
            teacher_capacity_adjustments=teacher_capacity_adjustments,
            approved_budget_counts=budget_counts,
        )
        result["backup_policy"] = backup_policy
        result["backup_overrides"] = list(backup_overrides)
        result["request_resolutions"] = resolutions
        result["affected_student_count"] = len(resolutions)
    run = StaffingPlanRun.objects.create(
        academic_year=academic_year,
        budget_approval=budget_approval,
        teacher_roster=teacher_roster,
        created_by=created_by,
        status=(
            SECTION_PLANNING_RUN_STATUS_COMPLETE
            if result["status"] == "complete"
            else SECTION_PLANNING_RUN_STATUS_INFEASIBLE
        ),
        backup_policy=backup_policy,
        backup_overrides=list(backup_overrides),
        scenario_constraints={
            "offering_constraints": list(offering_constraints),
            "teacher_capacity_adjustments": list(teacher_capacity_adjustments),
        },
        input_snapshot=asdict(data),
        result=result,
        solver_metadata={
            "engine": "ortools-cp-sat",
            "decision_unit": "physical_delivery_group",
            "named_teacher_assignments": False,
        },
    )
    alternate_requests = {
        (item.student_id, item.course_id): item
        for item in CourseRequest.objects.filter(
            academic_year=academic_year,
            request_type=COURSE_REQUEST_TYPE_ALTERNATE,
        )
    }
    for item in resolutions:
        StaffingRequestResolution.objects.create(
            staffing_run=run,
            student_id=item["student_id"],
            cancelled_course_ids=item["cancelled_course_ids"],
            backup_request=alternate_requests.get(
                (item["student_id"], item["backup_course_id"])
            ),
            outcome=item["outcome"],
            unresolved_course_count=item["unresolved_course_count"],
        )
    return run


def _normalize_selections(run, selections):
    if run.status != SECTION_PLANNING_RUN_STATUS_COMPLETE or run.result.get("status") != "complete":
        raise PlanningApprovalValidationError({
            "detail": "Only a completed, feasible staffing plan can be approved."
        })
    recommendations = {
        int(item["offering_id"]): item for item in run.result.get("offerings", [])
    }
    if selections is None:
        values = [{
            "offering_id": offering_id,
            "semester_1_count": item["semester_1_count"],
            "semester_2_count": item["semester_2_count"],
        } for offering_id, item in recommendations.items()]
    else:
        values = [dict(item) for item in selections]
    ensure_unique_selection(
        values,
        "offering_id",
        field="offerings",
        message="Each delivery group may be selected only once.",
        error_class=PlanningApprovalValidationError,
    )
    selected_ids = [item["offering_id"] for item in values]
    if set(selected_ids) != set(recommendations):
        raise PlanningApprovalValidationError({
            "offerings": "A staffing approval must review every physical delivery group in the run."
        })
    return recommendations, values


def preview_staffing_plan_approval(run, *, selections=None):
    """Validate adjusted group counts against current catalog and roster state."""

    recommendations, selections = _normalize_selections(run, selections)
    groups = {
        group.id: group
        for group in DeliveryGroup.objects.select_related("capacity_profile").prefetch_related(
            "offerings__course"
        ).filter(id__in=recommendations)
    }
    validation_errors = []
    conflicts = []
    reviews = []
    current_active_group_ids = set(
        active_delivery_groups_for_year(run.academic_year_id).values_list(
            "id",
            flat=True,
        )
    )
    if current_active_group_ids != set(recommendations):
        validation_errors.append({
            "code": ACTIVE_DELIVERY_GROUPS_CHANGED_SINCE_RUN,
            "run_delivery_group_ids": sorted(recommendations),
            "current_delivery_group_ids": sorted(current_active_group_ids),
        })
    for selection in selections:
        offering_id = selection["offering_id"]
        result = recommendations[offering_id]
        group = groups.get(offering_id)
        semester_one = selection["semester_1_count"]
        semester_two = selection["semester_2_count"]
        annual = semester_one + semester_two
        item_errors = []
        item_conflicts = []
        if group is None or group.academic_year_id != run.academic_year_id:
            item_errors.append("delivery_group_no_longer_exists")
        else:
            courses = [offering.course for offering in group.offerings.all()]
            current_ids = {course.id for course in courses}
            if current_ids != set(result["member_course_ids"]):
                item_errors.append("delivery_group_membership_changed_since_run")
            allowed = combined_allowed_semester(courses)
            if allowed is None:
                item_errors.append("delivery_group_has_no_common_semester")
            if allowed == "semester_1_only" and semester_two:
                item_errors.append("delivery_group_not_allowed_in_semester_2")
            if allowed == "semester_2_only" and semester_one:
                item_errors.append("delivery_group_not_allowed_in_semester_1")
            if result["predicted_enrollment"] > 0 and annual < 1:
                item_errors.append("positive_demand_requires_a_section")
            if result["is_combined"] and result["predicted_enrollment"] > 0 and annual != 1:
                item_errors.append("combined_delivery_requires_exactly_one_section")
            existing = list(
                Section.objects.filter(
                    academic_year_id=run.academic_year_id,
                    delivery_group_id=offering_id,
                ).values("id", "section_number", "lifecycle_status")
            )
            if existing:
                item_conflicts.append(EXISTING_SECTIONS_FOR_DELIVERY_GROUP)
                conflicts.append({
                    "code": EXISTING_SECTIONS_FOR_DELIVERY_GROUP,
                    "offering_id": offering_id,
                    "sections": existing,
                })
        validation_errors.extend({
            "code": code,
            "offering_id": offering_id,
        } for code in item_errors)
        reviews.append({
            **result,
            "recommended_semester_1_count": result["semester_1_count"],
            "recommended_semester_2_count": result["semester_2_count"],
            "proposed_semester_1_count": semester_one,
            "proposed_semester_2_count": semester_two,
            "proposed_annual_count": annual,
            "validation_errors": item_errors,
            "conflicts": item_conflicts,
            "can_approve": not item_errors and not item_conflicts,
        })
    if hasattr(run, "approval"):
        conflicts.append({"code": STAFFING_RUN_ALREADY_APPROVED})
    linked_total = run.result.get("linked_budget_total")
    proposed_total = sum(item["proposed_annual_count"] for item in reviews)
    if linked_total is not None and proposed_total != linked_total:
        validation_errors.append({
            "code": LINKED_BUDGET_TOTAL_MUST_BE_PRESERVED,
            "required_total": linked_total,
            "proposed_total": proposed_total,
        })
    # Counselor adjustments need a fresh anonymous staffing feasibility proof.
    # Always produce a fresh anonymous staffing proof. Even an unchanged
    # recommendation must not be approved after roster readiness, verified
    # qualifications, requests, capacities, or offering state has changed.
    if not validation_errors and not conflicts:
        try:
            data = load_scheduling_input(run.academic_year_id, require_ready_roster=True)
            cancelled = list(CourseOffering.objects.filter(
                academic_year_id=run.academic_year_id,
                status=COURSE_OFFERING_STATUS_CANCELLED,
            ).values_list("course_id", flat=True))
            budget_counts = _approved_budget_counts(run.budget_approval)
            effective, _, failure = _effective_staffing_demand(
                data,
                cancelled_course_ids=cancelled,
                backup_policy=run.backup_policy,
                backup_overrides=run.backup_overrides,
                budget_counts=budget_counts,
            )
            proof = failure or plan_staffing_counts(
                data,
                effective_requests=effective,
                offering_constraints=[{
                    "offering_id": item["offering_id"],
                    "exact_sections": item["proposed_annual_count"],
                    "semester_1_count": item["proposed_semester_1_count"],
                    "semester_2_count": item["proposed_semester_2_count"],
                } for item in reviews],
                teacher_capacity_adjustments=run.scenario_constraints.get(
                    "teacher_capacity_adjustments", []
                ),
                approved_budget_counts=budget_counts,
            )
            if proof["status"] != "complete":
                validation_errors.append({
                    "code": ADJUSTED_COUNTS_NOT_STAFFING_FEASIBLE,
                    "diagnostics": proof.get("diagnostics", []),
                })
        except ValueError as error:
            validation_errors.append({
                "code": STAFFING_CONFIGURATION_CHANGED_SINCE_RUN,
                "message": str(error),
            })
    return {
        "staffing_run_id": run.id,
        "academic_year": run.academic_year_id,
        "offerings": reviews,
        "proposed_physical_section_total": proposed_total,
        "linked_budget_total": linked_total,
        "validation_errors": validation_errors,
        "conflicts": conflicts,
        "can_approve": bool(reviews) and not validation_errors and not conflicts,
    }


@transaction.atomic
def approve_staffing_plan_run(run, *, approved_by, reason, selections=None):
    """Create auditable, unstaffed physical Section rows in one transaction."""

    reason = require_text_reason(
        reason,
        message="An approval reason is required.",
        error_class=PlanningApprovalValidationError,
    )
    run = StaffingPlanRun.objects.select_for_update().get(pk=run.pk)
    group_ids = [item["offering_id"] for item in run.result.get("offerings", [])]
    list(DeliveryGroup.objects.select_for_update().filter(id__in=group_ids).order_by("id"))
    preview = preview_staffing_plan_approval(run, selections=selections)
    if preview["validation_errors"]:
        raise PlanningApprovalValidationError({
            "detail": "The staffing plan is not valid for approval.",
            "validation_errors": preview["validation_errors"],
        })
    if preview["conflicts"]:
        raise PlanningApprovalConflictError({
            "detail": "The staffing plan would overwrite an existing decision.",
            "conflicts": preview["conflicts"],
        })
    groups = {
        group.id: group
        for group in DeliveryGroup.objects.select_related("capacity_profile").prefetch_related(
            "offerings__course"
        ).filter(id__in=group_ids)
    }
    approval = StaffingPlanApproval.objects.create(
        staffing_run=run,
        approved_by=approved_by,
        reason=reason,
    )
    # The staffing workflow is an authoritative Section materializer, not only
    # the older section-planning workflow.  Preserve the catalog's narrow
    # first/second-half identity here so later placement, named staffing, and
    # student assignment do not mistake sequential trimestre sections for two
    # simultaneous full-semester classes.
    half_pairs = list(HalfSemesterCoursePair.objects.filter(is_active=True).order_by("id"))
    half_course_segment = {
        pair.first_course_id: "first_half"
        for pair in half_pairs
    } | {
        pair.second_course_id: "second_half"
        for pair in half_pairs
    }
    created_sections_by_course_semester = {}
    for item in preview["offerings"]:
        group = groups[item["offering_id"]]
        line = StaffingPlanApprovalOffering.objects.create(
            approval=approval,
            delivery_group=group,
            recommended_semester_1_count=item["recommended_semester_1_count"],
            recommended_semester_2_count=item["recommended_semester_2_count"],
            approved_semester_1_count=item["proposed_semester_1_count"],
            approved_semester_2_count=item["proposed_semester_2_count"],
        )
        offerings = list(group.offerings.select_related("course").all())
        compatibility_course = offerings[0].course if len(offerings) == 1 else None
        for semester, count in (
            (SEMESTER_FALL, item["proposed_semester_1_count"]),
            (SEMESTER_WINTER, item["proposed_semester_2_count"]),
        ):
            for sequence in range(1, count + 1):
                section = Section.objects.create(
                    course=compatibility_course,
                    delivery_group=group,
                    section_number=f"S{semester}-{sequence:02d}",
                    academic_year=run.academic_year,
                    semester=semester,
                    half_semester_segment=(
                        half_course_segment.get(compatibility_course.id)
                        if compatibility_course is not None
                        else None
                    ),
                    teacher=None,
                    capacity_min=group.capacity_profile.hard_min,
                    capacity_max=group.capacity_profile.hard_max,
                    is_locked=False,
                    staffing_approval_offering=line,
                )
                if compatibility_course is not None:
                    created_sections_by_course_semester.setdefault(
                        (compatibility_course.id, semester), []
                    ).append(section)
    # Pair matching sections only after the approval has materialized every
    # selected course.  A mismatch remains an explicit counselor-review case;
    # the workflow must not invent a companion section or silently reassign a
    # section across semesters just to make a pair exist.
    for pair in half_pairs:
        for semester in (SEMESTER_FALL, SEMESTER_WINTER):
            first_sections = created_sections_by_course_semester.get(
                (pair.first_course_id, semester), ()
            )
            second_sections = created_sections_by_course_semester.get(
                (pair.second_course_id, semester), ()
            )
            for first, second in zip(first_sections, second_sections):
                HalfSemesterSectionPair.objects.create(
                    course_pair=pair,
                    first_section=first,
                    second_section=second,
                )
    return approval
