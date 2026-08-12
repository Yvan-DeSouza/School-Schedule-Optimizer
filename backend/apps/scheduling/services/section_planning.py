"""Persistence orchestration for planning runs and counselor approvals.

This module deliberately never imports scheduling_engine; engine_adapter is the
single Django-to-engine boundary.  Keeping that rule here prevents ORM objects
from leaking into the pure solver package.

There are two intentionally different write paths:

* ``create_section_planning_run`` stores a frozen solver input and result but
  never creates operational ``Section`` rows.
* ``approve_section_planning_run`` turns an explicitly reviewed subset into
  draft sections inside one transaction.

Approval previews and writes share the same validation function so the UI sees
the same conflicts that the transactional endpoint will enforce.
"""

from django.db import transaction

from backend.apps.common.exceptions import DomainConflictError, DomainValidationError
from backend.apps.common.constants import (
    COURSE_ALLOWED_SEMESTER_1_ONLY,
    COURSE_ALLOWED_SEMESTER_2_ONLY,
    SECTION_PLANNING_RUN_STATUS_COMPLETE,
    SECTION_PLANNING_RUN_STATUS_INFEASIBLE,
    SEMESTER_FALL,
    SEMESTER_WINTER,
)
from backend.apps.common.constants import COURSE_OFFERING_STATUS_OFFERED
from backend.apps.courses.models import (
    Course, CourseOffering, HalfSemesterCoursePair, HalfSemesterSectionPair,
    Section,
)
from backend.apps.courses.services.offerings import ensure_academic_year_offerings
from backend.apps.scheduling.codes import (
    COMBINED_OFFERING_REQUIRES_PHYSICAL_STAFFING_WORKFLOW,
    COURSE_ALREADY_APPROVED_FROM_RUN,
    COURSE_NO_LONGER_EXISTS,
    COURSE_NOT_ALLOWED_IN_SEMESTER_1,
    COURSE_NOT_ALLOWED_IN_SEMESTER_2,
    COURSE_OFFERING_NOT_ACTIVE,
    EXISTING_SECTIONS_FOR_COURSE_YEAR,
    NO_UNAPPROVED_COURSES_REMAINING,
)
from backend.apps.scheduling.models import (
    CapacityProfile,
    SectionPlanningApproval,
    SectionPlanningApprovalCourse,
    SectionPlanningRun,
)
from backend.apps.scheduling.services.engine_adapter import (
    get_section_count_plan_with_snapshot,
)
from backend.apps.scheduling.services.run_contracts import (
    ensure_unique_selection,
)


class PlanningApprovalValidationError(DomainValidationError):
    """A proposed approval is malformed or violates current catalog rules."""


class PlanningApprovalConflictError(DomainConflictError):
    """A valid proposal would overwrite an existing planning decision."""


def create_section_planning_run(*, academic_year_id, created_by, course_constraints, teacher_capacity_adjustments):
    """Execute one immutable base/what-if run and persist its full provenance."""

    from backend.apps.common.models import AcademicYear

    ensure_academic_year_offerings(
        AcademicYear.objects.get(pk=academic_year_id),
        actor=created_by,
    )
    # Store the user's scenario separately from the expanded DTO snapshot.  The
    # former is concise for review; the latter is sufficient for future audit.
    scenario = {
        "course_constraints": list(course_constraints),
        "teacher_capacity_adjustments": list(teacher_capacity_adjustments),
    }
    # The adapter returns the result and the exact DTO input used for that same
    # invocation, avoiding a race caused by loading a snapshot in a second pass.
    result, snapshot = get_section_count_plan_with_snapshot(
        academic_year_id,
        course_constraints=course_constraints,
        teacher_capacity_adjustments=teacher_capacity_adjustments,
    )
    # A solver-level infeasible result is still a successfully recorded run, not
    # an application exception or partially written failure.
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
    """Validate approval eligibility and index the frozen per-course results."""

    # Approval is intentionally limited to completed feasible runs.  Infeasible
    # runs remain valuable audit records but cannot create draft offerings.
    if run.status != SECTION_PLANNING_RUN_STATUS_COMPLETE or run.result.get("status") != "complete":
        raise PlanningApprovalValidationError({
            "detail": "Only a completed, feasible section-planning run can be approved."
        })
    return {
        int(item["course_id"]): item
        for item in run.result.get("courses", [])
    }


