"""Stable, factual explanation payloads for counselor-facing run review.

This module deliberately translates facts that a run already stores or that a
review service has already validated.  It never asks a solver for a second
answer and it never assigns people, rooms, sections, or timeslots.  Keeping
the payload construction here gives every review endpoint the same small,
versioned vocabulary without creating a new persistence framework.
"""

from __future__ import annotations

from collections import Counter


EXPLANATION_SCHEMA_VERSION = 1

EXPLANATION_FACTOR_CATEGORY_HARD_CONSTRAINT = "hard_constraint"
EXPLANATION_FACTOR_CATEGORY_COUNSELOR_LOCK = "counselor_lock"
EXPLANATION_FACTOR_CATEGORY_FIXED_CONTEXT = "fixed_context"
EXPLANATION_FACTOR_CATEGORY_SOFT_OBJECTIVE = "soft_objective"
EXPLANATION_FACTOR_CATEGORY_DETERMINISTIC_TIE_BREAKER = "deterministic_tie_breaker"
EXPLANATION_FACTOR_CATEGORY_FEASIBILITY_WITNESS = "feasibility_witness"
EXPLANATION_FACTOR_CATEGORY_REVIEW_CONDITION = "review_condition"
EXPLANATION_FACTOR_CATEGORY_INSUFFICIENT_EVIDENCE = "insufficient_evidence"

EXPLANATION_FACTOR_CATEGORIES = frozenset({
    EXPLANATION_FACTOR_CATEGORY_HARD_CONSTRAINT,
    EXPLANATION_FACTOR_CATEGORY_COUNSELOR_LOCK,
    EXPLANATION_FACTOR_CATEGORY_FIXED_CONTEXT,
    EXPLANATION_FACTOR_CATEGORY_SOFT_OBJECTIVE,
    EXPLANATION_FACTOR_CATEGORY_DETERMINISTIC_TIE_BREAKER,
    EXPLANATION_FACTOR_CATEGORY_FEASIBILITY_WITNESS,
    EXPLANATION_FACTOR_CATEGORY_REVIEW_CONDITION,
    EXPLANATION_FACTOR_CATEGORY_INSUFFICIENT_EVIDENCE,
})


def explanation_factor(*, category, key, facts):
    """Return one structured factor after enforcing the public vocabulary.

    ``key`` identifies the factual subject of the factor.  It is intentionally
    not an English causal claim: client code can render it appropriately while
    the immutable facts remain the source of truth.
    """

    if category not in EXPLANATION_FACTOR_CATEGORIES:
        raise ValueError(f"Unknown explanation factor category: {category}")
    if not isinstance(key, str) or not key:
        raise ValueError("An explanation factor key is required.")
    return {"category": category, "key": key, "facts": dict(facts)}


def explanation_warning_items(*collections):
    """Normalize existing stable diagnostics without inventing new codes."""

    warnings = []
    for collection in collections:
        for item in collection or ():
            if isinstance(item, str):
                warnings.append({"code": item, "facts": {}})
                continue
            if not isinstance(item, dict) or not item.get("code"):
                continue
            warnings.append({
                "code": item["code"],
                "facts": {
                    key: value
                    for key, value in item.items()
                    if key not in {"code", "message", "detail"}
                },
            })
    return warnings


def build_review_summary(
    *,
    stage,
    run_id,
    academic_year_id,
    recommendation,
    factors=(),
    alternatives=(),
    trade_offs=(),
    warnings=(),
    available_actions=(),
):
    """Build the common versioned envelope shared by every review stage."""

    return {
        "explanation_schema_version": EXPLANATION_SCHEMA_VERSION,
        "decision": {
            "stage": stage,
            "run_id": run_id,
            "academic_year_id": academic_year_id,
        },
        "recommendation": dict(recommendation),
        "factors": list(factors),
        # Candidate-level alternatives are intentionally empty until a later,
        # dedicated evidence-capture increment can substantiate them.
        "alternatives": list(alternatives),
        "trade_offs": list(trade_offs),
        "warnings": list(warnings),
        "available_actions": list(available_actions),
    }


