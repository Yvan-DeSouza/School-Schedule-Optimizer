from types import SimpleNamespace

import pytest
from ortools.sat.python import cp_model

from scheduling_engine.realistic_student_assignment_validation import (
    apply_mixed_grade_v2_profile,
    build_mixed_grade_v2_fixture,
    summarize_mixed_grade_v2_fixture,
)
from scheduling_engine.student_assignment.core import (
    run_student_assignment_stage2_diagnostic,
)
from scheduling_engine.student_assignment.operator_characterization import (
    CHARACTERIZATION_SCHEMA,
    OPERATOR_ROLES,
    aggregate_operator_characterization,
    build_adaptive_readiness_matrix,
    build_capability_card,
    build_operator_characterization_record,
    estimate_attempts_per_time_window,
    run_operator_characterization_trial,
    summarize_stagnation,
)
from scheduling_engine.student_assignment.search_experiments import (
    source_decision_fingerprint,
)
from scheduling_engine.student_assignment.operator_session import OPERATOR_FAMILIES
from scheduling_engine.student_assignment.runtime import (
    semantic_student_assignment_input_fingerprint,
)
from scheduling_engine.student_assignment.stage2_benchmark import (
    read_durable_stage2_benchmark,
    read_student_assignment_input_snapshot,
    write_student_assignment_input_snapshot,
)
from scheduling_engine.student_assignment.solver import (
    validate_source_decision_candidate_with_status,
)


def _quality(*, utilization, semester, difficulty, category, pressure):
    components = {
        "section_utilization_balance": {
            "raw_penalty": utilization,
            "denominator": 100,
            "weighted_normalized_contribution": utilization * 2,
        },
        "student_semester_load_balance": {
            "raw_penalty": semester,
            "denominator": 100,
            "weighted_normalized_contribution": semester * 2,
        },
        "difficulty_balance": {
            "raw_penalty": difficulty,
            "denominator": 100,
            "weighted_normalized_contribution": difficulty * 2,
        },
        "course_category_diversity": {
            "raw_penalty": category,
            "denominator": 100,
            "weighted_normalized_contribution": category * 2,
        },
        "course_sequence_preferences": {
            "raw_penalty": 0,
            "denominator": 100,
            "weighted_normalized_contribution": 0,
        },
    }
    return {
        "objective_semantics": {"components": components},
        "student_semester_load_balance": {
            "entities": {"1": {"absolute_difference": pressure}}
        },
        "difficulty_balance": {
            "entities": {"1": {"absolute_difference": pressure}}
        },
        "course_category_diversity": {
            "entities": {"1": {"penalty": pressure}}
        },
        "course_sequence_preferences": {"entities": {}},
    }


def _result():
    return SimpleNamespace(
        solver_outcome="optimal",
        optimization_facts={
            "stage_2": {},
            "stage_2_local_bootstrap": {
                "candidate_found": True,
                "candidate_validated": True,
                "improvement_adopted": True,
                "neighborhood_radius": 8,
                "max_changed_students": 1,
                "solver_wall_time_seconds": 2.0,
                "validation_elapsed_seconds": 0.5,
                "model_variable_count": 11,
                "model_constraint_count": 12,
                "iterations": ({
                    "adopted": True,
                    "candidate_source_decision_fingerprint": "candidate-fingerprint",
                    "cumulative_session_elapsed_seconds": 3.0,
                },),
            }
        },
    )


def test_mixed_grade_v2_fixture_has_current_version_and_all_grades(tmp_path):
    data = build_mixed_grade_v2_fixture(student_count=80)
    reprofiled = apply_mixed_grade_v2_profile(data)
    assert reprofiled.student_grades == data.student_grades
    assert semantic_student_assignment_input_fingerprint(reprofiled) == (
        semantic_student_assignment_input_fingerprint(data)
    )
    summary = summarize_mixed_grade_v2_fixture(data)
    assert summary["objective_semantics_version"] == "v2"
    assert summary["grade_counts"] == {9: 20, 10: 20, 11: 20, 12: 20}
    assert summary["request_count"] > 0
    assert summary["section_count"] > 0
    assert summary["special_commitment_count"] > 0
    assert summary["input_fingerprint"]
    path = tmp_path / "mixed_grade_v2_input.json.gz"
    fingerprint = semantic_student_assignment_input_fingerprint(data)
    write_student_assignment_input_snapshot(
        path,
        data=data,
        input_fingerprint=fingerprint,
    )
    restored = read_student_assignment_input_snapshot(
        path,
        expected_input_fingerprint=fingerprint,
    )
    assert restored["data"].student_grades == data.student_grades
    assert restored["input_semantic_fingerprint"] == fingerprint


