"""Persist, review, and approve teacher-independent section-budget runs."""

from __future__ import annotations

from dataclasses import asdict

from django.db import transaction

from backend.apps.common.constants import (
    COURSE_OFFERING_STATUS_CANCELLED,
    COURSE_OFFERING_STATUS_OFFERED,
    COURSE_REQUEST_TYPE_ALTERNATE,
    COURSE_REQUEST_TYPE_PRIMARY,
    DELIVERY_GROUP_STATUS_ACTIVE,
    SECTION_BUDGET_EXACT,
    SECTION_PLANNING_RUN_STATUS_COMPLETE,
    SECTION_PLANNING_RUN_STATUS_INFEASIBLE,
)
from backend.apps.courses.models import CourseOffering, CourseRequest, DeliveryGroup
from backend.apps.courses.selectors import active_delivery_groups_for_year
from backend.apps.courses.services.offerings import (
    combined_allowed_semester,
    ensure_academic_year_offerings,
    get_combination_suggestions,
)
from backend.apps.scheduling.models import (
    PlanningRequestResolution,
    SectionBudgetApproval,
    SectionBudgetApprovalOffering,
    SectionBudgetRun,
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
    plan_section_budget_with_backups,
    resolve_backup_requests,
)


def create_section_budget_run(
    *,
    academic_year,
    created_by,
    budget_type,
    section_budget,
    backup_policy,
    backup_overrides,
    offering_constraints,
):
    """Create missing standalone offerings, run the pure planner, and freeze it."""

    ensure_academic_year_offerings(academic_year, actor=created_by)
    data = load_scheduling_input(academic_year.id)
    cancelled_course_ids = list(
        CourseOffering.objects.filter(
            academic_year=academic_year,
            status=COURSE_OFFERING_STATUS_CANCELLED,
        ).values_list("course_id", flat=True)
    )
    override_course_ids = {item["course_id"] for item in backup_overrides}
    if not override_course_ids <= set(cancelled_course_ids):
        raise ValueError("Backup-policy overrides may reference only cancelled courses.")
    result = plan_section_budget_with_backups(
        data,
        section_budget=section_budget,
        budget_type=budget_type,
        cancelled_course_ids=cancelled_course_ids,
        backup_policy=backup_policy,
        backup_overrides=backup_overrides,
        offering_constraints=offering_constraints,
    )
    # Suggestions are advisory and tied to approved compatibility rules. They
    # never mutate the just-solved delivery groups or act as an escape hatch.
    result["combination_suggestions"] = get_combination_suggestions(academic_year)
    status = (
        SECTION_PLANNING_RUN_STATUS_COMPLETE
        if result["status"] == "complete"
        else SECTION_PLANNING_RUN_STATUS_INFEASIBLE
    )
    return SectionBudgetRun.objects.create(
        academic_year=academic_year,
        created_by=created_by,
        status=status,
        budget_type=budget_type,
        section_budget=section_budget,
        backup_policy=backup_policy,
        backup_overrides=list(backup_overrides),
        scenario_constraints={"offering_constraints": list(offering_constraints)},
        input_snapshot=asdict(data),
        result=result,
        solver_metadata={
            "engine": "ortools-cp-sat",
            "planning_mode": "teacher_independent_budget",
        },
    )


def _normalize_approval(run, selections):
    if run.status != SECTION_PLANNING_RUN_STATUS_COMPLETE:
        raise PlanningApprovalValidationError({
            "detail": "Only a completed section-budget run can be approved."
        })
    recommendations = {
        int(item["offering_id"]): item for item in run.result.get("offerings", [])
    }
    if selections is None:
        normalized = [
            {
                "offering_id": item["offering_id"],
                "semester_1_count": item["semester_1_count"],
                "semester_2_count": item["semester_2_count"],
            }
            for item in run.result.get("offerings", [])
        ]
    else:
        normalized = [dict(item) for item in selections]
        ensure_unique_selection(
            normalized,
            "offering_id",
            field="offerings",
            message="Each delivery group may be selected only once.",
            error_class=PlanningApprovalValidationError,
        )
    unknown = sorted({
        item["offering_id"] for item in normalized
        if item["offering_id"] not in recommendations
    })
    if unknown:
        raise PlanningApprovalValidationError({
            "offerings": f"Unknown delivery-group ids for this run: {unknown}."
        })
    if set(item["offering_id"] for item in normalized) != set(recommendations):
        raise PlanningApprovalValidationError({
            "offerings": "Budget approval must include every offering from the run."
        })
    return recommendations, normalized


