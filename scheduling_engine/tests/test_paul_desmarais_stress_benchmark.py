"""Bounded static checks for the versioned Paul-Desmarais-shaped benchmark."""

from dataclasses import replace

from scheduling_engine.paul_desmarais_stress_benchmark import (
    BENCHMARK_ID,
    BENCHMARK_ROTATION,
    BENCHMARK_VERSION,
    PROVENANCE_CLASSES,
    build_paul_desmarais_shaped_g9_12_stress_fixture,
    fixed_context_co_op_coverage,
    summarize_paul_desmarais_shaped_g9_12_stress_fixture,
)
from scheduling_engine.benchmark_individual_feasibility import (
    check_individual_feasibility,
    preflight_individual_feasibility,
)
from scheduling_engine.student_assignment import solve_student_assignment


def _fixture_and_audit():
    fixture = build_paul_desmarais_shaped_g9_12_stress_fixture()
    return fixture, summarize_paul_desmarais_shaped_g9_12_stress_fixture(fixture)


def test_identity_population_structure_and_fingerprint_are_deterministic():
    fixture, audit = _fixture_and_audit()
    repeated, repeated_audit = _fixture_and_audit()

    assert (fixture.benchmark_id, fixture.benchmark_version) == (BENCHMARK_ID, BENCHMARK_VERSION)
    assert audit["student_count"] == 1_400
    assert audit["grade_distribution"] == {9: 350, 10: 350, 11: 350, 12: 350}
    assert {course.grade_level for course in fixture.courses}.issubset({9, 10, 11, 12})
    assert audit["term_count"] == 2
    assert audit["instructional_blocks_per_term"] == 4
    assert audit["rotation"] == {day: list(blocks) for day, blocks in BENCHMARK_ROTATION}
    assert fixture.fixture_fingerprint == repeated.fixture_fingerprint
    assert audit["input_fingerprint"] == repeated_audit["input_fingerprint"]


def test_provenance_and_static_population_counts_are_explicit():
    fixture, audit = _fixture_and_audit()
    assert {item.provenance for item in fixture.assumptions} == PROVENANCE_CLASSES
    assert all(item.provenance in PROVENANCE_CLASSES for item in fixture.assumptions)
    assert audit["occupied_position_distribution"] == {8: 1_400}
    assert audit["study_request_count"] == 70
    assert audit["study_by_grade"] == {12: 70}
    assert audit["focus_by_grade"] == {11: 14, 12: 14}
    assert audit["co_op_by_grade"] == {9: 1, 10: 1, 11: 20, 12: 20}
    assert audit["online_by_grade"] == {9: 21, 10: 21, 11: 21, 12: 21}


def test_half_courses_study_and_sequence_have_the_correct_fixture_semantics():
    fixture, audit = _fixture_and_audit()
    course_by_code = {course.code: course for course in fixture.courses}
    requests = fixture.input_data.requests

    assert course_by_code["CHV2O"].credit_value == 0.5
    assert course_by_code["GLC2O"].credit_value == 0.5
    assert audit["half_pair_student_count"] == 350
    assert audit["half_pair_upper_grade_count"] == 5
    assert audit["half_course_credit_values"] == {0.5: 700}
    assert audit["sequence_preference"] == {
        "earlier": "MCF3M",
        "later": "MCR3U",
        "eligible_opportunities": 56,
        "provenance": "synthetic_coverage_case",
    }
    assert not fixture.input_data.hard_prerequisites
    assert all(request.credit_value == 0.5 for request in requests if request.duration == "half_semester")
    paired_requests = [request for request in requests if request.student_id == 352 and request.duration == "half_semester"]
    assert {(request.course_id, request.paired_half_course_id) for request in paired_requests} == {(15, 16), (16, 15)}
    assert all(request.course_id != 41 or request.credit_value == 2.0 for request in requests)
    assert next(item.value for item in fixture.assumptions if item.key == "study_maximum") is None