def build_section_planning_review_summary(run, preview):
    """Summarize demand, capacity, and staffing facts for section counts."""

    courses = preview.get("courses", ())
    predicted_enrollment = sum(item.get("predicted_enrollment", 0) for item in courses)
    unmet_demand = sum(item.get("unmet_demand", 0) for item in courses)
    staffing_feasible_count = sum(
        item.get("staffing_feasible_annual_count", item.get("recommended_annual_count", 0))
        for item in run.result.get("courses", ())
    )
    return build_review_summary(
        stage="section_count",
        run_id=run.id,
        academic_year_id=run.academic_year_id,
        recommendation={
            "course_count": len(courses),
            "recommended_section_count": sum(item.get("recommended_annual_count", 0) for item in courses),
            "proposed_section_count": preview.get("proposed_section_count", 0),
            "predicted_enrollment": predicted_enrollment,
            "unmet_demand": unmet_demand,
        },
        factors=(
            explanation_factor(
                category=EXPLANATION_FACTOR_CATEGORY_HARD_CONSTRAINT,
                key="normal_instruction_section_scope",
                facts={
                    "included_delivery_kind": "normal_instruction",
                    "excluded_delivery_kinds": ["online", "co_op"],
                    "excluded_commitment_types": ["study", "focus"],
                },
            ),
            explanation_factor(
                category=EXPLANATION_FACTOR_CATEGORY_FEASIBILITY_WITNESS,
                key="staffing_feasible_section_count",
                facts={"staffing_feasible_section_count": staffing_feasible_count},
            ),
            explanation_factor(
                category=EXPLANATION_FACTOR_CATEGORY_INSUFFICIENT_EVIDENCE,
                key="per_course_candidate_comparisons_not_captured",
                facts={},
            ),
        ),
        warnings=explanation_warning_items(
            run.result.get("diagnostics", ()), preview.get("conflicts", ()), preview.get("validation_errors", ()),
        ),
        available_actions=(
            {"code": "adjust_section_counts", "available": True},
            {"code": "approve_reviewed_section_counts", "available": bool(preview.get("can_approve"))},
            {"code": "create_new_section_count_run", "available": True},
        ),
    )


def build_section_budget_review_summary(run, preview):
    """Summarize an independently reviewed physical-section budget."""

    return build_review_summary(
        stage="section_budget",
        run_id=run.id,
        academic_year_id=run.academic_year_id,
        recommendation={
            "budget_type": run.budget_type,
            "section_budget": run.section_budget,
            "approved_total": preview.get("approved_total", 0),
            "offering_count": len(preview.get("offerings", ())),
            "affected_student_count": preview.get("affected_student_count", 0),
        },
        factors=(
            explanation_factor(
                category=EXPLANATION_FACTOR_CATEGORY_HARD_CONSTRAINT,
                key="physical_section_budget_limit",
                facts={"budget_type": run.budget_type, "section_budget": run.section_budget},
            ),
            explanation_factor(
                category=EXPLANATION_FACTOR_CATEGORY_FIXED_CONTEXT,
                key="approved_backup_request_resolutions",
                facts={"resolution_count": len(preview.get("request_resolutions", ()))},
            ),
            explanation_factor(
                category=EXPLANATION_FACTOR_CATEGORY_INSUFFICIENT_EVIDENCE,
                key="per_offering_candidate_comparisons_not_captured",
                facts={},
            ),
        ),
        warnings=explanation_warning_items(preview.get("validation_errors", ())),
        available_actions=(
            {"code": "adjust_section_budget", "available": True},
            {"code": "approve_reviewed_section_budget", "available": bool(preview.get("can_approve"))},
            {"code": "create_new_section_budget_run", "available": True},
        ),
    )