def preview_section_budget_approval(run, *, selections=None):
    """Validate adjusted counts against current offering state without writing."""

    recommendations, normalized = _normalize_approval(run, selections)
    groups = {
        group.id: group
        for group in DeliveryGroup.objects.prefetch_related("offerings__course").filter(
            id__in=recommendations
        )
    }
    current_primary_course_ids = set(
        CourseRequest.objects.filter(
            academic_year=run.academic_year,
            request_type=COURSE_REQUEST_TYPE_PRIMARY,
        ).values_list("course_id", flat=True)
    )
    errors = []
    courses = []
    approved_total = 0
    current_active_group_ids = set(
        active_delivery_groups_for_year(run.academic_year_id).values_list(
            "id",
            flat=True,
        )
    )
    if current_active_group_ids != set(recommendations):
        errors.append({
            "code": "active_delivery_groups_changed_since_run",
            "run_delivery_group_ids": sorted(recommendations),
            "current_delivery_group_ids": sorted(current_active_group_ids),
        })
    for selection in normalized:
        recommendation = recommendations[selection["offering_id"]]
        group = groups.get(selection["offering_id"])
        one = selection["semester_1_count"]
        two = selection["semester_2_count"]
        annual = one + two
        item_errors = []
        if group is None or group.status != DELIVERY_GROUP_STATUS_ACTIVE:
            item_errors.append("delivery_group_no_longer_active")
        else:
            current_offerings = list(group.offerings.select_related("course").all())
            current_members = sorted(item.course_id for item in current_offerings)
            if current_members != sorted(recommendation["member_course_ids"]):
                item_errors.append("delivery_group_membership_changed")
            current_positive_demand = any(
                course_id in current_primary_course_ids for course_id in current_members
            )
            if (
                (recommendation["predicted_enrollment"] > 0 or current_positive_demand)
                and annual == 0
            ):
                item_errors.append("positive_demand_requires_manual_cancellation")
            if (
                recommendation["is_combined"]
                and (recommendation["predicted_enrollment"] > 0 or current_positive_demand)
                and annual != 1
            ):
                item_errors.append("combined_offering_requires_one_shared_section")
            allowed_semester = combined_allowed_semester([
                item.course for item in current_offerings
            ])
            if allowed_semester is None:
                item_errors.append("delivery_group_has_no_common_semester")
            if allowed_semester == "semester_1_only" and two:
                item_errors.append("delivery_group_not_allowed_in_semester_2")
            if allowed_semester == "semester_2_only" and one:
                item_errors.append("delivery_group_not_allowed_in_semester_1")
        for code in item_errors:
            errors.append({
                "code": code,
                "offering_id": selection["offering_id"],
            })
        approved_total += annual
        courses.append({
            **recommendation,
            "approved_semester_1_count": one,
            "approved_semester_2_count": two,
            "approved_annual_count": annual,
            "validation_errors": item_errors,
        })
    if run.budget_type == SECTION_BUDGET_EXACT and approved_total != run.section_budget:
        errors.append({
            "code": "exact_budget_total_mismatch",
            "required_total": run.section_budget,
            "approved_total": approved_total,
        })
    if approved_total > run.section_budget:
        errors.append({
            "code": "section_budget_exceeded",
            "section_budget": run.section_budget,
            "approved_total": approved_total,
        })
    if not errors:
        # Re-prove the complete adjusted budget against current requests and
        # offering state. This prevents a once-valid zero from silently
        # cancelling newly positive demand and prevents meaningless over-supply.
        data = load_scheduling_input(run.academic_year_id)
        preliminary = plan_section_budget(
            data,
            section_budget=run.section_budget,
            budget_type="ceiling",
        )
        proof = preliminary
        if preliminary["status"] == "complete":
            available_course_ids = {
                course_id
                for item in preliminary["offerings"] if item["annual_count"] > 0
                for course_id in item["member_course_ids"]
            }
            cancelled_course_ids = list(CourseOffering.objects.filter(
                academic_year=run.academic_year,
                status=COURSE_OFFERING_STATUS_CANCELLED,
            ).values_list("course_id", flat=True))
            effective, _resolutions = resolve_backup_requests(
                data,
                cancelled_course_ids=cancelled_course_ids,
                available_backup_course_ids=available_course_ids,
                default_policy=run.backup_policy,
                course_overrides=run.backup_overrides,
            )
            proof = plan_section_budget(
                data,
                section_budget=run.section_budget,
                budget_type=run.budget_type,
                effective_requests=effective,
                offering_constraints=[{
                    "offering_id": item["offering_id"],
                    "exact_sections": item["approved_annual_count"],
                    "semester_1_count": item["approved_semester_1_count"],
                    "semester_2_count": item["approved_semester_2_count"],
                } for item in courses],
            )
        if proof["status"] != "complete":
            errors.append({
                "code": "adjusted_budget_no_longer_feasible",
                "diagnostics": proof.get("diagnostics", []),
            })
    return {
        "budget_run_id": run.id,
        "budget_type": run.budget_type,
        "section_budget": run.section_budget,
        "approved_total": approved_total,
        "offerings": courses,
        "request_resolutions": run.result.get("request_resolutions", []),
        "affected_student_count": run.result.get("affected_student_count", 0),
        "validation_errors": errors,
        "can_approve": not errors and not hasattr(run, "approval"),
    }


