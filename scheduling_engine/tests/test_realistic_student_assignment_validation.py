"""Realistic-condition validation for the pure student-assignment contract."""

from collections import Counter, defaultdict

from scheduling_engine.realistic_student_assignment_validation import (
    build_production_shaped_medium_fixture,
    build_mixed_grade_v2_fixture,
    build_realistic_scale_fixture,
    build_realistic_scoped_rerun_fixture,
    build_realistic_quality_tradeoff_fixture,
    build_realistic_validation_fixture,
    summarize_realistic_fixture,
    summarize_production_shaped_medium_fixture,
)
from scheduling_engine.student_assignment import solve_student_assignment
from scheduling_engine.student_assignment.policy_generalization import (
    DEFAULT_POLICY_GENERALIZATION_SCENARIOS,
    POLICY_GENERALIZATION_POLICIES,
    POLICY_GENERALIZATION_PROFILE,
    POLICY_GENERALIZATION_RANDOM_SEED,
    POLICY_GENERALIZATION_WORKER_COUNT,
    TARGET_POLICY_GENERALIZATION_SCENARIOS,
    build_policy_generalization_suite,
    summarize_policy_generalization_scenario,
)


def test_realistic_fixture_preserves_hard_rules_and_explains_legitimate_gaps():
    """Uneven demand must not bypass capacity, locks, A-D, or prerequisites."""

    data = build_realistic_validation_fixture()
    result = solve_student_assignment(data)
    assignments_by_request = {item.request_id: item for item in result.assignments}
    unmet_by_request = {item.request_id: item for item in result.unmet_requests}
    sections = {item.section_id: item for item in data.sections}

    assert result.status == "partial"
    assert assignments_by_request[5].assignment_basis == "approved_backup"
    assert assignments_by_request[7].section_id == 7
    assert assignments_by_request[8].section_id == 2
    assert assignments_by_request[8].previous_section_id == 2

    # The prerequisite relationship is hard whenever both courses are in the
    # target year; the result puts the earlier course before its dependent.
    assert assignments_by_request[1].semester == 1
    assert assignments_by_request[2].semester == 2

    assert unmet_by_request[6].diagnostic_code == "student_assignment_no_active_placed_section"
    assert unmet_by_request[10].diagnostic_code == "student_assignment_section_capacity_exhausted"

    fixed_load_by_section = Counter(
        item.section_id
        for item in data.fixed_enrollments
        if item.is_active and not item.is_historical and item.is_locked
    )
    assigned_load_by_section = Counter(item.section_id for item in result.assignments)
    for section_id, section in sections.items():
        assert assigned_load_by_section[section_id] + fixed_load_by_section[section_id] <= section.capacity_max

    used_timeslots_by_student = defaultdict(set)
    for assignment in result.assignments:
        assert assignment.timeslot_id not in used_timeslots_by_student[assignment.student_id]
        used_timeslots_by_student[assignment.student_id].add(assignment.timeslot_id)


def test_realistic_scoped_rerun_keeps_protected_context_and_respects_preservation():
    """A changed student can rerun without reopening another student's schedule."""

    flexible = solve_student_assignment(
        build_realistic_scoped_rerun_fixture(schedule_preservation_level="none")
    )
    preserved = solve_student_assignment(
        build_realistic_scoped_rerun_fixture(schedule_preservation_level="strong")
    )
    flexible_by_request = {item.request_id: item for item in flexible.assignments}
    preserved_by_request = {item.request_id: item for item in preserved.assignments}

    assert flexible.status == preserved.status == "complete"
    assert set(flexible_by_request) == set(preserved_by_request) == {1, 2}
    assert flexible_by_request[1].section_id == 1
    assert preserved_by_request[1].section_id == 2
    assert preserved_by_request[1].previous_section_id == 2
    assert flexible_by_request[2].section_id == preserved_by_request[2].section_id == 3
    assert all(item.student_id == 1 for item in preserved.assignments)