def _normalize_selections(run, selections):
    """Resolve omitted selections and reject course IDs outside the run."""

    result_by_course = _course_results_by_id(run)
    # A course approved with zero sections still counts as reviewed, so the audit
    # table—not the presence of generated Section rows—is the source of truth.
    approved_course_ids = set(
        SectionPlanningApprovalCourse.objects.filter(
            approval__planning_run=run,
        ).values_list("course_id", flat=True)
    )
    if selections is None:
        # Omission means "all remaining recommendations."  An explicit empty
        # array is rejected earlier by the request serializer to avoid ambiguity.
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
        # Copy serializer mappings before enriching/iterating so callers do not
        # observe accidental mutation of their validated data.
        normalized = [dict(item) for item in selections]
        ensure_unique_selection(
            normalized,
            "course_id",
            field="courses",
            message="Each course may be selected only once.",
            error_class=PlanningApprovalValidationError,
        )

    unknown_course_ids = sorted({
        item["course_id"]
        for item in normalized
        if item["course_id"] not in result_by_course
    })
    if unknown_course_ids:
        # A catalog course can exist today yet still be invalid for this run if it
        # was absent from the frozen result.
        raise PlanningApprovalValidationError({
            "courses": (
                "Every selected course must be present in the planning run result. "
                f"Unknown course ids: {unknown_course_ids}."
            )
        })
    return result_by_course, approved_course_ids, normalized


def _append_once(values, value):
    """Append a stable warning code without duplicating an engine warning."""

    if value not in values:
        values.append(value)