@transaction.atomic
def approve_section_budget_run(run, *, approved_by, reason, selections=None):
    """Store accepted budget counts/resolutions without creating Section rows."""

    reason = require_text_reason(
        reason,
        message="An approval reason is required.",
        error_class=PlanningApprovalValidationError,
    )
    run = SectionBudgetRun.objects.select_for_update().get(pk=run.pk)
    if SectionBudgetApproval.objects.filter(budget_run=run).exists():
        raise PlanningApprovalConflictError({
            "detail": "This section-budget run has already been approved.",
            "conflicts": [{"code": "budget_run_already_approved"}],
        })
    preview = preview_section_budget_approval(run, selections=selections)
    if preview["validation_errors"]:
        raise PlanningApprovalValidationError({
            "detail": "The adjusted section budget is not valid.",
            "validation_errors": preview["validation_errors"],
        })
    approval = SectionBudgetApproval.objects.create(
        budget_run=run,
        approved_by=approved_by,
        reason=reason,
    )
    for item in preview["offerings"]:
        SectionBudgetApprovalOffering.objects.create(
            approval=approval,
            delivery_group_id=item["offering_id"],
            recommended_annual_count=item["annual_count"],
            recommended_semester_1_count=item["semester_1_count"],
            recommended_semester_2_count=item["semester_2_count"],
            approved_annual_count=item["approved_annual_count"],
            approved_semester_1_count=item["approved_semester_1_count"],
            approved_semester_2_count=item["approved_semester_2_count"],
        )
    alternate_requests = {
        (item.student_id, item.course_id): item
        for item in CourseRequest.objects.filter(
            academic_year=run.academic_year,
            request_type=COURSE_REQUEST_TYPE_ALTERNATE,
        )
    }
    for item in run.result.get("request_resolutions", []):
        backup = alternate_requests.get((item["student_id"], item["backup_course_id"]))
        PlanningRequestResolution.objects.create(
            approval=approval,
            student_id=item["student_id"],
            cancelled_course_ids=item["cancelled_course_ids"],
            backup_request=backup,
            outcome=item["outcome"],
            unresolved_course_count=item["unresolved_course_count"],
        )
    return approval