def build_staffing_plan_review_summary(run, preview):
    """Summarize anonymous staffing feasibility without naming a teacher."""

    return build_review_summary(
        stage="staffing_plan",
        run_id=run.id,
        academic_year_id=run.academic_year_id,
        recommendation={
            "offering_count": len(preview.get("offerings", ())),
            "proposed_physical_section_total": preview.get("proposed_physical_section_total", 0),
            "linked_budget_total": preview.get("linked_budget_total"),
            "affected_student_count": run.result.get("affected_student_count", 0),
        },
        factors=(
            explanation_factor(
                category=EXPLANATION_FACTOR_CATEGORY_FEASIBILITY_WITNESS,
                key="anonymous_staffing_feasibility",
                facts={"named_teacher_assignments_returned": False},
            ),
            explanation_factor(
                category=EXPLANATION_FACTOR_CATEGORY_FIXED_CONTEXT,
                key="approved_budget_and_roster_context",
                facts={"has_linked_budget_approval": run.budget_approval_id is not None},
            ),
            explanation_factor(
                category=EXPLANATION_FACTOR_CATEGORY_INSUFFICIENT_EVIDENCE,
                key="per_offering_staffing_candidate_comparisons_not_captured",
                facts={},
            ),
        ),
        warnings=explanation_warning_items(
            run.result.get("diagnostics", ()), preview.get("validation_errors", ()), preview.get("conflicts", ()),
        ),
        available_actions=(
            {"code": "adjust_staffing_plan_counts", "available": True},
            {"code": "approve_reviewed_staffing_plan", "available": bool(preview.get("can_approve"))},
            {"code": "create_new_staffing_plan_run", "available": True},
        ),
    )


def build_section_placement_review_summary(run, preview):
    """Summarize timing facts while keeping the anonymous witness anonymous."""

    staffing_summary = preview.get("staffing_summary", {})
    assignments = preview.get("assignments", ())
    online_session_count = sum(
        item.get("online_supervision_session_id") is not None for item in assignments
    )
    return build_review_summary(
        stage="section_placement",
        run_id=run.id,
        academic_year_id=run.academic_year_id,
        recommendation={
            "timing_assignment_count": preview.get("assignment_count", 0),
            "online_supervision_session_count": online_session_count,
            "objective_components": run.result.get("objective_components", {}),
        },
        factors=(
            explanation_factor(
                category=EXPLANATION_FACTOR_CATEGORY_FEASIBILITY_WITNESS,
                key="anonymous_staffing_witness",
                facts={
                    "witness_proven": staffing_summary.get("witness_proven", False),
                    "confirmed_teacher_count": staffing_summary.get("confirmed_teacher_count", 0),
                    "teacher_names_or_assignments_returned": False,
                },
            ),
            explanation_factor(
                category=EXPLANATION_FACTOR_CATEGORY_FIXED_CONTEXT,
                key="accepted_timing_context",
                facts={"rooms_included": False, "teacher_assignments_included": False},
            ),
            explanation_factor(
                category=EXPLANATION_FACTOR_CATEGORY_INSUFFICIENT_EVIDENCE,
                key="per_unit_timing_candidate_comparisons_not_captured",
                facts={},
            ),
        ),
        trade_offs=[
            explanation_factor(
                category=EXPLANATION_FACTOR_CATEGORY_SOFT_OBJECTIVE,
                key="placement_objective_components",
                facts=run.result.get("objective_components", {}),
            ),
        ],
        warnings=explanation_warning_items(preview.get("diagnostics", ())),
        available_actions=(
            {"code": "approve_reviewed_section_placement", "available": bool(preview.get("approval_allowed"))},
            {"code": "create_new_section_placement_run", "available": True},
        ),
    )