def test_current_category_and_metadata_only_difficulty_lineage_are_preserved():
    fixture, audit = _fixture_and_audit()
    assert "english" not in {course.category for course in fixture.courses}
    assert set(audit["category_request_counts"]) == {
        "arts", "business", "humanities", "language", "math", "science", "technology",
    }
    assert audit["category_relationship_provenance"] == "synthetic_default_relationships"
    assert audit["difficulty_provenance"] == {
        "calculation_version": "metadata_and_relative_history_v2",
        "source": "metadata",
        "historical_observation_count": 0,
        "historical_confidence": 0.0,
        "study_intended_future_domain_difficulty": 0,
    }
    assert all(item.source == "metadata" for item in fixture.input_data.course_difficulties)
    assert all(item.historical_observation_count == 0 for item in fixture.input_data.course_difficulties)


def test_special_programs_are_compositional_and_other_co_op_shapes_are_not_silently_movable():
    fixture, audit = _fixture_and_audit()
    combinations = audit["special_program_combinations"]
    for combination in (
        "co_op+study",
        "co_op+online",
        "online+study",
        "co_op+online+study",
        "focus+online",
    ):
        assert combinations[combination] >= 1

    shapes = {item["shape_id"]: item["materialization_status"] for item in audit["co_op_shape_coverage"]}
    assert shapes["connected_two_credit"] == "movable_supported_current_v1"
    assert shapes["two_independent_one_credit"] == "movable_unsupported_current_v1"
    assert shapes["flexible_two_credit"] == "movable_unsupported_current_v1"
    fixed = fixed_context_co_op_coverage()
    assert fixed[0].credit_value == 1.0 and len(fixed[0].occupancy) == 2
    assert fixed[1].credit_value == 4.0 and len(fixed[1].occupancy) == 8


def test_student_352_is_feasible_under_the_actual_detached_candidate_contract():
    fixture, _audit = _fixture_and_audit()
    result = check_individual_feasibility(fixture.input_data, 352)

    assert result.status == "feasible"
    assert len(result.decision_groups) == 8  # Seven full course groups plus CHV2O/GLC2O.
    group_by_request = {
        request_id: group
        for group in result.decision_groups
        for request_id in group.source_request_ids
    }
    assert [candidate.identity for candidate in group_by_request[2813].candidates] == [
        (13,), (14,), (-1,), (-2,), (-3,), (-4,), (-5,), (-6,), (-7,), (-8,),
    ]
    half_group = group_by_request[2814]
    assert half_group.source_request_ids == (2814, 2815)
    assert [candidate.identity for candidate in half_group.candidates] == [(77, 78), (79, 80)]
    occupied = [item for candidate in result.selected_candidates for item in candidate.occupancy]
    assert len(occupied) == len(set(occupied)) == 16


def test_every_stress_fixture_student_has_an_individually_feasible_completion():
    fixture, _audit = _fixture_and_audit()
    preflight = preflight_individual_feasibility(fixture.input_data)

    assert preflight["student_count"] == 1_400
    assert preflight["feasible_count"] == 1_400
    assert preflight["infeasible_count"] == 0
    assert preflight["unresolved_count"] == 0
    assert preflight["by_status_grade"]["feasible"] == {9: 350, 10: 350, 11: 350, 12: 350}


def test_student_352_preflight_matches_the_bounded_engine_completion_model():
    fixture, _audit = _fixture_and_audit()
    data = fixture.input_data
    student_data = replace(
        data,
        requests=tuple(request for request in data.requests if request.student_id == 352),
        schedule_commitment_requests=(),
        student_grades=((352, 10),),
        time_limit_seconds=2.0,
    )
    result = solve_student_assignment(student_data)
    request_by_id = {request.request_id: request for request in student_data.requests}

    assert result.status == "complete"
    assert not result.unmet_requests
    # This is the current detached input contract: negative engine-only online
    # section identities are candidates whenever their offering membership says
    # so, even for a request whose delivery kind is normal_instruction.
    assert any(
        assignment.section_id is not None
        and assignment.section_id < 0
        and request_by_id[assignment.request_id].delivery_kind == "normal_instruction"
        for assignment in result.assignments
    )
