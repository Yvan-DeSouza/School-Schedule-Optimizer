"""Persistence orchestration for immutable planning runs.

This module deliberately never imports scheduling_engine; engine_adapter is the
single Django-to-engine boundary.
"""

from django.db import transaction

from backend.apps.common.constants import (
    COURSE_ALLOWED_SEMESTER_1_ONLY,
    COURSE_ALLOWED_SEMESTER_2_ONLY,
    SECTION_PLANNING_RUN_STATUS_COMPLETE,
    SECTION_PLANNING_RUN_STATUS_INFEASIBLE,
    SEMESTER_FALL,
    SEMESTER_WINTER,
)
from backend.apps.courses.models import Course, Section
from backend.apps.scheduling.models import (
    CapacityProfile,
    SectionPlanningApproval,
    SectionPlanningApprovalCourse,
    SectionPlanningRun,
)
from backend.apps.scheduling.services.engine_adapter import (
    get_section_count_plan_with_snapshot,
)


class PlanningApprovalValidationError(Exception):
    def __init__(self, detail):
        self.detail = detail
        super().__init__(str(detail))


class PlanningApprovalConflictError(Exception):
    def __init__(self, detail):
        self.detail = detail
        super().__init__(str(detail))


def create_section_planning_run(*, academic_year_id, created_by, course_constraints, teacher_capacity_adjustments):
    scenario = {
        "course_constraints": list(course_constraints),
        "teacher_capacity_adjustments": list(teacher_capacity_adjustments),
    }
    result, snapshot = get_section_count_plan_with_snapshot(
        academic_year_id,
        course_constraints=course_constraints,
        teacher_capacity_adjustments=teacher_capacity_adjustments,
    )
    status = (
        SECTION_PLANNING_RUN_STATUS_COMPLETE
        if result["status"] == "complete"
        else SECTION_PLANNING_RUN_STATUS_INFEASIBLE
    )
    return SectionPlanningRun.objects.create(
        academic_year_id=academic_year_id,
        created_by=created_by,
        status=status,
        scenario_constraints=scenario,
        input_snapshot=snapshot,
        result=result,
        solver_metadata={"engine": "ortools-cp-sat", "objective": "lexicographic"},
    )


def _course_results_by_id(run):
    if run.status != SECTION_PLANNING_RUN_STATUS_COMPLETE or run.result.get("status") != "complete":
        raise PlanningApprovalValidationError({
            "detail": "Only a completed, feasible section-planning run can be approved."
        })
    return {
        int(item["course_id"]): item
        for item in run.result.get("courses", [])
    }


def _normalize_selections(run, selections):
    result_by_course = _course_results_by_id(run)
    approved_course_ids = set(
        SectionPlanningApprovalCourse.objects.filter(
            approval__planning_run=run,
        ).values_list("course_id", flat=True)
    )
    if selections is None:
        normalized = [
            {
                "course_id": course_id,
                "semester_1_count": result["semester_1_count"],
                "semester_2_count": result["semester_2_count"],
            }
            for course_id, result in result_by_course.items()
            if course_id not in approved_course_ids
        ]
    else:
        normalized = [dict(item) for item in selections]

    unknown_course_ids = sorted({
        item["course_id"]
        for item in normalized
        if item["course_id"] not in result_by_course
    })
    if unknown_course_ids:
        raise PlanningApprovalValidationError({
            "courses": (
                "Every selected course must be present in the planning run result. "
                f"Unknown course ids: {unknown_course_ids}."
            )
        })
    return result_by_course, approved_course_ids, normalized


def _append_once(values, value):
    if value not in values:
        values.append(value)