def build_teacher_assignment_review_summary(run, preview):
    """Summarize named staffing facts and recorded candidate evidence."""

    assignments = preview.get("assignments", ())
    candidate_ledger = preview.get("candidate_ledger", ())
    online_supervision_assignment_count = sum(
        item.get("online_supervision_session_id") is not None for item in assignments
    )
    half_pair_keys = {
        item.get("shared_staffing_key")
        for item in run.input_snapshot.get("sections", ())
        if item.get("shared_staffing_key")
    }
    return build_review_summary(
        stage="named_teacher_assignment",
        run_id=run.id,
        academic_year_id=run.academic_year_id,
        recommendation={
            "named_assignment_count": preview.get("assignment_count", 0),
            "online_supervision_assignment_count": online_supervision_assignment_count,
            "shared_half_semester_teacher_pair_count": len(half_pair_keys),
            "objective_components": preview.get("objective_components", {}),
        },
        factors=(
            explanation_factor(
                category=EXPLANATION_FACTOR_CATEGORY_HARD_CONSTRAINT,
                key="accepted_timing_is_fixed_context",
                facts={"timeslots_changed_by_this_stage": False},
            ),
            explanation_factor(
                category=EXPLANATION_FACTOR_CATEGORY_HARD_CONSTRAINT,
                key="online_supervision_qualification_exception",
                facts={
                    "online_supervision_assignment_count": online_supervision_assignment_count,
                    "course_specific_qualification_required": False,
                    "availability_and_workload_still_required": True,
                },
            ),
            explanation_factor(
                category=EXPLANATION_FACTOR_CATEGORY_FIXED_CONTEXT,
                key="shared_half_semester_teacher_workload",
                facts={"shared_teacher_pair_count": len(half_pair_keys)},
            ),
            explanation_factor(
                category=EXPLANATION_FACTOR_CATEGORY_HARD_CONSTRAINT,
                key="teacher_candidate_elimination_ledger",
                facts={
                    "staffing_decision_count": len(candidate_ledger),
                    "roster_candidate_count": sum(
                        len(item.get("candidates", ())) for item in candidate_ledger
                    ),
                    "possible_in_isolation_count": sum(
                        candidate.get("comparison_state")
                        == "possible_in_isolation_global_comparison_not_yet_proven"
                        for item in candidate_ledger
                        for candidate in item.get("candidates", ())
                    ),
                },
            ),
        ),
        alternatives=(
            {
                "key": "teacher_assignment_candidate_ledger",
                "facts": {
                    "available": bool(candidate_ledger),
                    "online_supervision_decision_count": sum(
                        item.get("decision_kind") == "online_supervision"
                        for item in candidate_ledger
                    ),
                    "half_semester_pair_decision_count": sum(
                        item.get("decision_kind") == "half_semester_pair"
                        for item in candidate_ledger
                    ),
                    "possible_in_isolation_wording": (
                        "possible in isolation; global comparison not yet proven"
                    ),
                },
            },
        ),
        trade_offs=[
            explanation_factor(
                category=EXPLANATION_FACTOR_CATEGORY_SOFT_OBJECTIVE,
                key="teacher_assignment_objective_components",
                facts=preview.get("objective_components", {}),
            ),
        ],
        warnings=explanation_warning_items(preview.get("diagnostics", ())),
        available_actions=(
            {"code": "approve_reviewed_teacher_assignment", "available": bool(preview.get("approval_allowed"))},
            {"code": "create_new_teacher_assignment_run", "available": True},
        ),
    )


