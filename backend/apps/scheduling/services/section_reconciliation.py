"""Preview and atomically apply revised section plans to existing sections."""

from __future__ import annotations

from hashlib import sha256
import json
import re

from django.db import transaction

from backend.apps.common.constants import (
    COURSE_ALLOWED_SEMESTER_1_ONLY,
    COURSE_ALLOWED_SEMESTER_2_ONLY,
    SECTION_LIFECYCLE_ACTIVE,
    SECTION_LIFECYCLE_RETIRED,
    SECTION_RECONCILIATION_ACTION_CREATED,
    SECTION_RECONCILIATION_ACTION_KEPT,
    SECTION_RECONCILIATION_ACTION_MOVED,
    SECTION_RECONCILIATION_ACTION_REACTIVATED,
    SECTION_RECONCILIATION_ACTION_RETIRED,
    SEMESTER_FALL,
    SEMESTER_WINTER,
)
from backend.apps.courses.models import Course, CourseOffering, Section
from backend.apps.courses.services.offerings import ensure_academic_year_offerings
from backend.apps.courses.services.section_state import (
    fixed_context_reasons as _protection_reasons,
    section_dependency_sets as _dependency_sets,
)
from backend.apps.scheduling.models import (
    CapacityProfile,
    SectionPlanningApproval,
    SectionPlanningApprovalCourse,
    SectionPlanningReconciliation,
    SectionPlanningReconciliationAction,
    SectionPlanningReconciliationCourse,
    SectionPlanningRun,
)
from backend.apps.scheduling.services.section_planning import (
    PlanningApprovalConflictError,
    PlanningApprovalValidationError,
    _normalize_selections,
)
from backend.apps.scheduling.services.run_contracts import require_text_reason


SECTION_NUMBER_PATTERN = re.compile(r"^S(?P<semester>[12])-(?P<sequence>\d+)$")


def _append_once(values, value):
    if value not in values:
        values.append(value)


def _section_payload(section, *, protection_reasons=()):
    return {
        "section_id": section.id,
        "section_number": section.section_number,
        "semester": section.semester,
        "lifecycle_status": section.lifecycle_status,
        "capacity_min": section.capacity_min,
        "capacity_max": section.capacity_max,
        "protection_reasons": list(protection_reasons),
    }


def _reserved_section_numbers(course_id, academic_year_id, sections):
    """Reserve live and historical labels so reconciliation never reuses one."""

    reserved = {section.section_number for section in sections}
    historical_values = SectionPlanningReconciliationAction.objects.filter(
        section__course_id=course_id,
        section__academic_year_id=academic_year_id,
    ).values_list("previous_section_number", "new_section_number")
    for previous, new in historical_values:
        if previous:
            reserved.add(previous)
        if new:
            reserved.add(new)
    return reserved


def _number_allocator(reserved):
    """Return a closure allocating deterministic semester-prefixed labels."""

    next_sequence = {SEMESTER_FALL: 1, SEMESTER_WINTER: 1}
    for value in reserved:
        match = SECTION_NUMBER_PATTERN.fullmatch(value)
        if match:
            semester = int(match.group("semester"))
            next_sequence[semester] = max(
                next_sequence[semester],
                int(match.group("sequence")) + 1,
            )

    def allocate(semester):
        sequence = next_sequence[semester]
        candidate = f"S{semester}-{sequence:02d}"
        while candidate in reserved:
            sequence += 1
            candidate = f"S{semester}-{sequence:02d}"
        next_sequence[semester] = sequence + 1
        reserved.add(candidate)
        return candidate

    return allocate


def _empty_actions():
    return {
        "keep": [],
        "move": [],
        "retire": [],
        "reactivate": [],
        "create": [],
    }


def _kept_action(section, protection_reasons):
    return {
        **_section_payload(section, protection_reasons=protection_reasons),
        "action": SECTION_RECONCILIATION_ACTION_KEPT,
    }


