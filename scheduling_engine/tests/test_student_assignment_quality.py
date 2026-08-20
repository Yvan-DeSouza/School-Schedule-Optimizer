from dataclasses import replace

from scheduling_engine.dto import (
    CourseCategoryRelationshipDTO,
    StudentScheduleCommitmentAssignmentDTO,
    StudentScheduleCommitmentRequestDTO,
)
from scheduling_engine.realistic_student_assignment_validation import (
    build_realistic_quality_tradeoff_fixture,
    build_realistic_scoped_rerun_fixture,
)
from scheduling_engine.student_assignment import solve_student_assignment
from scheduling_engine.student_assignment.quality import (
    compare_student_assignment_quality,
    evaluate_student_assignment_quality,
)


def test_quality_reconstructs_solver_penalties_and_records_stage_comparison():
    data = build_realistic_quality_tradeoff_fixture(
        difficulty_importance="important",
        course_category_diversity_importance="important",
    )
    result = solve_student_assignment(data)
    report = evaluate_student_assignment_quality(
        data,
        assignments=result.assignments,
        commitment_assignments=result.commitment_assignments,
    )

    assert report["section_utilization_balance"]["solver_aligned_penalty"] == result.objective_components[
        "section_utilization_balance_penalty"
    ]
    assert report["student_semester_load_balance"]["solver_aligned_penalty"] == result.objective_components[
        "student_semester_balance_penalty"
    ]
    assert report["difficulty_balance"]["solver_aligned_penalty"] == result.objective_components[
        "difficulty_balance_penalty"
    ]
    assert report["course_category_diversity"]["solver_aligned_penalty"] == result.objective_components[
        "course_category_diversity_penalty"
    ]
    section = report["section_utilization_balance"]
    assert "average_section_deviation_distribution" in section
    assert "perfectly_balanced_group_count" in section
    assert "within_one_enrollment_group_percentage" in section
    assert result.optimization_facts["quality"]["stage_1"]
    assert result.optimization_facts["quality"]["stage_2"]
    assert len(result.optimization_facts["optimization_passes"]) >= 1


def test_quality_keeps_cp_sat_aggregate_authoritative_when_reconstruction_differs():
    data = build_realistic_quality_tradeoff_fixture(
        difficulty_importance="important",
        course_category_diversity_importance="not_important",
    )
    result = solve_student_assignment(data)
    report = evaluate_student_assignment_quality(
        data,
        assignments=result.assignments,
        commitment_assignments=result.commitment_assignments,
        solver_objective_components={
            "difficulty_balance_penalty": (
                result.objective_components["difficulty_balance_penalty"] + 1
            ),
        },
    )

    difficulty = report["difficulty_balance"]
    assert difficulty["solver_aligned_penalty"] == result.objective_components[
        "difficulty_balance_penalty"
    ] + 1
    assert difficulty["reconstructed_penalty"] == result.objective_components[
        "difficulty_balance_penalty"
    ]
    assert difficulty["reconstruction_delta"] == 1


def test_category_quality_uses_similarity_relationships_not_category_counts():
    data = build_realistic_quality_tradeoff_fixture(
        difficulty_importance="not_important",
        course_category_diversity_importance="important",
    )
    data = replace(
        data,
        course_category_relationships=(
            CourseCategoryRelationshipDTO("math", "science", 40),
        ),
    )
    result = solve_student_assignment(data)
    report = evaluate_student_assignment_quality(
        data,
        assignments=result.assignments,
        commitment_assignments=result.commitment_assignments,
    )

    assert report["course_category_diversity"]["solver_aligned_penalty"] == result.objective_components[
        "course_category_diversity_penalty"
    ]
    assert report["course_category_diversity"]["solver_aligned_penalty"] == 40


def test_category_quality_divides_after_combining_shared_halves():
    data = build_realistic_quality_tradeoff_fixture(
        difficulty_importance="not_important",
        course_category_diversity_importance="important",
    )
    data = replace(
        data,
        course_category_relationships=(
            CourseCategoryRelationshipDTO("math", "science", 25),
        ),
    )
    result = solve_student_assignment(data)
    report = evaluate_student_assignment_quality(
        data,
        assignments=result.assignments,
        commitment_assignments=result.commitment_assignments,
    )

    # Both academic courses share both halves in Semester 1.  The solver
    # computes floor(25 * 2 / 2), not floor(25 / 2) twice.
    assert report["course_category_diversity"]["solver_aligned_penalty"] == 25


def test_focus_students_are_excluded_from_semester_and_difficulty_distributions():
    data = build_realistic_quality_tradeoff_fixture(
        difficulty_importance="important",
        course_category_diversity_importance="not_important",
    )
    from scheduling_engine.dto import StudentScheduleCommitmentRequestDTO

    data = replace(
        data,
        schedule_commitment_requests=(
            StudentScheduleCommitmentRequestDTO(900, 1, "focus"),
        ),
    )
    result = solve_student_assignment(data)
    report = evaluate_student_assignment_quality(
        data,
        assignments=result.assignments,
        commitment_assignments=result.commitment_assignments,
    )

    assert report["student_semester_load_balance"]["distribution"]["count"] == 0
    assert report["difficulty_balance"]["distribution"]["count"] == 0


