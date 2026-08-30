from scheduling_engine.dto import (
    StudentAssignmentInputDTO,
    StudentAssignmentRequestDTO,
    StudentAssignmentSectionDTO,
)
from dataclasses import replace
from scheduling_engine.student_assignment.core import (
    run_student_assignment_stage2_diagnostic,
)
from scheduling_engine.student_assignment.validation_benchmark import (
    VALIDATION_BENCHMARK_SCHEMA,
    run_source_decision_validation_benchmark,
)


def _data():
    return StudentAssignmentInputDTO(
        academic_year_id=1,
        requests=(StudentAssignmentRequestDTO(
            request_id=1,
            student_id=1,
            course_id=1,
            course_offering_id=11,
            is_primary=True,
            is_mandatory=True,
            priority_tier=1,
        ),),
        sections=(StudentAssignmentSectionDTO(
            section_id=1,
            delivery_group_id=1,
            member_course_offering_ids=(11,),
            member_course_ids=(1,),
            semester=1,
            timeslot_id=101,
            capacity_max=1,
            target_capacity=1,
        ),),
        fixed_enrollments=(),
        hard_prerequisites=(),
        soft_sequence_preferences=(),
        section_utilization_balance_importance="not_important",
        student_semester_balance_importance="not_important",
        course_sequence_preferences_importance="not_important",
        difficulty_balance_importance="not_important",
        course_category_diversity_importance="not_important",
    )


def _source_decisions(data):
    result = run_student_assignment_stage2_diagnostic(
        data,
        total_time_limit_seconds=5.0,
        collect_incumbent_timeline=False,
        capture_final_source_decisions=True,
    )
    return tuple(result.optimization_facts["stage_2"]["final_source_decisions"])


def test_validation_benchmark_reports_authoritative_telemetry_for_valid_candidate():
    data = _data()
    source_decisions = _source_decisions(data)

    report = run_source_decision_validation_benchmark(
        data,
        source_decisions,
        candidate_name="valid-small",
        time_limit_seconds=5.0,
        worker_count=1,
    )

    assert report["schema"] == VALIDATION_BENCHMARK_SCHEMA
    assert report["validation"]["classification"] == "validated"
    assert report["validation"]["solver_outcome"] in {"feasible", "optimal"}
    assert report["validation"]["source_decision_identity_matches"] is True
    telemetry = report["validation"]["telemetry"]
    assert telemetry["clone_wall_time_seconds"] >= 0
    assert telemetry["cp_sat_solve_external_wall_time_seconds"] >= 0
    assert report["result"]["assignment_count"] == 1
    assert report["result"]["unmet_request_count"] == 0


def test_validation_benchmark_fails_closed_for_unrepresentable_candidate():
    data = _data()

    report = run_source_decision_validation_benchmark(
        data,
        ((("course", 1), (1, 999, None, 1, 101, None)),),
        candidate_name="invalid-small",
        time_limit_seconds=5.0,
        worker_count=1,
    )

    assert report["validation"]["classification"] == "validation_error"
    assert report["validation"]["solver_outcome"] == "error"
    assert report["result"] is None
    assert "failed full-model validation" in report["validation"]["error"]


def test_validation_benchmark_rejects_stale_input_identity_before_solving():
    data = _data()
    source_decisions = _source_decisions(data)

    try:
        run_source_decision_validation_benchmark(
            data,
            source_decisions,
            expected_input_fingerprint="stale-input",
            time_limit_seconds=5.0,
        )
    except ValueError as error:
        assert "input fingerprint" in str(error)
    else:  # pragma: no cover - assertion-shaped defensive branch
        raise AssertionError("stale input identity was not rejected")


def test_validation_benchmark_can_capture_native_search_start_facts():
    data = _data()
    report = run_source_decision_validation_benchmark(
        data,
        _source_decisions(data),
        time_limit_seconds=5.0,
        worker_count=1,
        collect_validation_search_start_telemetry=True,
    )

    native = report["validation"]["telemetry"]["native_cp_sat"]
    assert report["validation"]["classification"] == "validated"
    assert native["log_summary_available"] is True
    assert native["search_start"]["search_started"] is True
    assert native["search_start"]["first_solution_found"] is True


def test_validation_benchmark_presolve_probe_is_inconclusive_not_authoritative():
    data = _data()
    report = run_source_decision_validation_benchmark(
        data,
        _source_decisions(data),
        time_limit_seconds=5.0,
        worker_count=1,
        collect_validation_presolve_telemetry=True,
    )

    native = report["validation"]["telemetry"]["native_cp_sat"]
    assert report["validation"]["classification"] == "validation_unknown"
    assert report["validation"]["solver_outcome"] == "unknown"
    assert report["result"] is None
    assert native["stop_after_presolve"] is True
    assert native["presolve_phase_wall_seconds"] >= 0


def test_validation_benchmark_reports_optional_source_decisions_as_free():
    data = replace(
        _data(),
        requests=(
            _data().requests[0],
            StudentAssignmentRequestDTO(
                request_id=2,
                student_id=1,
                course_id=2,
                course_offering_id=12,
                is_primary=False,
                is_mandatory=False,
                priority_tier=2,
            ),
        ),
        sections=(
            _data().sections[0],
            StudentAssignmentSectionDTO(
                section_id=2,
                delivery_group_id=2,
                member_course_offering_ids=(12,),
                member_course_ids=(2,),
                semester=1,
                timeslot_id=102,
                capacity_max=1,
                target_capacity=1,
            ),
        ),
    )
    report = run_source_decision_validation_benchmark(
        data,
        _source_decisions(data),
        time_limit_seconds=5.0,
        worker_count=1,
    )

    freedom = report["validation"]["telemetry"]["variable_freedom"]
    assert report["validation"]["classification"] == "validated"
    assert freedom["source_variable_count"] == 2
    assert freedom["completion_defining_source_variable_count"] == 1
    assert freedom["source_variable_not_fixed_by_candidate_count"] == 1
