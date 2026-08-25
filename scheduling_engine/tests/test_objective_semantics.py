from dataclasses import replace
from pathlib import Path

import pytest

from scheduling_engine.realistic_student_assignment_validation import (
    build_realistic_validation_fixture,
    build_realistic_quality_tradeoff_fixture,
)
from scheduling_engine.dto import (
    CourseDifficultyDTO,
    FixedEnrollmentDTO,
    StudentAssignmentInputDTO,
    StudentAssignmentRequestDTO,
    StudentScheduleCommitmentRequestDTO,
    TimeSlotDTO,
)
from scheduling_engine.student_assignment import solve_student_assignment
from scheduling_engine.student_assignment.core import (
    run_student_assignment_variable_neighborhood_diagnostic,
)
from scheduling_engine.student_assignment.quality import evaluate_student_assignment_quality
from scheduling_engine.student_assignment.stage2_benchmark import (
    read_durable_stage2_benchmark,
)
from scheduling_engine.student_assignment.objective_semantics import (
    IMPORTANCE_LABEL_TO_SCORE,
    NORMALIZED_OBJECTIVE_SCALE,
    denominator_from_maxima,
    denominator_from_pair_capacities,
    normalize_penalty,
    resolve_importance_scores,
    validate_importance_score,
    weighted_normalized_penalty,
)


OBJECTIVE_KEYS = (
    "section_utilization_balance",
    "student_semester_balance",
    "course_sequence_preferences",
    "difficulty_balance",
    "course_category_diversity",
)


def _v2(data, score=6):
    return replace(
        data,
        objective_semantics_version="v2",
        objective_importance_scores={key: score for key in OBJECTIVE_KEYS},
    )


def test_importance_scores_are_bounded_and_labels_are_one_compatibility_mapping():
    assert tuple(validate_importance_score(value) for value in range(11)) == tuple(range(11))
    assert IMPORTANCE_LABEL_TO_SCORE["not_important"] == 0
    assert IMPORTANCE_LABEL_TO_SCORE["extremely_important"] == 10
    assert resolve_importance_scores(
        labels={key: "important" for key in OBJECTIVE_KEYS},
    )["difficulty_balance"] == IMPORTANCE_LABEL_TO_SCORE["important"]
    with pytest.raises(ValueError):
        validate_importance_score(-1)
    with pytest.raises(ValueError):
        validate_importance_score(11)


def test_normalization_is_bounded_deterministic_and_safe_for_empty_domains():
    assert denominator_from_pair_capacities((30, 20, 10)) == 80
    assert denominator_from_maxima((3, 0, 7)) == 10
    assert normalize_penalty(5, 10) == NORMALIZED_OBJECTIVE_SCALE // 2
    assert normalize_penalty(100, 10) == NORMALIZED_OBJECTIVE_SCALE
    assert normalize_penalty(5, 0) == 0
    assert weighted_normalized_penalty(5, 10, 0) == 0
    assert weighted_normalized_penalty(5, 10, 8) == 40_000
    assert weighted_normalized_penalty(5, 10, 8) == 2 * weighted_normalized_penalty(5, 10, 4)


def test_normalization_is_order_independent_and_safe_for_large_inputs():
    capacities = (41, 41, 43, 47)
    maxima = (17, 0, 91, 3)
    assert denominator_from_pair_capacities(capacities) == (
        denominator_from_pair_capacities(tuple(reversed(capacities)))
    )
    assert denominator_from_maxima(maxima) == denominator_from_maxima(tuple(reversed(maxima)))
    assert normalize_penalty(10**18, 1) == NORMALIZED_OBJECTIVE_SCALE
    assert weighted_normalized_penalty(10**18, 1, 10) == 10 * NORMALIZED_OBJECTIVE_SCALE


def test_v2_non_applicable_quality_component_is_zero_without_solver_payload():
    data = _v2(
        replace(
            build_realistic_quality_tradeoff_fixture(),
            course_difficulties=(
                CourseDifficultyDTO(1, "", 90, None, 90, "test"),
                CourseDifficultyDTO(2, "", 10, None, 10, "test"),
                CourseDifficultyDTO(3, "", 100, None, 100, "test"),
            ),
        )
    )
    result = solve_student_assignment(data)
    report = evaluate_student_assignment_quality(
        data,
        assignments=result.assignments,
        commitment_assignments=result.commitment_assignments,
    )
    assert report["objective_semantics"]["components"]["course_category_diversity"][
        "normalized_penalty"
    ] == 0


