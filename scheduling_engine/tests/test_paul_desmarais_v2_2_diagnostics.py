"""Fast regression coverage for the preserved v2.2 negative case."""

from scheduling_engine.benchmark_individual_feasibility import (
    preflight_individual_feasibility,
)
from scheduling_engine.paul_desmarais_stress_benchmark import (
    V2_2_AUTHORITATIVE_FINGERPRINT,
    reconstruct_paul_desmarais_v2_2,
)
from scheduling_engine.paul_desmarais_v2_2_diagnostics import (
    build_small_collision_regression_input,
    capacity_only_matching,
    collision_diagnostic,
    mth1w_cgc1w_witness,
    run_v2_2_diagnostics,
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
