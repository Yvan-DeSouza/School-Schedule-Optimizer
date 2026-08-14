"""Contracts for factual, solver-free scheduling-review explanations."""

from types import SimpleNamespace

import pytest

from scheduling_engine.dto import (
    FixedEnrollmentDTO,
    StudentAssignmentLockDTO,
    StudentSpecialCommitmentLockDTO,
    TimeSlotDTO,
)

from backend.apps.scheduling.services.review_explanations import (
    EXPLANATION_FACTOR_CATEGORY_COUNSELOR_LOCK,
    EXPLANATION_SCHEMA_VERSION,
    build_review_summary,
    build_section_budget_review_summary,
    build_section_placement_review_summary,
    build_section_planning_review_summary,
    build_staffing_plan_review_summary,
    build_student_assignment_lock_impacts,
    build_student_assignment_review_summary,
    build_teacher_assignment_review_summary,
    explanation_factor,
)


def _student_input():
    return SimpleNamespace(
        sections=(SimpleNamespace(section_id=11),),
        student_assignment_locks=(
            StudentAssignmentLockDTO(
                lock_id=1,
                lock_type="exact_student_section",
                student_id=10,
                section_id=11,
                course_id=20,
            ),
        ),
        special_commitment_locks=(
            StudentSpecialCommitmentLockDTO(
                lock_id=2,
                lock_type="study_time",
                lock_mode="exact",
                schedule_commitment_request_id=101,
                timeslot_id=1,
            ),
            StudentSpecialCommitmentLockDTO(
                lock_id=3,
                lock_type="focus_semester",
                lock_mode="exclude",
                schedule_commitment_request_id=102,
                semester=2,
            ),
            StudentSpecialCommitmentLockDTO(
                lock_id=4,
                lock_type="co_op_time",
                lock_mode="exact",
                course_request_id=201,
                semester=1,
                co_op_block_pair="a_b",
            ),
            StudentSpecialCommitmentLockDTO(
                lock_id=5,
                lock_type="online_supervision_time",
                lock_mode="exclude",
                course_request_id=202,
                timeslot_id=4,
            ),
        ),
        fixed_enrollments=(
            FixedEnrollmentDTO(
                student_id=10,
                section_id=11,
                course_offering_id=30,
                course_id=20,
                semester=1,
                timeslot_id=1,
                enrollment_id=40,
                is_locked=True,
                lock_ids=(1,),
            ),
        ),
        timeslots=(
            TimeSlotDTO(id=1, academic_year_id=7, semester=1, block="A"),
            TimeSlotDTO(id=2, academic_year_id=7, semester=1, block="B"),
            TimeSlotDTO(id=3, academic_year_id=7, semester=1, block="C"),
            TimeSlotDTO(id=4, academic_year_id=7, semester=1, block="D"),
            TimeSlotDTO(id=5, academic_year_id=7, semester=2, block="A"),
            TimeSlotDTO(id=6, academic_year_id=7, semester=2, block="B"),
            TimeSlotDTO(id=7, academic_year_id=7, semester=2, block="C"),
            TimeSlotDTO(id=8, academic_year_id=7, semester=2, block="D"),
        ),
        schedule_commitment_requests=(
            SimpleNamespace(student_id=12, commitment_type="focus"),
        ),
        fixed_schedule_commitments=(),
    )


def test_explanation_payload_uses_the_shared_versioned_vocabulary():
    """Clients receive all common fields even when no alternatives exist yet."""

    payload = build_review_summary(
        stage="student_assignment",
        run_id=1,
        academic_year_id=7,
        recommendation={"assignment_count": 1},
        factors=(explanation_factor(
            category=EXPLANATION_FACTOR_CATEGORY_COUNSELOR_LOCK,
            key="test_lock",
            facts={"lock_id": 1},
        ),),
    )

    assert payload["explanation_schema_version"] == EXPLANATION_SCHEMA_VERSION
    assert set(payload) == {
        "explanation_schema_version", "decision", "recommendation", "factors",
        "alternatives", "trade_offs", "warnings", "available_actions",
    }
    assert payload["alternatives"] == []
    assert payload["factors"][0]["category"] == "counselor_lock"