def build_student_assignment_review_summary(run, data, review):
    """Summarize existing student and special-commitment review facts."""

    assignments = review.get("assignments", ())
    commitments = review.get("commitment_assignments", ())
    candidate_ledger = review.get("candidate_ledger", ())
    commitment_counts = Counter(item.get("commitment_kind") for item in commitments)
    special_review_counts = Counter(
        item.get("code") for item in review.get("special_commitment_review_items", ())
    )
    focus_student_ids = {
        item.student_id
        for item in data.schedule_commitment_requests
        if item.commitment_type == "focus"
    } | {
        item.student_id
        for item in data.fixed_schedule_commitments
        if item.is_active and not item.is_historical and item.commitment_kind == "focus"
    }
    online_assignment_count = sum(
        item.get("online_supervision_session_id") is not None for item in assignments
    )
    half_semester_assignment_count = sum(
        item.get("half_semester_segment") is not None for item in assignments
    )
    return build_review_summary(
        stage="student_assignment",
        run_id=run.id,
        academic_year_id=run.academic_year_id,
        recommendation={
            "assignment_count": review.get("assignment_count", 0),
            "unmet_request_count": len(review.get("unmet_requests", ())),
            "study_commitment_count": commitment_counts["study"],
            "focus_commitment_count": commitment_counts["focus"],
            "co_op_commitment_count": commitment_counts["co_op"],
            "online_supervision_assignment_count": online_assignment_count,
            "half_semester_assignment_count": half_semester_assignment_count,
            "objective_components": review.get("objective_components", {}),
            "optimization_facts": review.get("optimization_facts", {}),
        },
        factors=(
            explanation_factor(
                category=EXPLANATION_FACTOR_CATEGORY_FIXED_CONTEXT,
                key="accepted_sections_timeslots_and_staffing_context",
                facts={
                    "staffing_mode": run.staffing_mode,
                    "section_count": len(data.sections),
                    "student_assignment_changes_sections_or_teachers": False,
                },
            ),
            explanation_factor(
                category=EXPLANATION_FACTOR_CATEGORY_FIXED_CONTEXT,
                key="special_commitment_representation",
                facts={
                    "study_commitment_count": commitment_counts["study"],
                    "focus_commitment_count": commitment_counts["focus"],
                    "co_op_commitment_count": commitment_counts["co_op"],
                    "online_supervision_assignment_count": online_assignment_count,
                    "half_semester_assignment_count": half_semester_assignment_count,
                },
            ),
            explanation_factor(
                category=EXPLANATION_FACTOR_CATEGORY_FIXED_CONTEXT,
                key="focus_semester_comparison_not_applicable",
                facts={
                    "focus_student_count": len(focus_student_ids),
                    "excluded_from_cross_semester_difficulty_and_category_comparisons": True,
                },
            ),
            explanation_factor(
                category=EXPLANATION_FACTOR_CATEGORY_REVIEW_CONDITION,
                key="special_commitment_review_conditions",
                facts={"review_item_counts_by_code": dict(sorted(special_review_counts.items()))},
            ),
            explanation_factor(
                category=EXPLANATION_FACTOR_CATEGORY_HARD_CONSTRAINT,
                key="bounded_student_candidate_elimination_ledger",
                facts={
                    "request_count": len(candidate_ledger),
                    "recorded_rejected_candidate_count": sum(
                        item.get("recorded_rejected_candidate_count", 0)
                        for item in candidate_ledger
                    ),
                    "omitted_rejected_candidate_count": sum(
                        item.get("omitted_rejected_candidate_count", 0)
                        for item in candidate_ledger
                    ),
                },
            ),
            explanation_factor(
                category=EXPLANATION_FACTOR_CATEGORY_SOFT_OBJECTIVE,
                key="student_assignment_two_stage_quality",
                facts=review.get("optimization_facts", {}),
            ),
        ),
        alternatives=(
            {
                "key": "student_assignment_candidate_ledger",
                "facts": {
                    "available": bool(candidate_ledger),
                    "per_student_explanation_available": True,
                    "unresolved_request_count": sum(
                        item.get("selection_state") == "unresolved"
                        for item in candidate_ledger
                    ),
                },
            },
        ),
        trade_offs=[
            explanation_factor(
                category=EXPLANATION_FACTOR_CATEGORY_SOFT_OBJECTIVE,
                key="student_assignment_objective_components",
                facts=review.get("objective_components", {}),
            ),
        ],
        warnings=explanation_warning_items(
            review.get("diagnostics", ()), review.get("special_commitment_review_items", ()),
        ),
        available_actions=(
            {"code": "approve_reviewed_student_assignment", "available": bool(review.get("approval_allowed"))},
            {"code": "create_new_student_assignment_run", "available": True},
            {"code": "release_active_lock", "available": bool(data.student_assignment_locks or data.special_commitment_locks)},
        ),
    )