def test_realistic_scale_fixture_has_uneven_but_sufficient_course_capacity():
    """The manual school-scale fixture is varied, not a copy of the uniform benchmark."""

    data = build_realistic_scale_fixture()
    summary = summarize_realistic_fixture(data)
    demand_by_course = Counter(item.course_id for item in data.requests)
    capacity_by_course = Counter()
    for section in data.sections:
        capacity_by_course[section.member_course_ids[0]] += section.capacity_max

    assert summary == {
        "student_count": 1400,
        "section_count": 300,
        "request_count": 9800,
        "mandatory_request_count": 5600,
        "primary_request_count": 9800,
        "approved_backup_request_count": 0,
        "nominal_seat_capacity": 10500,
        "fixed_active_enrollment_count": 0,
    }
    assert {demand_by_course[course_id] for course_id in range(1, 11)} == {280}
    assert {demand_by_course[course_id] for course_id in range(11, 31)} == {210}
    assert {demand_by_course[course_id] for course_id in range(31, 51)} == {140}
    assert {capacity_by_course[course_id] for course_id in range(1, 11)} == {336}
    assert {capacity_by_course[course_id] for course_id in range(11, 31)} == {216}
    assert {capacity_by_course[course_id] for course_id in range(31, 51)} == {141}


def test_production_shaped_medium_fixture_preserves_mixed_search_structure():
    data = build_production_shaped_medium_fixture(student_count=120)
    summary = summarize_production_shaped_medium_fixture(data)

    assert summary["student_count"] == 120
    assert summary["section_count"] == 308
    assert summary["online_request_count"] > 0
    assert summary["co_op_request_count"] > 0
    assert summary["half_semester_request_count"] > 0
    assert summary["study_request_count"] > 0
    assert summary["focus_request_count"] > 0
    assert summary["online_supervision_session_count"] == 4
    assert summary["special_lock_count"] > 0
    assert {
        request.delivery_kind for request in data.requests
    } >= {"normal_instruction", "online", "co_op"}
    assert {
        request.commitment_type
        for request in data.schedule_commitment_requests
    } >= {"study", "focus"}


def test_policy_generalization_suite_is_deterministic_and_semantically_distinct():
    suite = build_policy_generalization_suite()
    summaries = [
        summarize_policy_generalization_scenario(scenario, data)
        for scenario, data in suite
    ]

    assert [scenario.scenario_id for scenario, _data in suite] == [
        scenario.scenario_id for scenario in DEFAULT_POLICY_GENERALIZATION_SCENARIOS
    ]
    assert len({summary["input_fingerprint"] for summary in summaries}) == 3
    assert {scenario.scenario_version for scenario, _data in suite} == {"v1"}
    assert {scenario.generation_seed for scenario, _data in suite} == {None}
    assert [summary["mixed_grade_summary"]["student_count"] for summary in summaries] == [
        240, 320, 240
    ]
    assert summaries[0]["production_shaped_summary"]["special_lock_count"] < summaries[2][
        "production_shaped_summary"
    ]["special_lock_count"]

    repeated = [
        summarize_policy_generalization_scenario(
            scenario,
            build_mixed_grade_v2_fixture(
                student_count=scenario.student_count,
                special_profile_cycle=scenario.special_profile_cycle,
            ),
        )["input_fingerprint"]
        for scenario in DEFAULT_POLICY_GENERALIZATION_SCENARIOS
    ]
    assert repeated == [summary["input_fingerprint"] for summary in summaries]