def test_level_one_lock_impacts_report_targets_and_existing_occupancy_without_solving():
    """Special and ordinary locks share factual impact data, not predictions."""

    data = _student_input()
    result = {
        "assignments": [
            {"request_id": 201, "student_id": 10, "course_id": 21, "section_id": None},
            {"request_id": 202, "student_id": 10, "course_id": 22, "online_supervision_session_id": 8},
        ],
        "commitment_assignments": [
            {"request_id": 101, "student_id": 10, "commitment_kind": "study"},
            {"request_id": 102, "student_id": 12, "commitment_kind": "focus"},
        ],
    }

    impacts = build_student_assignment_lock_impacts(
        data=data,
        result=result,
        ordinary_lock_details={1: {"reason": "Keep the reviewed class."}},
        special_lock_details={
            2: {"reason": "Keep Study in A."},
            3: {"reason": "Focus cannot use Semester 2."},
            4: {"reason": "Employer requires A+B."},
            5: {"reason": "Avoid a conflicting online block."},
        },
    )

    by_id = {item["lock_id"]: item for item in impacts}
    assert by_id[1]["frozen_occupancy"] == [{
        "enrollment_id": 40,
        "student_id": 10,
        "section_id": 11,
        "timeslot_id": 1,
        "semester": 1,
        "half_semester_segment": None,
    }]
    assert by_id[2]["direct_effect"] == "requires_target_occupancy"
    assert by_id[2]["target_occupancy"] == [{"timeslot_id": 1, "semester": 1, "block": "A"}]
    assert by_id[3]["direct_effect"] == "excludes_target_occupancy"
    assert {item["timeslot_id"] for item in by_id[3]["target_occupancy"]} == {5, 6, 7, 8}
    assert [item["block"] for item in by_id[4]["target_occupancy"]] == ["A", "B"]
    assert by_id[5]["target_occupancy"] == [{"timeslot_id": 4, "semester": 1, "block": "D"}]
    assert all(item["factor"]["category"] == "counselor_lock" for item in impacts)


def test_student_summary_labels_focus_as_not_comparable_instead_of_zero_load():
    """Focus removes a local semester from comparison; it is not an easy term."""

    data = _student_input()
    run = SimpleNamespace(id=3, academic_year_id=7, staffing_mode="final_staffing")
    review = {
        "assignment_count": 2,
        "assignments": [
            {"request_id": 201, "student_id": 10, "online_supervision_session_id": None, "half_semester_segment": None},
            {"request_id": 202, "student_id": 10, "online_supervision_session_id": 8, "half_semester_segment": "first_half"},
        ],
        "commitment_assignments": [
            {"request_id": 101, "student_id": 10, "commitment_kind": "study"},
            {"request_id": 102, "student_id": 12, "commitment_kind": "focus"},
            {"request_id": 201, "student_id": 10, "commitment_kind": "co_op"},
        ],
        "unmet_requests": [],
        "special_commitment_review_items": [
            {"code": "student_assignment_unallocated_school_time", "student_id": 10},
        ],
        "candidate_ledger": [
            {
                "request_id": 201,
                "selection_state": "selected",
                "recorded_rejected_candidate_count": 2,
                "omitted_rejected_candidate_count": 1,
            },
            {
                "request_id": 202,
                "selection_state": "unresolved",
                "recorded_rejected_candidate_count": 1,
                "omitted_rejected_candidate_count": 0,
            },
        ],
        "objective_components": {"mandatory_fulfillment": 2},
        "diagnostics": [],
        "approval_allowed": True,
    }

    summary = build_student_assignment_review_summary(run, data, review)
    focus_factor = next(
        item for item in summary["factors"]
        if item["key"] == "focus_semester_comparison_not_applicable"
    )

    assert focus_factor["facts"] == {
        "focus_student_count": 1,
        "excluded_from_cross_semester_difficulty_and_category_comparisons": True,
    }
    assert summary["recommendation"]["online_supervision_assignment_count"] == 1
    candidate_factor = next(
        item for item in summary["factors"]
        if item["key"] == "bounded_student_candidate_elimination_ledger"
    )
    assert candidate_factor["facts"] == {
        "request_count": 2,
        "recorded_rejected_candidate_count": 3,
        "omitted_rejected_candidate_count": 1,
    }
    assert summary["alternatives"] == [{
        "key": "student_assignment_candidate_ledger",
        "facts": {
            "available": True,
            "per_student_explanation_available": True,
            "unresolved_request_count": 1,
        },
    }]
    assert summary["warnings"] == [{
        "code": "student_assignment_unallocated_school_time",
        "facts": {"student_id": 10},
    }]