def build_student_assignment_lock_impacts(*, data, result, ordinary_lock_details, special_lock_details):
    """Return Level 1 lock facts only; this helper never re-solves a schedule."""

    timeslot_by_id = {item.id: item for item in data.timeslots}
    assignments_by_request = {
        int(item["request_id"]): item for item in result.get("assignments", ())
    }
    commitments_by_request = {
        int(item["request_id"]): item for item in result.get("commitment_assignments", ())
    }
    impacts = []

    ordinary_effects = {
        "exact_student_section": "requires_student_course_section",
        "whole_student_schedule": "preserves_existing_student_schedule",
        "section_roster_freeze": "preserves_existing_section_roster",
        "course_roster_freeze": "preserves_existing_course_roster",
        "student_group_same_section": "requires_group_same_section",
        "student_teacher_course": "requires_student_course_teacher_context",
    }
    for lock in data.student_assignment_locks:
        fixed_rows = [
            row for row in data.fixed_enrollments
            if lock.lock_id in row.lock_ids and row.is_active and not row.is_historical
        ]
        detail = ordinary_lock_details.get(lock.lock_id, {})
        affected_recommendations = [
            item for item in result.get("assignments", ())
            if (lock.student_id is None or int(item["student_id"]) == lock.student_id)
            and (lock.course_id is None or int(item["course_id"]) == lock.course_id)
            and (lock.section_id is None or item.get("section_id") == lock.section_id)
        ]
        impacts.append({
            "lock_id": lock.lock_id,
            "lock_record_type": "student_assignment_lock",
            "lock_type": lock.lock_type,
            "reason": detail.get("reason"),
            "factor": explanation_factor(
                category=EXPLANATION_FACTOR_CATEGORY_COUNSELOR_LOCK,
                key="student_assignment_lock_target",
                facts={"lock_type": lock.lock_type},
            ),
            "target": {
                "student_id": lock.student_id,
                "section_id": lock.section_id,
                "course_id": lock.course_id,
                "teacher_id": lock.teacher_id,
                "member_student_ids": list(lock.member_student_ids),
            },
            "direct_effect": ordinary_effects[lock.lock_type],
            "frozen_occupancy": [
                {
                    "enrollment_id": row.enrollment_id,
                    "student_id": row.student_id,
                    "section_id": row.section_id,
                    "timeslot_id": row.timeslot_id,
                    "semester": row.semester,
                    "half_semester_segment": row.half_semester_segment,
                }
                for row in fixed_rows
            ],
            "affected_recommendation_count": len(affected_recommendations),
        })

    def timeslot_fact(timeslot_id):
        timeslot = timeslot_by_id.get(timeslot_id)
        if timeslot is None:
            return {"timeslot_id": timeslot_id}
        return {
            "timeslot_id": timeslot.id,
            "semester": timeslot.semester,
            "block": timeslot.block,
        }

    for lock in data.special_commitment_locks:
        detail = special_lock_details.get(lock.lock_id, {})
        is_exact = lock.lock_mode == "exact"
        target_occupancy = []
        if lock.timeslot_id is not None:
            target_occupancy.append(timeslot_fact(lock.timeslot_id))
        elif lock.semester is not None:
            blocks = ("A", "B") if lock.co_op_block_pair == "a_b" else ("C", "D")
            target_occupancy.extend(
                timeslot_fact(item.id)
                for item in data.timeslots
                if item.semester == lock.semester and (
                    lock.co_op_block_pair is None or item.block in blocks
                )
            )
        request_id = lock.schedule_commitment_request_id or lock.course_request_id
        recommended = (
            commitments_by_request.get(lock.schedule_commitment_request_id)
            if lock.schedule_commitment_request_id is not None
            else assignments_by_request.get(lock.course_request_id)
        )
        impacts.append({
            "lock_id": lock.lock_id,
            "lock_record_type": "student_special_commitment_lock",
            "lock_type": lock.lock_type,
            "lock_mode": lock.lock_mode,
            "reason": detail.get("reason"),
            "factor": explanation_factor(
                category=EXPLANATION_FACTOR_CATEGORY_COUNSELOR_LOCK,
                key="special_commitment_lock_target",
                facts={"lock_type": lock.lock_type, "lock_mode": lock.lock_mode},
            ),
            "target": {
                "schedule_commitment_request_id": lock.schedule_commitment_request_id,
                "course_request_id": lock.course_request_id,
                "timeslot_id": lock.timeslot_id,
                "semester": lock.semester,
                "co_op_block_pair": lock.co_op_block_pair,
            },
            "direct_effect": "requires_target_occupancy" if is_exact else "excludes_target_occupancy",
            "target_occupancy": target_occupancy,
            "recommended_request_id": request_id,
            "recommended_assignment": recommended,
        })
    return sorted(impacts, key=lambda item: (item["lock_record_type"], item["lock_id"]))