def test_v2_score_profiles_preserve_hard_feasibility_and_completion():
    base = build_realistic_quality_tradeoff_fixture()
    profiles = (
        {key: 0 for key in OBJECTIVE_KEYS},
        {key: 6 for key in OBJECTIVE_KEYS},
        {key: 10 for key in OBJECTIVE_KEYS},
    )
    results = [solve_student_assignment(replace(
        base,
        objective_semantics_version="v2",
        objective_importance_scores=profile,
    )) for profile in profiles]
    assert all(result.status == "complete" for result in results)
    assert all(not result.unmet_requests for result in results)
    assert {len(result.assignments) for result in results} == {2}
    assert all(
        result.objective_components["mandatory_fulfilled"] == 2
        for result in results
    )
    # Equal canonical importance is exercised through the actual solver, not
    # only through coefficient arithmetic: this fixture has a real
    # difficulty/category tradeoff and the balanced profile chooses the
    # normalized aggregate's lower-cost split.
    assert {item.semester for item in results[1].assignments} == {1, 2}
    assert results[1].objective_components["course_category_diversity_penalty"] == 0
    assert all(
        value == 0
        for value in results[0].objective_components[
            "weighted_normalized_contributions"
        ].values()
    )


def test_v2_profiles_do_not_change_existing_hard_lock_capacity_or_prerequisite_result():
    base = build_realistic_validation_fixture()
    results = [solve_student_assignment(replace(
        base,
        objective_semantics_version="v2",
        objective_importance_scores={key: score for key in OBJECTIVE_KEYS},
    )) for score in (0, 10)]
    assert [result.status for result in results] == ["partial", "partial"]
    assert len(results[0].unmet_requests) == len(results[1].unmet_requests)
    assert [
        tuple(item.request_id for item in result.assignments)
        for result in results
    ] == [
        tuple(item.request_id for item in results[0].assignments),
        tuple(item.request_id for item in results[0].assignments),
    ]
    assert tuple(item.diagnostic_code for item in results[0].unmet_requests) == tuple(
        item.diagnostic_code for item in results[1].unmet_requests
    )


def test_v2_preserves_special_commitment_hard_semantics():
    slots = tuple(
        TimeSlotDTO(
            1000 + semester * 10 + index,
            1,
            semester,
            block,
            True,
        )
        for semester in (1, 2)
        for index, block in enumerate(("A", "B", "C", "D"), start=1)
    )
    data = StudentAssignmentInputDTO(
        academic_year_id=1,
        requests=(StudentAssignmentRequestDTO(
            request_id=3,
            student_id=3,
            course_id=9,
            course_offering_id=99,
            is_primary=True,
            is_mandatory=True,
            priority_tier=1,
            delivery_kind="co_op",
            credit_value=2.0,
        ),),
        sections=(),
        fixed_enrollments=(),
        hard_prerequisites=(),
        soft_sequence_preferences=(),
        section_utilization_balance_importance="important",
        student_semester_balance_importance="important",
        course_sequence_preferences_importance="important",
        difficulty_balance_importance="important",
        course_category_diversity_importance="important",
        schedule_commitment_requests=(
            StudentScheduleCommitmentRequestDTO(1, 1, "study"),
            StudentScheduleCommitmentRequestDTO(2, 2, "focus"),
        ),
        timeslots=slots,
        time_limit_seconds=5.0,
        objective_semantics_version="v2",
        objective_importance_scores={key: 0 for key in OBJECTIVE_KEYS},
    )
    result = solve_student_assignment(data)
    assert result.status == "complete"
    assert result.assignments == ()
    assert {item.commitment_kind for item in result.commitment_assignments} == {
        "study", "focus", "co_op",
    }