def test_other_stage_summaries_expose_existing_facts_in_the_same_envelope():
    """Section, placement, and staffing reviews share the public shape."""

    planning_run = SimpleNamespace(
        id=1,
        academic_year_id=7,
        result={"courses": [{"course_id": 20, "staffing_feasible_annual_count": 2}]},
    )
    planning = build_section_planning_review_summary(planning_run, {
        "courses": [{
            "recommended_annual_count": 2,
            "predicted_enrollment": 36,
            "unmet_demand": 0,
        }],
        "proposed_section_count": 2,
        "can_approve": True,
        "conflicts": [],
        "validation_errors": [],
    })
    budget = build_section_budget_review_summary(SimpleNamespace(
        id=2, academic_year_id=7, budget_type="ceiling", section_budget=10,
    ), {
        "approved_total": 8,
        "offerings": [{"offering_id": 1}],
        "affected_student_count": 0,
        "request_resolutions": [],
        "validation_errors": [],
        "can_approve": True,
    })
    staffing = build_staffing_plan_review_summary(SimpleNamespace(
        id=3, academic_year_id=7, budget_approval_id=4,
        result={"affected_student_count": 2, "diagnostics": []},
    ), {
        "offerings": [{"offering_id": 1}],
        "proposed_physical_section_total": 8,
        "linked_budget_total": 8,
        "validation_errors": [],
        "conflicts": [],
        "can_approve": True,
    })
    placement = build_section_placement_review_summary(SimpleNamespace(
        id=4, academic_year_id=7,
        result={"objective_components": {"placed_units": 8}},
    ), {
        "assignment_count": 8,
        "assignments": [{"online_supervision_session_id": 9}],
        "diagnostics": [],
        "staffing_summary": {"witness_proven": True, "confirmed_teacher_count": 4},
        "approval_allowed": True,
    })
    teacher = build_teacher_assignment_review_summary(SimpleNamespace(
        id=5,
        academic_year_id=7,
        input_snapshot={"sections": [{"shared_staffing_key": "pair-1"}]},
    ), {
        "assignment_count": 8,
        "assignments": [{"online_supervision_session_id": 9}],
        "diagnostics": [],
        "objective_components": {"preferred_course_match": 3},
        "approval_allowed": True,
    })

    for summary, stage in (
        (planning, "section_count"),
        (budget, "section_budget"),
        (staffing, "staffing_plan"),
        (placement, "section_placement"),
        (teacher, "named_teacher_assignment"),
    ):
        assert summary["explanation_schema_version"] == 1
        assert summary["decision"]["stage"] == stage
        assert isinstance(summary["alternatives"], list)
        assert isinstance(summary["warnings"], list)

    witness = next(item for item in placement["factors"] if item["key"] == "anonymous_staffing_witness")
    assert witness["facts"]["teacher_names_or_assignments_returned"] is False
    online = next(item for item in teacher["factors"] if item["key"] == "online_supervision_qualification_exception")
    assert online["facts"]["course_specific_qualification_required"] is False


def test_unknown_factor_categories_fail_closed():
    with pytest.raises(ValueError, match="Unknown explanation factor category"):
        explanation_factor(category="guessed_reason", key="invalid", facts={})
