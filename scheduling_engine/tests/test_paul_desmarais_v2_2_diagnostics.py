"""Fast regression coverage for the preserved v2.2 negative case."""

from scheduling_engine.benchmark_individual_feasibility import (
    preflight_individual_feasibility,
)
from scheduling_engine.paul_desmarais_stress_benchmark import (
    V2_2_AUTHORITATIVE_FINGERPRINT,
    V2_3_AUTHORITATIVE_FINGERPRINT,
    build_paul_desmarais_shaped_g9_12_stress_v2_3_fixture,
    reconstruct_paul_desmarais_v2_2,
)
from scheduling_engine.paul_desmarais_v2_2_diagnostics import (
    build_small_collision_regression_input,
    capacity_only_matching,
    collision_diagnostic,
    mth1w_cgc1w_witness,
    run_v2_2_diagnostics,
    run_v2_3_diagnostics,
    v2_3_cross_grade_shared_course_witness,
)
from scheduling_engine.paul_desmarais_v2_3_qualification import (
    run_explicit_stage1_only,
)


def test_v2_2_reconstruction_preserves_exact_input_identity_and_topology():
    fixture = reconstruct_paul_desmarais_v2_2()
    assert fixture.fixture_fingerprint == V2_2_AUTHORITATIVE_FINGERPRINT
    assert len(fixture.input_data.student_grades) == 1400
    assert len(fixture.input_data.requests) == 11326
    assert len(fixture.input_data.sections) == 300
    assert sum(section.section_id > 0 for section in fixture.input_data.sections) == 294
    assert sum(section.section_id < 0 for section in fixture.input_data.sections) == 6


def test_full_v2_2_negative_case_reproduces_without_stage1():
    result = run_v2_2_diagnostics()
    assert result["fingerprint"] == V2_2_AUTHORITATIVE_FINGERPRINT
    assert result["isolated_preflight"] == {
        "student_count": 1400,
        "feasible_count": 1400,
        "infeasible_count": 0,
        "unresolved_count": 0,
    }
    assert result["static_audit"]["section_counts"] == {
        "ordinary_full": 276,
        "paired_half": 18,
        "online_supervision": 6,
        "total": 300,
    }
    assert result["static_audit"]["ordinary_total_seats"] == 11720
    assert result["capacity_only_matching"] == {
        "required_group_count": 10500,
        "maximum_assignable_group_count": 10500,
        "deficit": 0,
        "feasible": True,
    }
    assert result["collision_diagnostic"]["status"] == "infeasible"
    assert result["known_witness"] == {
        "courses": ("MTH1W", "CGC1W"),
        "course_ids": (1, 5),
        "shared_student_count": 329,
        "reachable_cells": [(1, 1), (2, 6)],
        "witness_cell": (1, 1),
        "reachable_capacity": 320,
        "deficiency": 9,
    }


def test_small_collision_regression_has_the_same_layered_failure():
    data = build_small_collision_regression_input()
    preflight = preflight_individual_feasibility(data)
    assert (preflight["student_count"], preflight["feasible_count"]) == (3, 3)
    assert preflight["infeasible_count"] == 0
    assert capacity_only_matching(data) == {
        "required_group_count": 6,
        "maximum_assignable_group_count": 6,
        "deficit": 0,
        "feasible": True,
    }
    assert collision_diagnostic(data)["status"] == "infeasible"


def test_v2_3_has_a_distinct_fingerprint_and_complementary_repeated_positions():
    fixture = build_paul_desmarais_shaped_g9_12_stress_v2_3_fixture()
    assert fixture.benchmark_id == "paul_desmarais_shaped_g9_12_stress_v2_3"
    assert fixture.benchmark_version == "v2.3"
    assert fixture.fixture_fingerprint == V2_3_AUTHORITATIVE_FINGERPRINT
    assert fixture.fixture_fingerprint != V2_2_AUTHORITATIVE_FINGERPRINT

    capacity_by_course_cell = {}
    for course_id in (1, 5):
        rows = [
            section for section in fixture.input_data.sections
            if section.section_id > 0 and section.member_course_ids == (course_id,)
        ]
        capacity_by_course_cell[course_id] = {
            cell: sum(
                section.capacity_max
                for section in rows
                if (section.semester, section.timeslot_id) == cell
            )
            for cell in {(1, 1), (2, 6)}
        }
    assert capacity_by_course_cell == {
        1: {(1, 1): 200, (2, 6): 160},
        5: {(1, 1): 160, (2, 6): 200},
    }


def test_v2_3_one_repair_passes_static_layers_but_retains_remaining_layer_d_failure():
    result = run_v2_3_diagnostics()
    assert result["fingerprint"] == V2_3_AUTHORITATIVE_FINGERPRINT
    assert result["isolated_preflight"] == {
        "student_count": 1400,
        "feasible_count": 1400,
        "infeasible_count": 0,
        "unresolved_count": 0,
    }
    assert result["capacity_only_matching"]["feasible"] is True
    assert result["correlated_pair_checks"]["failing_count"] == 0
    assert result["collision_diagnostic"]["status"] == "infeasible"
    assert v2_3_cross_grade_shared_course_witness(
        build_paul_desmarais_shaped_g9_12_stress_v2_3_fixture().input_data
    ) == {
        "left_courses": ("FRA1W", "FRA2D"),
        "shared_course": "TIJ1O",
        "shared_by_grade": {9: 349, 10: 349},
        "left_candidate_cells": {3: ((1, 3), (2, 8)), 11: ((1, 3), (2, 8))},
        "left_s2d_capacity": {3: 160, 11: 160},
        "witness_cell": (2, 8),
        "paired_side_cell": (1, 3),
        "required_shared_course_use_in_s2d": 378,
        "shared_course_s2d_capacity": 360,
        "deficiency": 18,
    }


def test_v2_3_stage1_guard_stops_before_solver_when_layer_d_fails():
    result = run_v2_3_diagnostics()
    stage1 = run_explicit_stage1_only(result)
    assert stage1 == {
        "stage1_reached": False,
        "reason": "qualification_layer_failed",
        "gates": {
            "static_identity": True,
            "isolated": True,
            "capacity_only": True,
            "correlated_pairs": True,
            "collision_layer": False,
        },
    }