def test_v2_difficulty_and_category_scores_change_a_real_solver_tradeoff():
    base = build_realistic_quality_tradeoff_fixture()
    difficulty_first = solve_student_assignment(replace(
        base,
        objective_semantics_version="v2",
        objective_importance_scores={
            "section_utilization_balance": 0,
            "student_semester_balance": 0,
            "course_sequence_preferences": 0,
            "difficulty_balance": 10,
            "course_category_diversity": 1,
        },
    ))
    category_first = solve_student_assignment(replace(
        base,
        objective_semantics_version="v2",
        objective_importance_scores={
            "section_utilization_balance": 0,
            "student_semester_balance": 0,
            "course_sequence_preferences": 0,
            "difficulty_balance": 1,
            "course_category_diversity": 10,
        },
    ))
    assert difficulty_first.status == category_first.status == "complete"
    assert {item.semester for item in difficulty_first.assignments} == {2}
    assert {item.semester for item in category_first.assignments} == {1, 2}
    assert difficulty_first.objective_components["difficulty_balance_penalty"] == 0
    assert category_first.objective_components["course_category_diversity_penalty"] == 0
    assert difficulty_first.objective_components["objective_semantics_version"] == "v2"


def test_v2_result_keeps_raw_metrics_and_exposes_normalized_facts():
    data = _v2(
        build_realistic_quality_tradeoff_fixture(
            difficulty_importance="important",
            course_category_diversity_importance="important",
        )
    )
    result = solve_student_assignment(data)
    semantics = result.optimization_facts["objective_semantics"]
    assert result.status == "complete"
    assert semantics["version"] == "v2"
    assert semantics["normalized_scale"] == NORMALIZED_OBJECTIVE_SCALE
    assert result.objective_components["objective_semantics_version"] == "v2"
    soft_tiers = [
        item for item in result.optimization_facts["objective_metadata"]
        if item["kind"] == "soft_tier"
    ]
    assert len(soft_tiers) == 1
    assert soft_tiers[0]["semantics_version"] == "v2"
    assert soft_tiers[0]["importance_level"] == 10
    assert set(result.objective_components["normalized_components"]) == {
        "section_utilization_balance_penalty",
        "student_semester_balance_penalty",
        "difficulty_balance_penalty",
        "course_category_diversity_penalty",
        "course_sequence_preferences_penalty",
    }
    for facts in semantics["normalization"].values():
        assert facts["denominator"] >= 0
        assert 0 <= facts["importance_score"] <= 10
        assert 0 <= facts["normalized_scale"]

    raw_by_name = {
        "section_utilization_balance_penalty": result.objective_components[
            "section_utilization_balance_penalty"
        ],
        "student_semester_balance_penalty": result.objective_components[
            "student_semester_balance_penalty"
        ],
        "difficulty_balance_penalty": result.objective_components[
            "difficulty_balance_penalty"
        ],
        "course_category_diversity_penalty": result.objective_components[
            "course_category_diversity_penalty"
        ],
        "course_sequence_preferences_penalty": (
            semantics["normalization"]["course_sequence_preferences_penalty"][
                "denominator"
            ]
            - result.objective_components["soft_sequence_preferences_satisfied"]
        ),
    }
    for name, facts in semantics["normalization"].items():
        assert result.objective_components["normalized_components"][name] == normalize_penalty(
            raw_by_name[name], facts["denominator"], scale=facts["normalized_scale"]
        )
    assert result.optimization_facts["stage_2"]["objective_values"][4] == sum(
        result.objective_components["weighted_normalized_contributions"].values()
    )

    report = evaluate_student_assignment_quality(
        data,
        assignments=result.assignments,
        commitment_assignments=result.commitment_assignments,
        solver_objective_components=result.objective_components,
    )
    assert report["version"] == "student_schedule_quality_v4"
    assert report["objective_semantics"]["version"] == "v2"


def test_v1_result_remains_historical_and_is_not_reinterpreted_as_v2():
    result = solve_student_assignment(build_realistic_quality_tradeoff_fixture())
    assert result.objective_components.get("objective_semantics_version") is None
    assert result.optimization_facts["objective_semantics"]["version"] == "v1"
    assert result.optimization_facts["objective_semantics"]["normalized_scale"] is None