def _build_course_delta(
    course,
    all_sections,
    proposed_counts,
    dependencies,
    *,
    academic_year_id,
):
    """Return a deterministic, non-mutating section reconciliation delta."""

    actions = _empty_actions()
    active = [
        section for section in all_sections
        if section.lifecycle_status == SECTION_LIFECYCLE_ACTIVE
    ]
    retired = [
        section for section in all_sections
        if section.lifecycle_status == SECTION_LIFECYCLE_RETIRED
    ]
    current_counts = {
        semester: sum(section.semester == semester for section in active)
        for semester in (SEMESTER_FALL, SEMESTER_WINTER)
    }

    protected = {SEMESTER_FALL: [], SEMESTER_WINTER: []}
    mutable = {SEMESTER_FALL: [], SEMESTER_WINTER: []}
    protection_by_id = {}
    for section in active:
        reasons = _protection_reasons(section, dependencies)
        protection_by_id[section.id] = reasons
        target = protected if reasons else mutable
        target[section.semester].append(section)
    for values in (*protected.values(), *mutable.values()):
        # Section has no creation timestamp, so its monotonic primary key is the
        # durable proxy for age. Preserve the oldest usable identity first even
        # if an earlier reconciliation changed its human-facing number.
        values.sort(key=lambda item: (item.id, item.section_number))

    conflicts = []
    for semester in (SEMESTER_FALL, SEMESTER_WINTER):
        if len(protected[semester]) > proposed_counts[semester]:
            conflicts.append({
                "code": "protected_sections_exceed_target",
                "course_id": course.id,
                "semester": semester,
                "protected_section_count": len(protected[semester]),
                "proposed_section_count": proposed_counts[semester],
                "section_ids": [section.id for section in protected[semester]],
                "message": (
                    f"{course.course_code} has {len(protected[semester])} fixed Semester "
                    f"{semester} section(s), above the proposed count of {proposed_counts[semester]}."
                ),
            })

    if conflicts:
        for section in active:
            actions["keep"].append(
                _kept_action(section, protection_by_id[section.id])
            )
        return actions, current_counts, current_counts, conflicts

    deficits = {}
    surplus = {}
    for semester in (SEMESTER_FALL, SEMESTER_WINTER):
        remaining_target = proposed_counts[semester] - len(protected[semester])
        kept_mutable = mutable[semester][:remaining_target]
        surplus[semester] = mutable[semester][remaining_target:]
        for section in protected[semester]:
            actions["keep"].append(
                _kept_action(section, protection_by_id[section.id])
            )
        for section in kept_mutable:
            actions["keep"].append(_kept_action(section, ()))
        deficits[semester] = remaining_target - len(kept_mutable)

    reserved = _reserved_section_numbers(
        course.id,
        academic_year_id,
        all_sections,
    )
    allocate_number = _number_allocator(reserved)

    # Move excess active generated drafts before reactivating or creating rows.
    for source, target in (
        (SEMESTER_FALL, SEMESTER_WINTER),
        (SEMESTER_WINTER, SEMESTER_FALL),
    ):
        move_count = min(len(surplus[source]), deficits[target])
        moving = surplus[source][:move_count]
        surplus[source] = surplus[source][move_count:]
        deficits[target] -= move_count
        for section in moving:
            actions["move"].append({
                "action": SECTION_RECONCILIATION_ACTION_MOVED,
                "section_id": section.id,
                "previous_section_number": section.section_number,
                "new_section_number": allocate_number(target),
                "previous_semester": source,
                "new_semester": target,
                "previous_lifecycle_status": SECTION_LIFECYCLE_ACTIVE,
                "new_lifecycle_status": SECTION_LIFECYCLE_ACTIVE,
                "previous_capacity_min": section.capacity_min,
                "previous_capacity_max": section.capacity_max,
                "new_capacity_min": section.capacity_min,
                "new_capacity_max": section.capacity_max,
                "protection_reasons": [],
            })

    # Retired generated drafts are reusable; manual or dependency-bearing rows
    # remain historical and are never revived automatically.
    eligible_retired = [
        section for section in retired
        if section.planning_approval_course_id is not None
        and not _protection_reasons(section, dependencies)
    ]
    eligible_retired.sort(key=lambda item: (item.semester, item.id, item.section_number))
    used_retired_ids = set()
    for target in (SEMESTER_FALL, SEMESTER_WINTER):
        same_semester = [
            section for section in eligible_retired
            if section.id not in used_retired_ids and section.semester == target
        ]
        other_semester = [
            section for section in eligible_retired
            if section.id not in used_retired_ids and section.semester != target
        ]
        for section in same_semester + other_semester:
            if deficits[target] == 0:
                break
            used_retired_ids.add(section.id)
            deficits[target] -= 1
            new_number = (
                section.section_number
                if section.semester == target
                else allocate_number(target)
            )
            actions["reactivate"].append({
                "action": SECTION_RECONCILIATION_ACTION_REACTIVATED,
                "section_id": section.id,
                "previous_section_number": section.section_number,
                "new_section_number": new_number,
                "previous_semester": section.semester,
                "new_semester": target,
                "previous_lifecycle_status": SECTION_LIFECYCLE_RETIRED,
                "new_lifecycle_status": SECTION_LIFECYCLE_ACTIVE,
                "previous_capacity_min": section.capacity_min,
                "previous_capacity_max": section.capacity_max,
                "new_capacity_min": course.capacity_profile.hard_min,
                "new_capacity_max": course.capacity_profile.hard_max,
                "protection_reasons": [],
            })

    for semester in (SEMESTER_FALL, SEMESTER_WINTER):
        for section in surplus[semester]:
            actions["retire"].append({
                "action": SECTION_RECONCILIATION_ACTION_RETIRED,
                "section_id": section.id,
                "previous_section_number": section.section_number,
                "new_section_number": section.section_number,
                "previous_semester": semester,
                "new_semester": semester,
                "previous_lifecycle_status": SECTION_LIFECYCLE_ACTIVE,
                "new_lifecycle_status": SECTION_LIFECYCLE_RETIRED,
                "previous_capacity_min": section.capacity_min,
                "previous_capacity_max": section.capacity_max,
                "new_capacity_min": section.capacity_min,
                "new_capacity_max": section.capacity_max,
                "protection_reasons": [],
            })
        for _ in range(deficits[semester]):
            actions["create"].append({
                "action": SECTION_RECONCILIATION_ACTION_CREATED,
                "section_id": None,
                "previous_section_number": "",
                "new_section_number": allocate_number(semester),
                "previous_semester": None,
                "new_semester": semester,
                "previous_lifecycle_status": "",
                "new_lifecycle_status": SECTION_LIFECYCLE_ACTIVE,
                "previous_capacity_min": None,
                "previous_capacity_max": None,
                "new_capacity_min": course.capacity_profile.hard_min,
                "new_capacity_max": course.capacity_profile.hard_max,
                "protection_reasons": [],
            })

    final_counts = dict(proposed_counts)
    for values in actions.values():
        values.sort(key=lambda item: (
            item.get("new_semester") or item.get("semester") or 0,
            item.get("new_section_number") or item.get("section_number") or "",
            item.get("section_id") or 0,
        ))
    return actions, current_counts, final_counts, conflicts