def test_mixed_grade_v2_production_shape_artifact_is_versioned_and_input_bound():
    benchmark = read_durable_stage2_benchmark(
        "scheduling_engine/benchmarks/student_assignment/mixed_grade_v2_production_shape"
    )
    manifest = benchmark["manifest"]
    assert manifest["benchmark_schema"] == "student_assignment_stage2_benchmark_v1"
    assert manifest["input_semantic_fingerprint"] == (
        "c07c77d0aa077a3e72240f27644d86b8a1a4faecb2f72a900aacc3fcb792d28a"
    )
    assert manifest["seed_source_decision_fingerprint"] == (
        "d5036a44e71d5a3b2a94eebe51d645bb4034179a0dd29537492ea81feda2e900"
    )
    assert manifest["counts"] == {
        "student_count": 1400,
        "request_count": 10760,
        "required_source_decision_group_count": 10945,
        "normal_section_count": 304,
        "student_assignment_section_record_count": 317,
        "online_supervision_session_count": 13,
        "special_commitment_count": 310,
        "special_commitment_request_count": 185,
    }
    assert benchmark["data"].objective_semantics_version == "v2"
    assert len(benchmark["data"].student_grades) == 1400


def _single_boolean_candidate_model():
    model = cp_model.CpModel()
    selected = model.NewBoolVar("selected")
    return model, selected


def test_candidate_validation_distinguishes_validated_from_hard_invalid():
    model, selected = _single_boolean_candidate_model()
    outcome = validate_source_decision_candidate_with_status(
        model,
        ((selected,),),
        {selected.Index(): 1},
        5,
        worker_count=1,
    )
    assert outcome.classification == "validated"
    assert outcome.solver_outcome in {"optimal", "feasible"}
    assert outcome.solver is not None

    invalid_model, invalid_selected = _single_boolean_candidate_model()
    invalid_outcome = validate_source_decision_candidate_with_status(
        invalid_model,
        ((invalid_selected,),),
        {invalid_selected.Index(): 0},
        5,
        worker_count=1,
    )
    assert invalid_outcome.classification == "hard_invalid"
    assert invalid_outcome.solver_outcome == "infeasible"
    assert invalid_outcome.solver is None


def test_candidate_validation_unknown_and_errors_are_not_adoptable(monkeypatch):
    model, selected = _single_boolean_candidate_model()

    class UnknownSolver:
        def Solve(self, _model):
            return cp_model.UNKNOWN

    monkeypatch.setattr(
        "scheduling_engine.student_assignment.solver.new_solver",
        lambda *args, **kwargs: UnknownSolver(),
    )
    unknown = validate_source_decision_candidate_with_status(
        model,
        ((selected,),),
        {selected.Index(): 1},
        5,
        worker_count=1,
    )
    assert unknown.classification == "validation_unknown"
    assert unknown.solver_outcome == "unknown"
    assert unknown.solver is None

    def raise_solver(*args, **kwargs):
        raise RuntimeError("validator unavailable")

    monkeypatch.setattr(
        "scheduling_engine.student_assignment.solver.new_solver",
        raise_solver,
    )
    error = validate_source_decision_candidate_with_status(
        model,
        ((selected,),),
        {selected.Index(): 1},
        5,
        worker_count=1,
    )
    assert error.classification == "validation_error"
    assert error.solver_outcome == "error"
    assert error.solver is None
    assert "RuntimeError" in error.error


def test_characterization_role_map_covers_every_diagnostic_operator_family():
    assert set(OPERATOR_FAMILIES) <= set(OPERATOR_ROLES)