def test_policy_generalization_target_promotions_preserve_the_three_pressure_families():
    scenarios = TARGET_POLICY_GENERALIZATION_SCENARIOS
    summaries = [
        summarize_policy_generalization_scenario(
            scenario,
            build_mixed_grade_v2_fixture(
                student_count=scenario.student_count,
                special_profile_cycle=scenario.special_profile_cycle,
            ),
        )
        for scenario in scenarios
    ]

    assert [scenario.scenario_id for scenario in scenarios] == [
        "reference_target",
        "reference_near_target",
        "population_pressure_near_target",
        "special_commitment_pressure_target",
    ]
    assert [scenario.scenario_version for scenario in scenarios] == [
        "target-v1",
        "target-v1",
        "target-v1",
        "target-v1",
    ]
    assert [summary["mixed_grade_summary"]["student_count"] for summary in summaries] == [
        1400,
        800,
        1050,
        1400,
    ]
    assert [scenario.special_profile_cycle for scenario in scenarios] == [100, 100, 100, 50]
    assert len({summary["input_fingerprint"] for summary in summaries}) == 4
    assert summaries[2]["mixed_grade_summary"]["request_count"] > summaries[1][
        "mixed_grade_summary"
    ]["request_count"]
    assert summaries[3]["mixed_grade_summary"]["special_commitment_count"] > summaries[1][
        "mixed_grade_summary"
    ]["special_commitment_count"]


def test_policy_generalization_fixture_rejects_a_cycle_that_omits_profiles():
    import pytest

    with pytest.raises(ValueError, match="include every defined profile"):
        build_production_shaped_medium_fixture(
            student_count=120,
            special_profile_cycle=12,
        )


def test_policy_generalization_protocol_is_explicit_and_fixed():
    assert POLICY_GENERALIZATION_POLICIES == (
        "adaptive",
        "stateless_role",
        "fixed_cycle",
    )
    assert POLICY_GENERALIZATION_PROFILE == "balanced"
    assert POLICY_GENERALIZATION_RANDOM_SEED == 101
    assert POLICY_GENERALIZATION_WORKER_COUNT == 1


def test_production_shaped_medium_fixture_is_hard_feasible_with_diagnostic_limits(monkeypatch):
    """The mixed benchmark is valid before any quality curve is measured."""

    import scheduling_engine.student_assignment.core as core

    monkeypatch.setattr(core, "STUDENT_ASSIGNMENT_HARD_FEASIBILITY_TIME_LIMIT_SECONDS", 60.0)
    monkeypatch.setattr(core, "STUDENT_ASSIGNMENT_HARD_FEASIBILITY_VALIDATION_TIME_LIMIT_SECONDS", 30.0)
    monkeypatch.setattr(core, "STUDENT_ASSIGNMENT_HARD_FEASIBILITY_WORKER_COUNT", 8)
    monkeypatch.setattr(core, "STUDENT_ASSIGNMENT_HARD_FEASIBILITY_VALIDATION_WORKER_COUNT", 8)
    monkeypatch.setattr(core, "STUDENT_ASSIGNMENT_OPTIMIZATION_TIME_LIMIT_SECONDS", 15.0)
    monkeypatch.setattr(core, "STUDENT_ASSIGNMENT_OPTIMIZATION_WORKER_COUNT", 8)

    data = build_production_shaped_medium_fixture(student_count=80)
    result = solve_student_assignment(data)

    assert result.status == "complete"
    # The bounded optimization pass may time out after retaining the complete
    # validated seed.  In that contractually valid case the public outcome is
    # ``unknown`` even though the returned recommendation is complete.
    assert result.solver_outcome in {"optimal", "feasible", "unknown"}
    assert not result.unmet_requests
    assert result.optimization_facts["stage_1"]["complete_seed_produced"] is True
    assert result.optimization_facts["stage_1"]["seed_validated_against_full_model"] is True
    assert len(result.commitment_assignments) == 9


def test_realistic_quality_fixture_respects_counselor_soft_priority_order():
    difficulty_first = solve_student_assignment(build_realistic_quality_tradeoff_fixture(
        difficulty_importance="extremely_important",
        course_category_diversity_importance="important",
    ))
    category_first = solve_student_assignment(build_realistic_quality_tradeoff_fixture(
        difficulty_importance="important",
        course_category_diversity_importance="extremely_important",
    ))

    assert {item.semester for item in difficulty_first.assignments} == {2}
    assert {item.semester for item in category_first.assignments} == {1, 2}
    assert difficulty_first.objective_components["difficulty_balance_penalty"] == 0
    assert category_first.objective_components["course_category_diversity_penalty"] == 0