def preview_section_plan_reconciliation(run, *, selections=None):
    """Return the exact non-mutating delta for a revised section plan."""

    result_by_course, approved_course_ids, normalized = _normalize_selections(run, selections)
    normalized.sort(key=lambda item: (result_by_course[item["course_id"]]["course_code"], item["course_id"]))
    selected_course_ids = [item["course_id"] for item in normalized]
    courses = {
        course.id: course
        for course in Course.objects.select_related("capacity_profile").filter(id__in=selected_course_ids)
    }
    sections_by_course = {course_id: [] for course_id in selected_course_ids}
    all_sections = list(
        Section.objects.select_related("planning_approval_course")
        .filter(academic_year_id=run.academic_year_id, course_id__in=selected_course_ids)
        .order_by("course_id", "semester", "section_number", "id")
    )
    for section in all_sections:
        sections_by_course[section.course_id].append(section)
    dependencies = _dependency_sets([section.id for section in all_sections])

    conflicts = []
    validation_errors = []
    course_reviews = []
    for selection in normalized:
        course_id = selection["course_id"]
        result = result_by_course[course_id]
        course = courses.get(course_id)
        warnings = list(result.get("warnings", []))
        proposed = {
            SEMESTER_FALL: selection["semester_1_count"],
            SEMESTER_WINTER: selection["semester_2_count"],
        }
        item_errors = []
        item_conflicts = []
        actions = _empty_actions()
        current_counts = {SEMESTER_FALL: 0, SEMESTER_WINTER: 0}
        final_counts = dict(current_counts)
        current_policy = None
        current_allowed_semester = None

        if course_id in approved_course_ids:
            # Explicit selections must obey the same append-only rule as an
            # omitted "all remaining" selection. A revised decision belongs to
            # a newer run, never a second application of this frozen result.
            conflict = {
                "code": "course_already_approved_from_run",
                "course_id": course_id,
                "message": "This course has already been approved from this planning run.",
            }
            conflicts.append(conflict)
            item_conflicts.append(conflict["code"])

        if course is None:
            error = {
                "code": "course_no_longer_exists",
                "course_id": course_id,
                "message": "The course no longer exists and cannot be reconciled.",
            }
            validation_errors.append(error)
            item_errors.append(error["code"])
        else:
            current_policy = {
                "hard_min": course.capacity_profile.hard_min,
                "soft_min": course.capacity_profile.soft_min,
                "target": course.capacity_profile.target,
                "soft_max": course.capacity_profile.soft_max,
                "hard_max": course.capacity_profile.hard_max,
            }
            current_allowed_semester = course.allowed_semester
            if current_policy != result["capacity_policy"] or current_allowed_semester != result["allowed_semester"]:
                _append_once(warnings, "planning_configuration_changed_since_run")
            if proposed[SEMESTER_WINTER] and current_allowed_semester == COURSE_ALLOWED_SEMESTER_1_ONLY:
                error = {
                    "code": "course_not_allowed_in_semester_2",
                    "course_id": course_id,
                    "message": f"{course.course_code} is currently restricted to Semester 1.",
                }
                validation_errors.append(error)
                item_errors.append(error["code"])
            if proposed[SEMESTER_FALL] and current_allowed_semester == COURSE_ALLOWED_SEMESTER_2_ONLY:
                error = {
                    "code": "course_not_allowed_in_semester_1",
                    "course_id": course_id,
                    "message": f"{course.course_code} is currently restricted to Semester 2.",
                }
                validation_errors.append(error)
                item_errors.append(error["code"])
            if not item_errors:
                actions, current_counts, final_counts, delta_conflicts = _build_course_delta(
                    course,
                    sections_by_course[course_id],
                    proposed,
                    dependencies,
                    academic_year_id=run.academic_year_id,
                )
                conflicts.extend(delta_conflicts)
                item_conflicts.extend(item["code"] for item in delta_conflicts)

        recommended_one = result["semester_1_count"]
        recommended_two = result["semester_2_count"]
        if proposed[SEMESTER_FALL] != recommended_one or proposed[SEMESTER_WINTER] != recommended_two:
            _append_once(warnings, "counselor_adjusted_section_counts")
        proposed_total = proposed[SEMESTER_FALL] + proposed[SEMESTER_WINTER]
        course_reviews.append({
            "course_id": course_id,
            "course_code": result["course_code"],
            "priority_tier": result["priority_tier"],
            "predicted_enrollment": result["predicted_enrollment"],
            "unmet_demand": result["unmet_demand"],
            "current_semester_1_count": current_counts[SEMESTER_FALL],
            "current_semester_2_count": current_counts[SEMESTER_WINTER],
            "current_annual_count": current_counts[SEMESTER_FALL] + current_counts[SEMESTER_WINTER],
            "recommended_semester_1_count": recommended_one,
            "recommended_semester_2_count": recommended_two,
            "recommended_annual_count": recommended_one + recommended_two,
            "proposed_semester_1_count": proposed[SEMESTER_FALL],
            "proposed_semester_2_count": proposed[SEMESTER_WINTER],
            "proposed_annual_count": proposed_total,
            "projected_semester_1_count": final_counts[SEMESTER_FALL],
            "projected_semester_2_count": final_counts[SEMESTER_WINTER],
            "expected_enrollment_per_section": (
                result["predicted_enrollment"] / proposed_total if proposed_total else 0
            ),
            "run_capacity_policy": result["capacity_policy"],
            "current_capacity_policy": current_policy,
            "run_allowed_semester": result["allowed_semester"],
            "current_allowed_semester": current_allowed_semester,
            "actions": actions,
            "warnings": warnings,
            "reasons": result.get("reasons", []),
            "conflicts": item_conflicts,
            "validation_errors": item_errors,
            "can_reconcile": not item_conflicts and not item_errors,
        })

    if not normalized:
        conflicts.append({
            "code": "no_unapproved_courses_remaining",
            "message": "No unapproved courses remain in this planning run.",
        })

    action_totals = {
        action: sum(len(item["actions"][action]) for item in course_reviews)
        for action in _empty_actions()
    }
    preview = {
        "planning_run_id": run.id,
        "academic_year": run.academic_year_id,
        "courses": course_reviews,
        "selected_course_count": len(course_reviews),
        "current_section_count": sum(item["current_annual_count"] for item in course_reviews),
        "proposed_section_count": sum(item["proposed_annual_count"] for item in course_reviews),
        "action_totals": action_totals,
        "approved_course_ids": sorted(approved_course_ids),
        "diagnostics": run.result.get("diagnostics", []),
        "conflicts": conflicts,
        "validation_errors": validation_errors,
        "can_reconcile": bool(course_reviews) and not conflicts and not validation_errors,
    }
    canonical = json.dumps(preview, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    preview["preview_token"] = sha256(canonical.encode("utf-8")).hexdigest()
    return preview


def _create_action(course_reconciliation, section, action):
    SectionPlanningReconciliationAction.objects.create(
        course_reconciliation=course_reconciliation,
        section=section,
        action=action["action"],
        previous_lifecycle_status=action.get("previous_lifecycle_status", action.get("lifecycle_status", "")),
        new_lifecycle_status=action.get("new_lifecycle_status", action.get("lifecycle_status", "")),
        previous_semester=action.get("previous_semester", action.get("semester")),
        new_semester=action.get("new_semester", action.get("semester")),
        previous_section_number=action.get("previous_section_number", action.get("section_number", "")),
        new_section_number=action.get("new_section_number", action.get("section_number", "")),
        previous_capacity_min=action.get("previous_capacity_min", action.get("capacity_min")),
        previous_capacity_max=action.get("previous_capacity_max", action.get("capacity_max")),
        new_capacity_min=action.get("new_capacity_min", action.get("capacity_min")),
        new_capacity_max=action.get("new_capacity_max", action.get("capacity_max")),
        protection_reasons=action.get("protection_reasons", []),
    )


@transaction.atomic
def reconcile_section_planning_run(
    run,
    *,
    reconciled_by,
    preview_token,
    selections=None,
    reason,
):
    """Apply the exact reviewed delta and record one immutable audit graph."""

    # Enforce the audit contract for service callers as well as HTTP callers.
    reason = require_text_reason(
        reason,
        message="A reconciliation reason is required.",
        error_class=PlanningApprovalValidationError,
    )
    run = SectionPlanningRun.objects.select_for_update().get(pk=run.pk)
    ensure_academic_year_offerings(run.academic_year, actor=reconciled_by)
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
    locked_sections = list(
        Section.objects.select_for_update()
        .filter(academic_year_id=run.academic_year_id, course_id__in=selected_course_ids)
        .order_by("course_id", "id")
    )
    preview = preview_section_plan_reconciliation(run, selections=normalized)
    if preview["validation_errors"]:
        raise PlanningApprovalValidationError({
            "detail": "The proposed section reconciliation is not valid.",
            "validation_errors": preview["validation_errors"],
        })
    # Compare the token before applying current-state conflicts. A caller that
    # reviewed an earlier state should always be told to preview again, even if
    # the intervening edit also introduced a new protected-section conflict.
    if preview["preview_token"] != preview_token:
        raise PlanningApprovalConflictError({
            "detail": "The section state changed after preview. Preview the reconciliation again.",
            "conflicts": [{
                "code": "reconciliation_preview_stale",
                "message": "The reconciliation preview is stale.",
            }],
        })
    if preview["conflicts"]:
        raise PlanningApprovalConflictError({
            "detail": "The proposed reconciliation conflicts with existing section decisions.",
            "conflicts": preview["conflicts"],
        })

    courses = {
        course.id: course
        for course in Course.objects.select_related("capacity_profile").filter(id__in=selected_course_ids)
    }
    offering_groups = {
        offering.course_id: offering.delivery_group
        for offering in CourseOffering.objects.select_related("delivery_group").filter(
            academic_year_id=run.academic_year_id,
            course_id__in=selected_course_ids,
        )
    }
    sections = {section.id: section for section in locked_sections}
    approval = SectionPlanningApproval.objects.create(
        planning_run=run,
        approved_by=reconciled_by,
        reason=reason,
    )
    reconciliation = SectionPlanningReconciliation.objects.create(
        approval=approval,
        preview_token=preview_token,
        previous_active_section_count=preview["current_section_count"],
        final_active_section_count=preview["proposed_section_count"],
    )

    for item in preview["courses"]:
        course = courses[item["course_id"]]
        approval_course = SectionPlanningApprovalCourse.objects.create(
            approval=approval,
            course=course,
            recommended_semester_1_count=item["recommended_semester_1_count"],
            recommended_semester_2_count=item["recommended_semester_2_count"],
            approved_semester_1_count=item["proposed_semester_1_count"],
            approved_semester_2_count=item["proposed_semester_2_count"],
        )
        course_reconciliation = SectionPlanningReconciliationCourse.objects.create(
            reconciliation=reconciliation,
            approval_course=approval_course,
            previous_semester_1_count=item["current_semester_1_count"],
            previous_semester_2_count=item["current_semester_2_count"],
            final_semester_1_count=item["proposed_semester_1_count"],
            final_semester_2_count=item["proposed_semester_2_count"],
        )

        for action in item["actions"]["keep"]:
            _create_action(course_reconciliation, sections[action["section_id"]], action)
        for action in item["actions"]["move"]:
            section = sections[action["section_id"]]
            section.semester = action["new_semester"]
            section.section_number = action["new_section_number"]
            section.save(update_fields=["semester", "section_number"])
            _create_action(course_reconciliation, section, action)
        for action in item["actions"]["retire"]:
            section = sections[action["section_id"]]
            section.lifecycle_status = SECTION_LIFECYCLE_RETIRED
            section.save(update_fields=["lifecycle_status"])
            _create_action(course_reconciliation, section, action)
        for action in item["actions"]["reactivate"]:
            section = sections[action["section_id"]]
            section.lifecycle_status = SECTION_LIFECYCLE_ACTIVE
            section.semester = action["new_semester"]
            section.section_number = action["new_section_number"]
            section.capacity_min = action["new_capacity_min"]
            section.capacity_max = action["new_capacity_max"]
            section.save(update_fields=[
                "lifecycle_status", "semester", "section_number",
                "capacity_min", "capacity_max",
            ])
            _create_action(course_reconciliation, section, action)
        for action in item["actions"]["create"]:
            section = Section.objects.create(
                course=course,
                delivery_group=offering_groups[course.id],
                section_number=action["new_section_number"],
                academic_year_id=run.academic_year_id,
                semester=action["new_semester"],
                teacher=None,
                capacity_min=action["new_capacity_min"],
                capacity_max=action["new_capacity_max"],
                is_locked=False,
                lifecycle_status=SECTION_LIFECYCLE_ACTIVE,
                planning_approval_course=approval_course,
            )
            sections[section.id] = section
            _create_action(course_reconciliation, section, action)
    return reconciliation