def test_characterization_record_preserves_role_specific_and_global_facts():
    data = build_mixed_grade_v2_fixture(student_count=80)
    initial = _quality(utilization=10, semester=3, difficulty=20, category=30, pressure=8)
    final = _quality(utilization=6, semester=3, difficulty=18, category=30, pressure=2)
    record = build_operator_characterization_record(
        data=data,
        initial_quality=initial,
        final_quality=final,
        result=_result(),
        benchmark_name="mixed_grade_v2_test",
        operator="targeted_r8_s1",
        input_fingerprint="input",
        source_seed_fingerprint="seed",
        selected_student_ids=(1,),
    )
    payload = record.to_dict()
    assert payload["schema"] == CHARACTERIZATION_SCHEMA
    assert record.role == "student_pressure_repair"
    assert record.counselor_profile["difficulty_balance"] == 6
    assert record.utilization_cluster_policy == "not_applicable"
    assert record.total_gain > 0
    assert record.role_specific_gain > 0
    assert record.first_improvement_seconds == 3.0
    assert record.candidate_validated is True
    assert record.candidate_source_decision_fingerprint == "candidate-fingerprint"
    assert record.to_json()


def test_characterization_can_capture_raw_candidate_source_decisions_on_request():
    data = build_mixed_grade_v2_fixture(student_count=80)
    initial = _quality(utilization=10, semester=3, difficulty=20, category=30, pressure=8)
    result = _result()
    result.optimization_facts["stage_2"]["final_source_decisions"] = (
        (("course", 1), (7, 11, None, None, None, None)),
    )
    record = build_operator_characterization_record(
        data=data,
        initial_quality=initial,
        final_quality=initial,
        result=result,
        benchmark_name="mixed_grade_v2_test",
        operator="targeted_r8_s1",
        input_fingerprint="input",
        capture_candidate_source_decisions=True,
    )
    assert record.candidate_source_decisions == (
        (("course", 1), (7, 11, None, None, None, None)),
    )


def test_source_decision_fingerprint_is_order_independent_and_semantic():
    first = (
        (("course", 1), (7, 11, None, None, None, None)),
        (("commitment", 2), (4, "A+B")),
    )
    reordered = tuple(reversed(first))
    changed = (
        (("course", 1), (7, 12, None, None, None, None)),
        (("commitment", 2), (4, "A+B")),
    )

    assert source_decision_fingerprint(first) == source_decision_fingerprint(reordered)
    assert source_decision_fingerprint(first) != source_decision_fingerprint(changed)


def test_characterization_aggregation_and_readiness_matrix_are_descriptive():
    data = build_mixed_grade_v2_fixture(student_count=80)
    initial = _quality(utilization=10, semester=3, difficulty=20, category=30, pressure=8)
    final = _quality(utilization=6, semester=3, difficulty=18, category=30, pressure=2)
    records = tuple(
        build_operator_characterization_record(
            data=data,
            initial_quality=initial,
            final_quality=final,
            result=_result(),
            benchmark_name="mixed_grade_v2_test",
            operator=operator,
            input_fingerprint="input",
            source_seed_fingerprint="seed",
            selected_student_ids=(1,),
        )
        for operator in ("targeted_r8_s1", "targeted_r8_s2")
    )
    scorecard = aggregate_operator_characterization(records)
    assert scorecard["targeted_r8_s1"]["trial_count"] == 1
    assert scorecard["targeted_r8_s1"]["validated_adoption_count"] == 1
    assert scorecard["targeted_r8_s1"]["success_rate"] == 1.0
    assert build_capability_card("targeted_r8_s1", scorecard)["role"] == (
        "student_pressure_repair"
    )
    matrix = build_adaptive_readiness_matrix(scorecard)
    assert set(matrix) == {"student_pressure_repair"}
    assert len(matrix["student_pressure_repair"]) == 2


def test_role_specific_characterization_distinguishes_utilization_and_escape():
    data = build_mixed_grade_v2_fixture(student_count=80)
    initial = _quality(utilization=10, semester=3, difficulty=20, category=30, pressure=8)
    final = _quality(utilization=6, semester=3, difficulty=18, category=30, pressure=2)
    utilization = build_operator_characterization_record(
        data=data,
        initial_quality=initial,
        final_quality=final,
        result=_result(),
        benchmark_name="mixed_grade_v2_test",
        operator="targeted_utilization_r16_s2",
        input_fingerprint="input",
    )
    escape = build_operator_characterization_record(
        data=data,
        initial_quality=initial,
        final_quality=final,
        result=_result(),
        benchmark_name="mixed_grade_v2_test",
        operator="grade_bounded_g9",
        input_fingerprint="input",
        selected_grade=9,
    )
    assert utilization.role == "section_utilization_repair"
    assert utilization.starting_role_value == 10
    assert utilization.final_role_value == 6
    assert utilization.starting_role_facts["section_utilization"]["raw_penalty"] == 10
    assert utilization.final_role_facts["section_utilization"]["raw_penalty"] == 6
    assert escape.role == "basin_escape"
    assert escape.starting_role_value > escape.final_role_value