def preview_section_planning_approval(run, *, selections=None):
    result_by_course, approved_course_ids, selections = _normalize_selections(run, selections)
    selected_course_ids = sorted({item["course_id"] for item in selections})
    courses = {
        course.id: course
        for course in Course.objects.select_related("capacity_profile").filter(
            id__in=selected_course_ids,
        )
    }
    existing_by_course = {}
    for section in Section.objects.filter(
        academic_year_id=run.academic_year_id,
        course_id__in=selected_course_ids,
    ).order_by("course_id", "section_number"):
        existing_by_course.setdefault(section.course_id, []).append({
            "section_id": section.id,
            "section_number": section.section_number,
            "semester": section.semester,
        })

    conflicts = []
    validation_errors = []
    course_reviews = []
    for selection in selections:
        course_id = selection["course_id"]
        result = result_by_course[course_id]
        course = courses.get(course_id)
        proposed_semester_1 = selection["semester_1_count"]
        proposed_semester_2 = selection["semester_2_count"]
        warnings = list(result.get("warnings", []))
        recommended_semester_1 = result["semester_1_count"]
        recommended_semester_2 = result["semester_2_count"]
        if (
            proposed_semester_1 != recommended_semester_1
            or proposed_semester_2 != recommended_semester_2
        ):
            _append_once(warnings, "counselor_adjusted_section_counts")

        item_validation_errors = []
        item_conflicts = []
        if course is None:
            error = {
                "code": "course_no_longer_exists",
                "course_id": course_id,
                "message": "The course no longer exists and cannot be approved.",
            }
            validation_errors.append(error)
            item_validation_errors.append(error["code"])
            current_capacity_policy = None
            current_allowed_semester = None
        else:
            current_capacity_policy = {
                "hard_min": course.capacity_profile.hard_min,
                "soft_min": course.capacity_profile.soft_min,
                "target": course.capacity_profile.target,
                "soft_max": course.capacity_profile.soft_max,
                "hard_max": course.capacity_profile.hard_max,
            }
            current_allowed_semester = course.allowed_semester
            if (
                current_capacity_policy != result["capacity_policy"]
                or current_allowed_semester != result["allowed_semester"]
            ):
                _append_once(warnings, "planning_configuration_changed_since_run")
            if current_allowed_semester == COURSE_ALLOWED_SEMESTER_1_ONLY and proposed_semester_2:
                error = {
                    "code": "course_not_allowed_in_semester_2",
                    "course_id": course_id,
                    "message": f"{course.course_code} is currently restricted to Semester 1.",
                }
                validation_errors.append(error)
                item_validation_errors.append(error["code"])
            if current_allowed_semester == COURSE_ALLOWED_SEMESTER_2_ONLY and proposed_semester_1:
                error = {
                    "code": "course_not_allowed_in_semester_1",
                    "course_id": course_id,
                    "message": f"{course.course_code} is currently restricted to Semester 2.",
                }
                validation_errors.append(error)
                item_validation_errors.append(error["code"])

        if course_id in approved_course_ids:
            conflict = {
                "code": "course_already_approved_from_run",
                "course_id": course_id,
                "message": "This course has already been approved from this planning run.",
            }
            conflicts.append(conflict)
            item_conflicts.append(conflict["code"])
        existing_sections = existing_by_course.get(course_id, [])
        if existing_sections:
            course_code = result["course_code"]
            conflict = {
                "code": "existing_sections_for_course_year",
                "course_id": course_id,
                "message": (
                    f"{course_code} already has {len(existing_sections)} section(s) in this "
                    "academic year; approval will not replace them."
                ),
                "existing_sections": existing_sections,
            }
            conflicts.append(conflict)
            item_conflicts.append(conflict["code"])

        proposed_total = proposed_semester_1 + proposed_semester_2
        course_reviews.append({
            "course_id": course_id,
            "course_code": result["course_code"],
            "priority_tier": result["priority_tier"],
            "predicted_enrollment": result["predicted_enrollment"],
            "unmet_demand": result["unmet_demand"],
            "recommended_semester_1_count": recommended_semester_1,
            "recommended_semester_2_count": recommended_semester_2,
            "recommended_annual_count": recommended_semester_1 + recommended_semester_2,
            "proposed_semester_1_count": proposed_semester_1,
            "proposed_semester_2_count": proposed_semester_2,
            "proposed_annual_count": proposed_total,
            "expected_enrollment_per_section": (
                result["predicted_enrollment"] / proposed_total
                if proposed_total else 0
            ),
            "run_capacity_policy": result["capacity_policy"],
            "current_capacity_policy": current_capacity_policy,
            "run_allowed_semester": result["allowed_semester"],
            "current_allowed_semester": current_allowed_semester,
            "warnings": warnings,
            "reasons": result.get("reasons", []),
            "conflicts": item_conflicts,
            "validation_errors": item_validation_errors,
            "can_approve": not item_conflicts and not item_validation_errors,
        })

    if not selections:
        conflicts.append({
            "code": "no_unapproved_courses_remaining",
            "message": "No unapproved courses remain in this planning run.",
        })

    return {
        "planning_run_id": run.id,
        "academic_year": run.academic_year_id,
        "courses": course_reviews,
        "selected_course_count": len(course_reviews),
        "proposed_section_count": sum(item["proposed_annual_count"] for item in course_reviews),
        "approved_course_ids": sorted(approved_course_ids),
        "diagnostics": run.result.get("diagnostics", []),
        "conflicts": conflicts,
        "validation_errors": validation_errors,
        "can_approve": bool(course_reviews) and not conflicts and not validation_errors,
    }