def preview_section_planning_approval(run, *, selections=None):
    """Build the exact counselor review payload without changing database state.

    The preview deliberately compares the frozen run configuration with the
    current course configuration.  Counts originate from the run (or explicit
    counselor edits), while actual draft sections must obey current semester
    restrictions and use current capacity-policy bounds.
    """

    result_by_course, approved_course_ids, selections = _normalize_selections(run, selections)
    selected_course_ids = sorted({item["course_id"] for item in selections})
    # Courses are loaded in one query because large runs may contain hundreds of
    # offerings.  Missing rows are handled as structured validation errors.
    courses = {
        course.id: course
        for course in Course.objects.select_related("capacity_profile").filter(
            id__in=selected_course_ids,
        )
    }
    offerings = {
        offering.course_id: offering
        for offering in CourseOffering.objects.select_related("delivery_group").prefetch_related(
            "delivery_group__offerings"
        ).filter(
            academic_year_id=run.academic_year_id,
            course_id__in=selected_course_ids,
        )
    }
    half_course_segment = {}
    for pair in HalfSemesterCoursePair.objects.filter(is_active=True).select_related(
        "first_course", "second_course"
    ):
        # Catalog pairs supply the practical default sequence. An unpaired
        # half-semester request remains schedulable in the first half and is
        # surfaced later for counselor review rather than rejected here.
        half_course_segment[pair.first_course_id] = "first_half"
        half_course_segment[pair.second_course_id] = "second_half"
    existing_by_course = {}
    # Any existing section is a hard conflict.  The approval feature has no
    # implicit replace/delete semantics; reconciliation is a separate workflow.
    for section in Section.objects.filter(
        academic_year_id=run.academic_year_id,
        course_id__in=selected_course_ids,
    ).order_by("course_id", "section_number"):
        existing_by_course.setdefault(section.course_id, []).append({
            "section_id": section.id,
            "section_number": section.section_number,
            "semester": section.semester,
            "lifecycle_status": section.lifecycle_status,
        })

    conflicts = []
    validation_errors = []
    course_reviews = []
    for selection in selections:
        # Frozen result values explain the recommendation; current Course values
        # determine whether it remains safe to materialize today.
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
            # Counselor adjustments are allowed and audited, but they must be
            # visually distinguishable from the solver recommendation.
            _append_once(warnings, "counselor_adjusted_section_counts")

        item_validation_errors = []
        item_conflicts = []
        if course is None:
            # Planning snapshots intentionally survive catalog deletion.  They
            # remain readable, but a missing live Course cannot back a Section FK.
            error = {
                "code": COURSE_NO_LONGER_EXISTS,
                "course_id": course_id,
                "message": "The course no longer exists and cannot be approved.",
            }
            validation_errors.append(error)
            item_validation_errors.append(error["code"])
            current_capacity_policy = None
            current_allowed_semester = None
        else:
            # Section.capacity_min/max are compatibility fields.  The approval
            # service fills them from the current profile's hard bounds.
            current_capacity_policy = {
                "hard_min": course.capacity_profile.hard_min,
                "soft_min": course.capacity_profile.soft_min,
                "target": course.capacity_profile.target,
                "soft_max": course.capacity_profile.soft_max,
                "hard_max": course.capacity_profile.hard_max,
            }
            current_allowed_semester = course.allowed_semester
            offering = offerings.get(course_id)
            if offering and (
                offering.status != COURSE_OFFERING_STATUS_OFFERED
                or not offering.delivery_group_id
            ):
                error = {
                    "code": COURSE_OFFERING_NOT_ACTIVE,
                    "course_id": course_id,
                    "message": f"{course.course_code} is not an active offering for this year.",
                }
                validation_errors.append(error)
                item_validation_errors.append(error["code"])
            elif offering.delivery_group.offerings.count() > 1:
                error = {
                    "code": COMBINED_OFFERING_REQUIRES_PHYSICAL_STAFFING_WORKFLOW,
                    "course_id": course_id,
                    "message": (
                        f"{course.course_code} belongs to a combined physical class. "
                        "Use the delivery-group staffing workflow."
                    ),
                }
                validation_errors.append(error)
                item_validation_errors.append(error["code"])
            if (
                current_capacity_policy != result["capacity_policy"]
                or current_allowed_semester != result["allowed_semester"]
            ):
                # Configuration drift is a review warning rather than an
                # automatic rejection unless it makes the semester split illegal.
                _append_once(warnings, "planning_configuration_changed_since_run")
            if current_allowed_semester == COURSE_ALLOWED_SEMESTER_1_ONLY and proposed_semester_2:
                error = {
                    "code": COURSE_NOT_ALLOWED_IN_SEMESTER_2,
                    "course_id": course_id,
                    "message": f"{course.course_code} is currently restricted to Semester 1.",
                }
                validation_errors.append(error)
                item_validation_errors.append(error["code"])
            if current_allowed_semester == COURSE_ALLOWED_SEMESTER_2_ONLY and proposed_semester_1:
                error = {
                    "code": COURSE_NOT_ALLOWED_IN_SEMESTER_1,
                    "course_id": course_id,
                    "message": f"{course.course_code} is currently restricted to Semester 2.",
                }
                validation_errors.append(error)
                item_validation_errors.append(error["code"])

        if course_id in approved_course_ids:
            # This catches repeated zero-section approvals as well as approvals
            # that already generated rows.
            conflict = {
                "code": COURSE_ALREADY_APPROVED_FROM_RUN,
                "course_id": course_id,
                "message": "This course has already been approved from this planning run.",
            }
            conflicts.append(conflict)
            item_conflicts.append(conflict["code"])
        existing_sections = existing_by_course.get(course_id, [])
        if existing_sections:
            # Include concrete section identifiers so the counselor can inspect
            # the rows that require a future replace/reconciliation decision.
            course_code = result["course_code"]
            conflict = {
                "code": EXISTING_SECTIONS_FOR_COURSE_YEAR,
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
        # Keep recommended and proposed values side-by-side; a frontend should
        # not need to reconstruct audit-critical differences itself.
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
                # Zero-section decisions are valid controlled shortages and avoid
                # division-by-zero by reporting a neutral zero class size.
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
        # This normally means every course in the run has already been reviewed.
        conflicts.append({
            "code": NO_UNAPPROVED_COURSES_REMAINING,
            "message": "No unapproved courses remain in this planning run.",
        })

    return {
        # The response is deliberately JSON-ready because both review endpoints
        # return it directly and approval reuses it inside the transaction.
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
    """Atomically materialize reviewed counts as auditable draft sections."""

    reason = reason.strip() if isinstance(reason, str) else ""
    # Serialize approvals from the same run.  This makes the already-approved
    # check reliable even when two counselors submit concurrently.
    run = SectionPlanningRun.objects.select_for_update().get(pk=run.pk)
    # Old imported/manual run records may predate year-specific offerings. New
    # runs create them before solving, while this compatibility guard ensures a
    # legacy standalone approval still receives a canonical delivery group.
    ensure_academic_year_offerings(run.academic_year, actor=approved_by)
    _, _, normalized = _normalize_selections(run, selections)
    selected_course_ids = sorted({item["course_id"] for item in normalized})
    # Lock courses in deterministic ID order.  Besides preventing catalog edits,
    # the row locks serialize approvals from different runs targeting the same
    # course/year and avoid deadlock-prone arbitrary lock ordering.
    locked_courses = list(
        Course.objects.select_for_update()
        .filter(id__in=selected_course_ids)
        .order_by("id")
    )
    # A shared capacity profile can be edited independently of its courses.  Lock
    # it too so every generated section in this approval receives one coherent
    # set of hard capacity bounds.
    list(
        CapacityProfile.objects.select_for_update()
        .filter(id__in={course.capacity_profile_id for course in locked_courses})
        .order_by("id")
    )
    # Re-run the normal preview after locks are held.  Never trust a preview that
    # may have been shown seconds earlier against now-stale database state.
    preview = preview_section_planning_approval(run, selections=normalized)
    if preview["validation_errors"]:
        raise PlanningApprovalValidationError({
            "detail": "The proposed section counts are not valid.",
            "validation_errors": preview["validation_errors"],
        })
    if preview["conflicts"]:
        # Conflicts map to HTTP 409 at the view boundary; validation errors map
        # to 400.  Keeping them separate helps clients choose the right recovery.
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
    offerings = {
        offering.course_id: offering
        for offering in CourseOffering.objects.select_related("delivery_group").filter(
            academic_year_id=run.academic_year_id,
            course_id__in=selected_course_ids,
            status=COURSE_OFFERING_STATUS_OFFERED,
        )
    }
    # Create the audit header before its normalized per-course decisions.  The
    # surrounding transaction guarantees no orphaned audit row can survive.
    approval = SectionPlanningApproval.objects.create(
        planning_run=run,
        approved_by=approved_by,
        reason=reason,
    )
    created_sections_by_course_semester = {}
    for item in preview["courses"]:
        course = courses[item["course_id"]]
        # Preserve both values even when they match.  Future readers can prove
        # whether the counselor accepted or adjusted the solver output.
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
                # Semester-prefixed numbering is deterministic and unique across
                # both terms under the existing course/year uniqueness rule.
                section = Section.objects.create(
                    course=course,
                    delivery_group=offerings[course.id].delivery_group,
                    section_number=f"S{semester}-{sequence:02d}",
                    academic_year_id=run.academic_year_id,
                    semester=semester,
                    half_semester_segment=(
                        half_course_segment.get(course.id, "first_half")
                        if course.duration == "half_semester"
                        else None
                    ),
                    teacher=None,
                    capacity_min=course.capacity_profile.hard_min,
                    capacity_max=course.capacity_profile.hard_max,
                    is_locked=False,
                    # This link makes every generated draft traceable through the
                    # approval header to the immutable source planning run/user.
                    planning_approval_course=approved_course,
                )
                created_sections_by_course_semester.setdefault(
                    (course.id, semester), []
                ).append(section)
    # Pair matching generated sections only after every selected course has
    # materialized. Equal ordinal sections share their semester/A-D placement
    # and later named teacher; unmatched rows remain valid but reviewable
    # half-semester exceptions rather than disappearing from the plan.
    for pair in HalfSemesterCoursePair.objects.filter(is_active=True).order_by("id"):
        for semester in (SEMESTER_FALL, SEMESTER_WINTER):
            first_sections = created_sections_by_course_semester.get((pair.first_course_id, semester), ())
            second_sections = created_sections_by_course_semester.get((pair.second_course_id, semester), ())
            for first, second in zip(first_sections, second_sections):
                HalfSemesterSectionPair.objects.create(
                    course_pair=pair,
                    first_section=first,
                    second_section=second,
                )
    return approval