def test_stagnation_unknown_is_not_reported_as_optimality():
    summary = summarize_stagnation((
        {"adopted": False, "status": "unknown"},
        {"adopted": False, "status": "unknown"},
    ))
    assert summary["classification"] == "unresolved"
    assert summary["mathematical_optimality_claim"] is False
    assert estimate_attempts_per_time_window([
        {"total_operation_seconds": 10},
        {"total_operation_seconds": 20},
    ])["60"] == 4


def test_characterization_uses_attempt_model_facts_when_session_summary_is_compact():
    data = build_mixed_grade_v2_fixture(student_count=80)
    initial = _quality(utilization=10, semester=3, difficulty=20, category=30, pressure=8)
    result = _result()
    result.optimization_facts["stage_2_local_bootstrap"].pop("model_variable_count")
    result.optimization_facts["stage_2_local_bootstrap"].pop("model_constraint_count")
    result.optimization_facts["stage_2_local_bootstrap"].pop("branches", None)
    result.optimization_facts["stage_2_local_bootstrap"].pop("conflicts", None)
    result.optimization_facts["stage_2_local_bootstrap"]["iterations"] = ({
        "adopted": True,
        "candidate_adopted": True,
        "cumulative_session_elapsed_seconds": 3.0,
        "model_variable_count": 112,
        "model_constraint_count": 224,
        "branches": 17,
        "conflicts": 2,
    },)
    record = build_operator_characterization_record(
        data=data,
        initial_quality=initial,
        final_quality=initial,
        result=result,
        benchmark_name="mixed_grade_v2_test",
        operator="targeted_r8_s2",
        input_fingerprint="input",
    )
    assert record.model_variable_count == 112
    assert record.model_constraint_count == 224
    assert record.branches == 17
    assert record.conflicts == 2


def test_characterization_runner_uses_existing_diagnostic_session_boundary():
    from dataclasses import replace

    from scheduling_engine.realistic_student_assignment_validation import (
        build_realistic_quality_tradeoff_fixture,
    )

    data = replace(
        build_realistic_quality_tradeoff_fixture(),
        objective_semantics_version="v2",
        objective_importance_scores={
            "section_utilization_balance": 6,
            "student_semester_balance": 6,
            "course_sequence_preferences": 6,
            "difficulty_balance": 6,
            "course_category_diversity": 6,
        },
        student_grades=((1, 9),),
    )
    initial = run_student_assignment_stage2_diagnostic(
        data,
        total_time_limit_seconds=5,
        hard_feasibility_time_limit_seconds=5,
        hard_feasibility_validation_time_limit_seconds=5,
        hard_feasibility_worker_count=1,
        hard_feasibility_validation_worker_count=1,
        optimization_worker_count=1,
        capture_final_source_decisions=True,
    )
    assert initial.status == "complete"
    record = run_operator_characterization_trial(
        data,
        initial_result=initial,
        initial_source_decisions=initial.optimization_facts["stage_2"][
            "final_source_decisions"
        ],
        benchmark_name="operator_characterization_smoke",
        operator="r2",
        total_time_limit_seconds=2,
        max_attempts=1,
        per_attempt_time_limit_seconds=0.5,
        worker_count=1,
        collect_resource_telemetry=False,
        hard_feasibility_validation_time_limit_seconds=2,
        hard_feasibility_validation_worker_count=1,
        cp_sat_random_seed=202,
        cp_sat_max_deterministic_time_seconds=3.5,
    )
    assert record.schema == CHARACTERIZATION_SCHEMA
    assert record.operator == "r2"
    assert record.role == "local_descent"
    assert record.solver_status in {"optimal", "feasible", "infeasible", "unknown"}
    assert record.cp_sat_random_seed == 202
    assert record.cp_sat_max_deterministic_time_seconds == pytest.approx(3.5)