@transaction.atomic
def approve_section_planning_run(run, *, approved_by, selections=None, reason=""):
    run = SectionPlanningRun.objects.select_for_update().get(pk=run.pk)
    _, _, normalized = _normalize_selections(run, selections)
    selected_course_ids = sorted({item["course_id"] for item in normalized})
    locked_courses = list(
        Course.objects.select_for_update()
        .filter(id__in=selected_course_ids)
        .order_by("id")
    )
    list(
        CapacityProfile.objects.select_for_update()
        .filter(id__in={course.capacity_profile_id for course in locked_courses})
        .order_by("id")
    )
    preview = preview_section_planning_approval(run, selections=normalized)
    if preview["validation_errors"]:
        raise PlanningApprovalValidationError({
            "detail": "The proposed section counts are not valid.",
            "validation_errors": preview["validation_errors"],
        })
    if preview["conflicts"]:
        raise PlanningApprovalConflictError({
            "detail": "The proposed approval conflicts with existing planning decisions.",
            "conflicts": preview["conflicts"],
        })

    courses = {
        course.id: course
        for course in Course.objects.select_related("capacity_profile").filter(
            id__in=selected_course_ids,
        )
    }
    approval = SectionPlanningApproval.objects.create(
        planning_run=run,
        approved_by=approved_by,
        reason=reason,
    )
    for item in preview["courses"]:
        course = courses[item["course_id"]]
        approved_course = SectionPlanningApprovalCourse.objects.create(
            approval=approval,
            course=course,
            recommended_semester_1_count=item["recommended_semester_1_count"],
            recommended_semester_2_count=item["recommended_semester_2_count"],
            approved_semester_1_count=item["proposed_semester_1_count"],
            approved_semester_2_count=item["proposed_semester_2_count"],
        )
        for semester, count in (
            (SEMESTER_FALL, item["proposed_semester_1_count"]),
            (SEMESTER_WINTER, item["proposed_semester_2_count"]),
        ):
            for sequence in range(1, count + 1):
                Section.objects.create(
                    course=course,
                    section_number=f"S{semester}-{sequence:02d}",
                    academic_year_id=run.academic_year_id,
                    semester=semester,
                    teacher=None,
                    capacity_min=course.capacity_profile.hard_min,
                    capacity_max=course.capacity_profile.hard_max,
                    is_locked=False,
                    planning_approval_course=approved_course,
                )
    return approval