def test_special_commitment_fulfillment_counts_co_op_and_non_course_requests():
    data = build_realistic_quality_tradeoff_fixture(
        difficulty_importance="not_important",
        course_category_diversity_importance="not_important",
    )
    data = replace(
        data,
        requests=(replace(data.requests[0], delivery_kind="co_op"),),
        schedule_commitment_requests=(
            # Course-request and schedule-commitment IDs come from different
            # database sequences and may legitimately have the same value.
            StudentScheduleCommitmentRequestDTO(1, 1, "study"),
        ),
    )
    commitment_assignments = (
        StudentScheduleCommitmentAssignmentDTO(
            request_id=1,
            student_id=1,
            commitment_kind="study",
            course_request_id=None,
            course_offering_id=None,
            occupancy=(),
        ),
        StudentScheduleCommitmentAssignmentDTO(
            request_id=data.requests[0].request_id,
            student_id=1,
            commitment_kind="co_op",
            course_request_id=data.requests[0].request_id,
            course_offering_id=data.requests[0].course_offering_id,
            occupancy=(),
        ),
    )

    report = evaluate_student_assignment_quality(
        data,
        assignments=(),
        commitment_assignments=commitment_assignments,
    )

    assert report["request_fulfillment"]["special_commitments"] == {
        "requested_count": 2,
        "fulfilled_count": 2,
        "unmet_count": 0,
        "entities": {
            "commitment:1": {
                "commitment_kind": "study",
                "source_kind": "commitment",
                "source_request_id": 1,
                "fulfilled": 1,
            },
            "course:1": {
                "commitment_kind": "co_op",
                "source_kind": "course",
                "source_request_id": 1,
                "fulfilled": 1,
            },
        },
    }


def test_sequence_applicability_is_separate_from_satisfaction():
    data = build_realistic_quality_tradeoff_fixture(
        difficulty_importance="not_important",
        course_category_diversity_importance="not_important",
    )
    result = solve_student_assignment(data)
    report = evaluate_student_assignment_quality(
        data,
        assignments=result.assignments,
        commitment_assignments=result.commitment_assignments,
        sequence_opportunities=((1, 1, 2),),
    )

    assert report["course_sequence_preferences"]["eligible_opportunity_count"] == 1
    assert report["course_sequence_preferences"]["applicable_student_count"] == 1
    assert report["course_sequence_preferences"]["satisfied_opportunity_count"] in {0, 1}
    assert report["course_sequence_preferences"]["unsatisfied_opportunity_count"] + report[
        "course_sequence_preferences"
    ]["satisfied_opportunity_count"] == 1


def test_empty_preservation_is_not_reported_as_an_active_objective():
    data = build_realistic_quality_tradeoff_fixture(
        difficulty_importance="not_important",
        course_category_diversity_importance="not_important",
    )
    result = solve_student_assignment(data)
    report = evaluate_student_assignment_quality(
        data,
        assignments=result.assignments,
        commitment_assignments=result.commitment_assignments,
    )

    assert report["schedule_preservation"]["applicable"] is False
    assert report["schedule_preservation"]["solver_aligned_penalty"] == 0


def test_scoped_rerun_quality_reports_movable_and_preserved_context():
    data = build_realistic_scoped_rerun_fixture()
    result = solve_student_assignment(data)
    report = evaluate_student_assignment_quality(
        data,
        assignments=result.assignments,
        commitment_assignments=result.commitment_assignments,
    )

    preservation = report["schedule_preservation"]
    assert preservation["applicable"] is True
    assert preservation["movable_enrollment_count"] == 1
    assert preservation["preserved_enrollment_count"] == 1
    assert preservation["moved_enrollment_count"] == result.objective_components[
        "schedule_preservation_move_penalty"
    ]


def test_quality_comparison_reports_entity_level_tradeoffs():
    stage_1 = {
        "difficulty_balance": {"entities": {"1": {"absolute_difference": 20}}},
        "course_category_diversity": {"entities": {"1": 40}},
        "section_utilization_balance": {"entities": {"1": {"range": 3}}},
        "student_semester_load_balance": {"entities": {"1": {"absolute_difference": 2}}},
        "course_sequence_preferences": {"entities": {"1:1:2": 0}},
        "schedule_preservation": {"entities": {"1": 1}},
    }
    stage_2 = {
        "difficulty_balance": {"entities": {"1": {"absolute_difference": 0}}},
        "course_category_diversity": {"entities": {"1": 60}},
        "section_utilization_balance": {"entities": {"1": {"range": 3}}},
        "student_semester_load_balance": {"entities": {"1": {"absolute_difference": 4}}},
        "course_sequence_preferences": {"entities": {"1:1:2": 1}},
        "schedule_preservation": {"entities": {"1": 0}},
    }

    comparison = compare_student_assignment_quality(stage_1, stage_2)

    assert comparison["difficulty_balance"]["improved"] == 1
    assert comparison["difficulty_balance"]["change"]["mean_improvement"] == 20
    assert comparison["course_category_diversity"]["worsened"] == 1
    assert comparison["course_category_diversity"]["change"]["mean_worsening"] == 20
    assert comparison["course_sequence_preferences"]["improved"] == 1