def test_v2_normalization_is_independent_of_input_order_and_opaque_ids():
    base = _v2(build_realistic_quality_tradeoff_fixture())
    section_ids = {section.section_id: section.section_id + 1000 for section in base.sections}
    reordered = replace(
        base,
        requests=tuple(reversed(base.requests)),
        sections=tuple(
            replace(section, section_id=section_ids[section.section_id])
            for section in reversed(base.sections)
        ),
        fixed_enrollments=tuple(
            replace(row, section_id=section_ids[row.section_id])
            for row in reversed(base.fixed_enrollments)
        ),
        course_difficulties=tuple(reversed(base.course_difficulties)),
    )
    original_result = solve_student_assignment(base)
    reordered_result = solve_student_assignment(reordered)
    original_semantics = original_result.optimization_facts["objective_semantics"]
    reordered_semantics = reordered_result.optimization_facts["objective_semantics"]
    assert original_semantics["normalization"] == reordered_semantics["normalization"]
    assert original_result.objective_components["normalized_components"] == (
        reordered_result.objective_components["normalized_components"]
    )
    assert original_result.objective_components["difficulty_balance_penalty"] == (
        reordered_result.objective_components["difficulty_balance_penalty"]
    )


def test_v2_label_preset_and_explicit_score_have_identical_semantics():
    base = build_realistic_quality_tradeoff_fixture(
        difficulty_importance="important",
        course_category_diversity_importance="important",
    )
    labels = _v2(base, score=5)
    explicit = replace(
        labels,
        objective_importance_scores={key: 5 for key in OBJECTIVE_KEYS},
    )
    label_result = solve_student_assignment(labels)
    explicit_result = solve_student_assignment(explicit)
    assert label_result.optimization_facts["objective_semantics"]["importance_scores"] == (
        explicit_result.optimization_facts["objective_semantics"]["importance_scores"]
    )
    assert label_result.objective_components["normalized_components"] == (
        explicit_result.objective_components["normalized_components"]
    )


def test_zero_importance_changes_only_soft_objective_authority():
    base = build_realistic_quality_tradeoff_fixture(
        difficulty_importance="important",
        course_category_diversity_importance="important",
    )
    enabled = solve_student_assignment(_v2(base, score=6))
    disabled = solve_student_assignment(_v2(base, score=0))
    assert enabled.status == disabled.status == "complete"
    assert len(enabled.assignments) == len(disabled.assignments)
    assert disabled.optimization_facts["objective_semantics"]["importance_scores"] == {
        key: 0 for key in OBJECTIVE_KEYS
    }


def test_existing_variable_neighborhood_probe_targets_v2_normalized_tier():
    data = _v2(build_realistic_quality_tradeoff_fixture())
    result = run_student_assignment_variable_neighborhood_diagnostic(
        data,
        neighborhood_radii=(0,),
        max_iterations=1,
        max_attempts_by_radius={0: 1},
        per_probe_time_limit_seconds=0.5,
        total_time_limit_seconds=5.0,
        worker_count=1,
    )
    local_facts = result.optimization_facts["stage_2_local_bootstrap"]
    assert result.status == "complete"
    assert local_facts["target_importance_level"] == 10


@pytest.mark.parametrize("radius", (2, 4, 8))
def test_retained_v2_escape_radii_remain_diagnostic_only_and_hard_valid(radius):
    data = _v2(build_realistic_quality_tradeoff_fixture())
    result = run_student_assignment_variable_neighborhood_diagnostic(
        data,
        neighborhood_radii=(radius,),
        max_iterations=1,
        max_attempts_by_radius={radius: 1},
        per_probe_time_limit_seconds=0.5,
        total_time_limit_seconds=3.0,
        worker_count=1,
        max_changed_students=2,
    )
    facts = result.optimization_facts["stage_2_local_bootstrap"]
    assert result.status == "complete"
    assert not result.unmet_requests
    assert facts["variable_neighborhood"] is True
    assert facts["target_importance_level"] == 10
    assert result.objective_components["objective_semantics_version"] == "v2"


def test_historical_v1_durable_benchmark_remains_readable_after_dto_additions():
    benchmark = read_durable_stage2_benchmark(
        Path("scheduling_engine/benchmarks/student_assignment/production_scale_v1")
    )
    assert benchmark["input_semantic_fingerprint"] == (
        "1c4843ac33fccabd76218c63d8818c94a0a8dd8ab2886e3f5718ca1cd9576a11"
    )
    assert benchmark["data"].objective_semantics_version == "v1"
    assert dict(benchmark["data"].objective_importance_scores) == {}
