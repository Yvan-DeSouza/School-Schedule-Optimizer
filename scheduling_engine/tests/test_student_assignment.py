"""Contracts for pure student-to-section assignment; no Django dependency."""

from unittest.mock import patch

from ortools.sat.python import cp_model

import scheduling_engine.student_assignment.core as student_assignment_module
import scheduling_engine.student_assignment.solver as student_assignment_solver
from scheduling_engine.dto import (
    CourseCategoryRelationshipDTO,
    CourseDifficultyDTO,
    CoursePrerequisiteDTO,
    CourseSequencePreferenceDTO,
    FixedEnrollmentDTO,
    FixedStudentScheduleCommitmentDTO,
    StudentScheduleCommitmentRequestDTO,
    StudentSpecialCommitmentLockDTO,
    StudentAssignmentInputDTO,
    StudentAssignmentLockDTO,
    StudentAssignmentRequestDTO,
    StudentAssignmentSectionDTO,
    StudentAssignmentScopeDTO,
    TimeSlotDTO,
)
from scheduling_engine.student_assignment import (
    run_substantive_soft_tier_probe,
    solve_student_assignment,
)
from scheduling_engine.student_assignment.runtime import MonotonicDeadline
from scheduling_engine.student_assignment.runtime import (
    semantic_student_assignment_input_fingerprint,
)
from scheduling_engine.student_assignment.stage2_benchmark import (
    append_experiment_record,
    compact_substantive_probe_record,
    read_durable_stage2_benchmark,
    read_stage1_seed_snapshot,
    read_student_assignment_input_snapshot,
    replay_durable_stage1_seed,
    semantic_stage1_seed_source_fingerprint,
    write_durable_stage2_benchmark,
    write_student_assignment_input_snapshot,
    write_stage1_seed_snapshot,
)


def _request(request_id=1, **overrides):
    values = dict(
        request_id=request_id, student_id=1, course_id=1, course_offering_id=11,
        is_primary=True, is_mandatory=False, priority_tier=4,
    )
    values.update(overrides)
    return StudentAssignmentRequestDTO(**values)


def _section(section_id=1, **overrides):
    values = dict(
        section_id=section_id, delivery_group_id=1, member_course_offering_ids=(11,),
        member_course_ids=(1,), semester=1, timeslot_id=101,
        capacity_max=2, target_capacity=2,
    )
    values.update(overrides)
    return StudentAssignmentSectionDTO(**values)


def _input(**overrides):
    values = dict(
        academic_year_id=1, requests=(_request(),), sections=(_section(),),
        fixed_enrollments=(), hard_prerequisites=(), soft_sequence_preferences=(),
        section_utilization_balance_importance="important",
        student_semester_balance_importance="important",
        course_sequence_preferences_importance="important",
    )
    values.update(overrides)
    return StudentAssignmentInputDTO(**values)


def _timeslots():
    """One available A-D pattern in each semester for special commitments."""

    return tuple(
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


def _substantive_probe_input():
    """Two mandatory courses with a known zero semester-balance solution."""

    return _input(
        requests=(
            _request(
                1,
                course_id=1,
                course_offering_id=11,
                is_mandatory=True,
            ),
            _request(
                2,
                course_id=2,
                course_offering_id=12,
                is_mandatory=True,
            ),
        ),
        sections=(
            _section(
                1,
                delivery_group_id=1,
                member_course_offering_ids=(11,),
                member_course_ids=(1,),
                semester=1,
                timeslot_id=101,
            ),
            _section(
                2,
                delivery_group_id=2,
                member_course_offering_ids=(12,),
                member_course_ids=(2,),
                semester=2,
                timeslot_id=201,
            ),
        ),
        timeslots=(
            TimeSlotDTO(101, 1, 1, "A", True),
            TimeSlotDTO(201, 1, 2, "A", True),
        ),
        section_utilization_balance_importance="not_important",
        student_semester_balance_importance="important",
        course_sequence_preferences_importance="not_important",
        difficulty_balance_importance="not_important",
        course_category_diversity_importance="not_important",
    )


def test_substantive_probe_finds_complete_schedule_below_requested_threshold():
    result = run_substantive_soft_tier_probe(
        _substantive_probe_input(),
        threshold=1,
        time_limit_seconds=5.0,
        worker_count=1,
    )

    assert result.status in {"feasible", "optimal"}
    assert result.seed_validated is True
    assert result.complete_candidate_found is True
    assert result.candidate_substantive_value == 0.0
    assert result.candidate_assignment_count == 2
    assert result.component_deltas["student_semester_balance_penalty"] == 0.0


def test_substantive_probe_reports_infeasible_threshold_without_partial_seed():
    result = run_substantive_soft_tier_probe(
        _substantive_probe_input(),
        threshold=-1,
        time_limit_seconds=5.0,
        worker_count=1,
    )

    assert result.status == "infeasible"
    assert result.seed_validated is True
    assert result.complete_candidate_found is False
    assert result.candidate_substantive_value is None


def test_substantive_probe_can_derive_strict_threshold_from_seed():
    result = run_substantive_soft_tier_probe(
        _substantive_probe_input(),
        threshold=None,
        strict_improvement=True,
        time_limit_seconds=5.0,
        worker_count=1,
    )

    assert result.seed_validated is True
    assert result.requested_threshold == result.baseline_substantive_value - 1


def test_substantive_probe_zero_neighborhood_preserves_seed_source_decisions():
    result = run_substantive_soft_tier_probe(
        _substantive_probe_input(),
        threshold=1,
        time_limit_seconds=5.0,
        worker_count=1,
        neighborhood_radius=0,
    )

    assert result.status in {"feasible", "optimal"}
    assert result.neighborhood_radius == 0
    assert result.changed_source_decision_count == 0
    assert result.source_decision_deltas == ()


def test_substantive_probe_records_semantic_candidate_and_impact_facts():
    result = run_substantive_soft_tier_probe(
        _substantive_probe_input(),
        threshold=1,
        time_limit_seconds=5.0,
        worker_count=1,
    )

    assert result.seed_source_decisions
    assert result.seed_source_variable_values
    assert result.candidate_source_decisions
    assert result.candidate_quality_summary["difficulty_balance"]["solver_aligned_penalty"] == 0
    assert result.quality_comparison["difficulty_balance"]["unchanged"] >= 1
    assert result.seed_summary["hard_valid"] is True
    assert result.candidate_summary["hard_valid"] is True
    assert result.candidate_summary["fulfillment_complete"] is True
    assert isinstance(result.affected_student_ids, tuple)
    assert isinstance(result.section_load_deltas, dict)


def test_substantive_probe_quality_comparison_uses_detached_seed_context():
    """Diagnostic quality deltas are relative to the supplied seed, not a new bootstrap."""

    data = _substantive_probe_input()
    seed = run_substantive_soft_tier_probe(
        data,
        threshold=1,
        time_limit_seconds=5.0,
        worker_count=1,
    )
    replay = run_substantive_soft_tier_probe(
        data,
        threshold=1,
        time_limit_seconds=5.0,
        worker_count=1,
        alternate_source_decisions=seed.seed_source_decisions,
        alternate_source_variable_values=seed.seed_source_variable_values,
        hard_feasibility_time_limit_seconds=0.1,
        hard_feasibility_validation_time_limit_seconds=5.0,
        hard_feasibility_worker_count=1,
        hard_feasibility_validation_worker_count=1,
    )

    assert replay.complete_candidate_found is True
    assert replay.quality_comparison["request_fulfillment"]["worsened"] == 0


def test_substantive_probe_can_minimize_one_existing_component():
    result = run_substantive_soft_tier_probe(
        _substantive_probe_input(),
        threshold=None,
        time_limit_seconds=5.0,
        worker_count=1,
        minimize_component="student_semester_balance_penalty",
    )

    assert result.status in {"feasible", "optimal"}
    assert result.minimized_component == "student_semester_balance_penalty"
    assert result.minimized_component_value == 0.0
    assert result.best_bound == 0.0


def test_substantive_probe_preserves_special_commitment_completion():
    study_slot = TimeSlotDTO(301, 1, 1, "B", True)
    result = run_substantive_soft_tier_probe(
        _input(
            requests=(),
            sections=(),
            timeslots=(study_slot,),
            schedule_commitment_requests=(
                StudentScheduleCommitmentRequestDTO(
                    request_id=91,
                    student_id=1,
                    commitment_type="study",
                ),
            ),
            section_utilization_balance_importance="not_important",
            student_semester_balance_importance="important",
            course_sequence_preferences_importance="not_important",
            difficulty_balance_importance="not_important",
            course_category_diversity_importance="not_important",
        ),
        threshold=1,
        time_limit_seconds=5.0,
        worker_count=1,
    )

    assert result.seed_validated is True
    assert result.complete_candidate_found is True
    assert result.candidate_assignment_count == 1


def test_adaptive_bootstrap_preserves_special_commitment_completion():
    study_slot = TimeSlotDTO(301, 1, 1, "B", True)
    result = student_assignment_module.run_student_assignment_adaptive_local_bootstrap_diagnostic(
        _input(
            requests=(),
            sections=(),
            timeslots=(study_slot,),
            schedule_commitment_requests=(
                StudentScheduleCommitmentRequestDTO(
                    request_id=92,
                    student_id=1,
                    commitment_type="study",
                ),
            ),
            section_utilization_balance_importance="not_important",
            student_semester_balance_importance="important",
            course_sequence_preferences_importance="not_important",
            difficulty_balance_importance="not_important",
            course_category_diversity_importance="not_important",
        ),
        neighborhood_radii=(0,),
        max_iterations=1,
        per_probe_time_limit_seconds=0.5,
        total_time_limit_seconds=5.0,
        worker_count=1,
    )

    assert result.status == "complete"
    assert len(result.unmet_requests) == 0
    assert len(result.commitment_assignments) == 1


def test_substantive_probe_reports_unknown_as_inconclusive(monkeypatch):
    class UnknownSolver:
        def Solve(self, _model):
            return cp_model.UNKNOWN

        def NumConflicts(self):
            return 7

        def NumBranches(self):
            return 11

    monkeypatch.setattr(
        "scheduling_engine.student_assignment.substantive_probe.new_solver",
        lambda *_args, **_kwargs: UnknownSolver(),
    )
    result = run_substantive_soft_tier_probe(
        _substantive_probe_input(),
        threshold=1,
        time_limit_seconds=5.0,
        worker_count=1,
    )

    assert result.status == "unknown"
    assert result.complete_candidate_found is False
    assert result.seed_validated is True


def test_bounded_counterfactual_can_honor_its_short_feasibility_limit():
    """Review evidence must not inherit the production bootstrap floor."""

    captured = {}

    def fake_seed(model, required_groups, time_limit_seconds, **kwargs):
        captured["seed_time_limit"] = time_limit_seconds
        captured["seed_workers"] = kwargs["worker_count"]
        return model.Clone(), None, (), cp_model.UNKNOWN

    def fake_validate(model, seed_model, seed_solver, source_indexes, time_limit_seconds, **kwargs):
        captured["validation_time_limit"] = time_limit_seconds
        captured["validation_workers"] = kwargs["worker_count"]
        return None

    with patch.object(
        student_assignment_module,
        "_solve_complete_hard_feasibility_seed",
        side_effect=fake_seed,
    ), patch.object(
        student_assignment_module,
        "_validate_complete_hard_feasibility_seed",
        side_effect=fake_validate,
    ), patch.object(
        student_assignment_module,
        "_solve_lexicographically",
        return_value=(None, cp_model.UNKNOWN),
    ):
        result = student_assignment_module.solve_student_assignment(
            _input(time_limit_seconds=0.25),
            use_hard_feasibility_bootstrap=False,
        )

    assert result.status == "failed"
    assert captured == {
        "seed_time_limit": 0.25,
        "seed_workers": 8,
        "validation_time_limit": 0.25,
        "validation_workers": 8,
    }


def test_result_records_validated_seed_and_optimization_quality_facts():
    """The result proves the two-stage handoff without changing assignments."""

    result = solve_student_assignment(_input())

    facts = result.optimization_facts
    assert facts["stage_1"]["complete_seed_produced"] is True
    assert facts["stage_1"]["seed_validated_against_full_model"] is True
    assert facts["stage_2"]["validated_seed_received"] is True
    assert facts["stage_2"]["worker_count"] == 8
    assert facts["stage_2"]["time_limit_seconds"] == 1800.0
    assert tuple(facts["stage_2"]["objective_values"]) <= tuple(
        facts["stage_1"]["objective_values"]
    )
    assert all(
        "starting_quality" in item and "ending_quality" in item
        for item in facts["optimization_passes"]
    )
    assert facts["stage_1"]["timings"]["seed_external_wall_time_seconds"] >= 0
    assert facts["stage_1"]["timings"]["validation_external_wall_time_seconds"] >= 0
    assert facts["stage_2"]["operation_wall_time_seconds"] >= 0
    assert facts["stage_2"]["configured_deadline_seconds"] == 1800.0
    assert facts["full_model_variable_count"] > 0
    assert facts["full_model_constraint_count"] > 0
    assert facts["model_family_variable_counts"]
    assert any(
        item["kind"] == "soft_tier"
        for item in facts["objective_metadata"]
    )
    assert len(facts["input_semantic_fingerprint"]) == 64


def test_stage2_diagnostic_trace_records_seed_hint_and_objective_metadata():
    result = student_assignment_module.run_student_assignment_stage2_diagnostic(
        _substantive_probe_input()
    )

    trace = result.optimization_facts["stage_2_trace"]
    assert trace
    assert trace[0]["hint_source"] == "validated_seed"
    assert trace[0]["hinted_variable_count"] > 0
    assert all("objective_name" in item for item in trace)


def test_stage2_diagnostic_records_bounded_incumbent_timeline():
    result = student_assignment_module.run_student_assignment_stage2_diagnostic(
        _substantive_probe_input(),
        total_time_limit_seconds=5.0,
        timeline_max_events=16,
    )

    timeline = result.optimization_facts["stage_2"]["incumbent_timeline"]
    assert len(timeline) <= 16
    assert timeline
    assert all(
        "objective_index" in item
        and "elapsed_solver_seconds" in item
        and "elapsed_stage_2_wall_seconds" in item
        and "objective_vector" in item
        and "best_bound" in item
        for item in timeline
    )
    stage_elapsed = [
        item["elapsed_stage_2_wall_seconds"]
        for item in timeline
    ]
    assert stage_elapsed == sorted(stage_elapsed)


def test_stage2_diagnostic_can_replay_a_validated_alternate_incumbent():
    data = _substantive_probe_input()
    probe = run_substantive_soft_tier_probe(
        data,
        threshold=1,
        time_limit_seconds=5.0,
        worker_count=1,
    )

    result = student_assignment_module.run_student_assignment_stage2_diagnostic(
        data,
        alternate_source_decisions=probe.candidate_source_decisions,
        alternate_source_variable_values=probe.candidate_source_variable_values,
    )

    assert result.status == "complete"
    assert result.optimization_facts["stage_2"]["alternate_seed_validated"] is True
    first_pass = result.optimization_facts["stage_2_trace"][0]
    assert first_pass["entering_candidate"]["hard_valid"] is True
    assert first_pass["entering_candidate"]["fulfillment_complete"] is True
    assert first_pass["returned_candidate"]["objective_vector"]


def test_stage1_seed_snapshot_round_trip_is_versioned_and_input_bound(tmp_path):
    data = _substantive_probe_input()
    probe = run_substantive_soft_tier_probe(
        data,
        threshold=None,
        time_limit_seconds=5.0,
        worker_count=1,
    )
    assert probe.seed_validated is True

    seed = {
        "seed_objective_vector": probe.seed_objective_vector,
        "seed_source_decisions": probe.seed_source_decisions,
    }
    path = tmp_path / "stage1-seed.json"
    payload = write_stage1_seed_snapshot(
        path,
        data=data,
        input_fingerprint=semantic_student_assignment_input_fingerprint(data),
        seed=seed,
    )
    loaded = read_stage1_seed_snapshot(
        path,
        data=data,
        expected_input_fingerprint=semantic_student_assignment_input_fingerprint(data),
    )

    assert payload["schema"] == "student_assignment_stage1_seed_v1"
    assert loaded["seed_objective_vector"] == tuple(probe.seed_objective_vector)
    assert loaded["seed_source_decisions"] == tuple(probe.seed_source_decisions)
    assert loaded["seed_source_decision_fingerprint"] == (
        semantic_stage1_seed_source_fingerprint(data, probe.seed_source_decisions)
    )


def test_stage1_seed_snapshot_preserves_co_op_commitment_source_namespace(tmp_path):
    data = _input(
        requests=(_request(
            74,
            course_id=9,
            course_offering_id=99,
            delivery_kind="co_op",
            credit_value=2.0,
        ),),
        sections=(),
        timeslots=_timeslots(),
        special_commitment_locks=(StudentSpecialCommitmentLockDTO(
            lock_id=3,
            lock_type="co_op_time",
            lock_mode="exact",
            course_request_id=74,
            semester=1,
            co_op_block_pair="a_b",
        ),),
    )
    probe = run_substantive_soft_tier_probe(
        data,
        threshold=None,
        time_limit_seconds=5.0,
        worker_count=1,
    )
    assert probe.seed_validated is True
    assert any(
        key == ("commitment", 74)
        and value[1] == "co_op"
        and value[2] == 74
        for key, value in probe.seed_source_decisions
    )

    path = tmp_path / "co-op-stage1-seed.json"
    write_stage1_seed_snapshot(
        path,
        data=data,
        input_fingerprint=semantic_student_assignment_input_fingerprint(data),
        seed={
            "seed_objective_vector": probe.seed_objective_vector,
            "seed_source_decisions": probe.seed_source_decisions,
        },
    )
    loaded = read_stage1_seed_snapshot(
        path,
        data=data,
        expected_input_fingerprint=semantic_student_assignment_input_fingerprint(data),
    )

    assert loaded["seed_source_decisions"] == tuple(probe.seed_source_decisions)
    replay = student_assignment_module.run_student_assignment_stage2_diagnostic(
        data,
        alternate_source_decisions=loaded["seed_source_decisions"],
        total_time_limit_seconds=5.0,
        optimization_worker_count=1,
        hard_feasibility_time_limit_seconds=5.0,
        hard_feasibility_validation_time_limit_seconds=5.0,
        hard_feasibility_worker_count=1,
        hard_feasibility_validation_worker_count=1,
        collect_incumbent_timeline=False,
    )
    assert replay.optimization_facts["stage_2"]["alternate_seed_validated"] is True


def test_student_assignment_input_snapshot_is_versioned_and_fingerprint_bound(tmp_path):
    data = _substantive_probe_input()
    fingerprint = semantic_student_assignment_input_fingerprint(data)
    path = tmp_path / "student-assignment-input.json"

    payload = write_student_assignment_input_snapshot(
        path,
        data=data,
        input_fingerprint=fingerprint,
    )
    loaded = read_student_assignment_input_snapshot(
        path,
        expected_input_fingerprint=fingerprint,
    )

    assert payload["schema"] == "student_assignment_input_v1"
    assert loaded["data"] == data
    assert loaded["input_semantic_fingerprint"] == fingerprint


def test_durable_stage2_benchmark_round_trip_verifies_manifest_and_gzip_artifacts(tmp_path):
    data = _substantive_probe_input()
    probe = run_substantive_soft_tier_probe(
        data,
        threshold=None,
        time_limit_seconds=5.0,
        worker_count=1,
    )
    benchmark_dir = tmp_path / "production-scale-v1"
    manifest = write_durable_stage2_benchmark(
        benchmark_dir,
        data=data,
        seed={
            "input_semantic_fingerprint": semantic_student_assignment_input_fingerprint(data),
            "seed_objective_vector": probe.seed_objective_vector,
            "seed_component_values": probe.seed_component_values,
            "seed_assignment_count": probe.seed_assignment_count,
            "seed_validated": probe.seed_validated,
            "seed_source_decisions": probe.seed_source_decisions,
            "seed_summary": probe.seed_summary,
        },
        metadata={"benchmark_name": "test-production-scale-v1"},
    )

    loaded = read_durable_stage2_benchmark(benchmark_dir)

    assert manifest["benchmark_schema"] == "student_assignment_stage2_benchmark_v1"
    assert loaded["data"] == data
    assert loaded["seed"]["seed_source_decisions"] == tuple(
        probe.seed_source_decisions
    )
    assert loaded["manifest"]["counts"]["student_count"] == 1
    assert (benchmark_dir / "input.json.gz").read_bytes()[:2] == b"\x1f\x8b"
    assert (benchmark_dir / "stage1_seed.json.gz").read_bytes()[:2] == b"\x1f\x8b"
    replay = replay_durable_stage1_seed(
        benchmark_dir,
        validation_time_limit_seconds=5.0,
        validation_worker_count=1,
    )
    assert replay["status"] == "complete"
    assert replay["seed_validated_against_full_model"] is True


def test_compact_substantive_probe_record_is_bounded_and_jsonl_persisted(tmp_path):
    data = _substantive_probe_input()
    probe = run_substantive_soft_tier_probe(
        data,
        threshold=None,
        strict_improvement=True,
        time_limit_seconds=5.0,
        worker_count=1,
    )
    record = compact_substantive_probe_record(
        probe,
        experiment_id="test-r2",
        input_semantic_fingerprint=semantic_student_assignment_input_fingerprint(data),
        seed_source_decision_fingerprint="seed-test",
        radius=2,
        configured_time_limit_seconds=5.0,
        configured_worker_count=1,
    )
    path = tmp_path / "experiments.jsonl"
    append_experiment_record(path, record)

    stored = path.read_text(encoding="utf-8").strip()
    assert stored.count("\n") == 0
    assert '"experiment_id": "test-r2"' in stored
    assert record["candidate_adopted"] is False
    assert "raw_solver_candidate" not in stored


def test_local_bootstrap_diagnostic_consumes_shared_budget_and_keeps_complete_seed():
    result = student_assignment_module.run_student_assignment_local_bootstrap_diagnostic(
        _substantive_probe_input(),
        neighborhood_radius=0,
        time_limit_seconds=1.0,
        total_time_limit_seconds=5.0,
        worker_count=1,
    )

    bootstrap = result.optimization_facts["stage_2_local_bootstrap"]
    assert bootstrap["time_limit_seconds"] == 1.0
    assert bootstrap["status"] in {"optimal", "feasible", "infeasible", "unknown"}
    assert "affected_student_ids" in bootstrap
    assert "affected_section_ids" in bootstrap
    assert "section_load_deltas" in bootstrap
    assert result.status == "complete"
    assert len(result.unmet_requests) == 0
    assert result.optimization_facts["stage_2"]["validated_seed_received"] is True


def test_adaptive_local_bootstrap_restarts_and_records_bounded_iterations():
    result = student_assignment_module.run_student_assignment_adaptive_local_bootstrap_diagnostic(
        _substantive_probe_input(),
        neighborhood_radii=(0, 1),
        max_iterations=2,
        per_probe_time_limit_seconds=0.5,
        total_time_limit_seconds=5.0,
        worker_count=1,
    )

    facts = result.optimization_facts["stage_2_local_bootstrap"]
    assert facts["adaptive"] is True
    assert 1 <= len(facts["iterations"]) <= 2
    assert all(
        item["radius"] in {0, 1}
        and "incumbent_before" in item
        and "candidate_validated" in item
        and "iteration_requested_time_limit_seconds" in item
        and "iteration_remaining_seconds" in item
        and "probe_timings" in item
        for item in facts["iterations"]
    )
    assert result.status == "complete"
    assert len(result.unmet_requests) == 0


def test_adaptive_local_bootstrap_accepts_alternate_semantic_seed():
    data = _substantive_probe_input()
    seed = run_substantive_soft_tier_probe(
        data,
        threshold=1,
        time_limit_seconds=5.0,
        worker_count=1,
    )

    result = student_assignment_module.run_student_assignment_adaptive_local_bootstrap_diagnostic(
        data,
        neighborhood_radii=(0,),
        max_iterations=1,
        per_probe_time_limit_seconds=0.5,
        total_time_limit_seconds=5.0,
        worker_count=1,
        alternate_source_decisions=seed.seed_source_decisions,
        alternate_source_variable_values=seed.seed_source_variable_values,
    )

    assert result.optimization_facts["stage_2"]["alternate_seed_validated"] is True
    assert result.status == "complete"
    assert len(result.unmet_requests) == 0


def test_variable_neighborhood_diagnostic_records_bounded_radius_transitions():
    result = student_assignment_module.run_student_assignment_variable_neighborhood_diagnostic(
        _substantive_probe_input(),
        neighborhood_radii=(0, 1),
        max_iterations=2,
        max_attempts_by_radius={0: 1, 1: 1},
        per_probe_time_limit_seconds=0.5,
        total_time_limit_seconds=5.0,
        worker_count=1,
    )

    facts = result.optimization_facts["stage_2_local_bootstrap"]
    assert facts["adaptive"] is True
    assert facts["variable_neighborhood"] is True
    assert 1 <= len(facts["iterations"]) <= 2
    assert facts["stopping_reason"] in {
        "neighborhood_sequence_exhausted",
        "iteration_budget_exhausted",
        "shared_budget_exhausted",
    }
    assert facts["radius_attempts"]
    assert all("transition_reason" in item for item in facts["iterations"])
    assert result.status == "complete"
    assert len(result.unmet_requests) == 0


def test_monotonic_deadline_clamps_nested_allowances(monkeypatch):
    current = iter((104.0, 107.0, 107.0))
    monkeypatch.setattr(
        "scheduling_engine.student_assignment.runtime.monotonic",
        lambda: next(current),
    )

    deadline = MonotonicDeadline(10.0, started_at=100.0)

    assert deadline.requested_seconds == 10.0
    assert deadline.remaining() == 6.0
    assert deadline.allowance(20.0) == 3.0
    assert deadline.allowance(1.0) == 1.0


def test_semantic_input_fingerprint_ignores_fresh_database_ids():
    first = _input(
        timeslots=(TimeSlotDTO(101, 1, 1, "A", True),),
    )
    second = _input(
        academic_year_id=77,
        requests=(_request(
            request_id=9001,
            student_id=700,
            course_id=500,
            course_offering_id=5011,
        ),),
        sections=(_section(
            section_id=800,
            delivery_group_id=400,
            member_course_offering_ids=(5011,),
            member_course_ids=(500,),
            timeslot_id=9101,
        ),),
        timeslots=(TimeSlotDTO(9101, 77, 1, "A", True),),
    )

    assert semantic_student_assignment_input_fingerprint(first) == (
        semantic_student_assignment_input_fingerprint(second)
    )
    changed = _input(
        timeslots=(TimeSlotDTO(101, 1, 1, "B", True),),
        sections=(_section(timeslot_id=101),),
    )
    assert semantic_student_assignment_input_fingerprint(first) != (
        semantic_student_assignment_input_fingerprint(changed)
    )


def test_lexicographic_budget_is_shared_across_objective_passes():
    """A global offline budget cannot be multiplied by objective-tier count."""

    model = cp_model.CpModel()
    value = model.NewIntVar(0, 1, "value")
    captured_limits = []
    original_new_solver = student_assignment_solver.new_solver

    def capture_new_solver(time_limit_seconds, **kwargs):
        captured_limits.append(time_limit_seconds)
        return original_new_solver(time_limit_seconds, **kwargs)

    with patch.object(
        student_assignment_solver,
        "new_solver",
        side_effect=capture_new_solver,
    ):
        solver, outcome = student_assignment_solver.solve_lexicographically(
            model,
            (value, value),
            10.0,
            total_time_limit_seconds=0.2,
        )

    assert solver is not None
    assert outcome == cp_model.FEASIBLE
    assert len(captured_limits) == 2
    assert all(0 < limit <= 0.2 for limit in captured_limits)


def test_lexicographic_solver_honors_one_shared_monotonic_deadline():
    model = cp_model.CpModel()
    value = model.NewIntVar(0, 1, "value")
    captured_limits = []
    original_new_solver = student_assignment_solver.new_solver

    def capture_new_solver(time_limit_seconds, **kwargs):
        captured_limits.append(time_limit_seconds)
        return original_new_solver(time_limit_seconds, **kwargs)

    with patch.object(
        student_assignment_solver,
        "new_solver",
        side_effect=capture_new_solver,
    ):
        solver, outcome = student_assignment_solver.solve_lexicographically(
            model,
            (value, value),
            10.0,
            total_time_limit_seconds=10.0,
            deadline=MonotonicDeadline.start(0.2),
        )

    assert solver is not None
    assert outcome == cp_model.FEASIBLE
    assert len(captured_limits) == 2
    assert all(0 < limit <= 0.2 for limit in captured_limits)


def test_optional_incumbent_retention_uses_the_full_existing_objective_vector():
    """Retention never prefers a candidate that is lexicographically worse."""

    class Candidate:
        def __init__(self, values):
            self.values = values

        def Value(self, objective):
            return self.values[objective]

    objectives = ("fulfillment", "substantive", "tie_break")
    incumbent = Candidate({"fulfillment": 0, "substantive": 100, "tie_break": 5})

    # A strictly better active/lower-priority vector is safe to adopt.
    assert student_assignment_solver._candidate_is_lexicographically_better(
        Candidate({"fulfillment": 0, "substantive": 99, "tie_break": 999}),
        incumbent,
        objectives,
    ) is True

    # Equal current-tier values may still use a measured lower-tier
    # improvement; no invented aggregate score is involved.
    assert student_assignment_solver._candidate_is_lexicographically_better(
        Candidate({"fulfillment": 0, "substantive": 100, "tie_break": 4}),
        incumbent,
        objectives,
    ) is True

    # A lower-priority improvement cannot compensate for a worse higher tier.
    assert student_assignment_solver._candidate_is_lexicographically_better(
        Candidate({"fulfillment": 0, "substantive": 101, "tie_break": 0}),
        incumbent,
        objectives,
    ) is False


def _difficulty(course_id, score, category="math"):
    return CourseDifficultyDTO(
        course_id=course_id,
        category=category,
        calculated_difficulty=score,
        manual_difficulty_override=None,
        effective_difficulty=score,
        calculation_version="test_v1",
    )


def _semester_choice_sections():
    return (
        _section(1, semester=1, timeslot_id=101),
        _section(2, semester=2, timeslot_id=201),
        _section(3, delivery_group_id=2, member_course_offering_ids=(22,), member_course_ids=(2,), semester=1, timeslot_id=102),
        _section(4, delivery_group_id=2, member_course_offering_ids=(22,), member_course_ids=(2,), semester=2, timeslot_id=202),
    )


def test_assigns_primary_to_accepted_section_deterministically():
    first = solve_student_assignment(_input())
    second = solve_student_assignment(_input())

    assert first.status == "complete"
    assert first.assignments == second.assignments
    assert first.assignments[0].section_id == 1


def test_fixed_enrollment_blocks_student_timeslot_and_consumes_capacity():
    fixed = FixedEnrollmentDTO(
        student_id=1, section_id=2, course_offering_id=22, course_id=2,
        semester=1, timeslot_id=101,
    )
    result = solve_student_assignment(_input(
        sections=(_section(), _section(2, delivery_group_id=2, member_course_offering_ids=(22,), member_course_ids=(2,), timeslot_id=101, capacity_max=1)),
        fixed_enrollments=(fixed,),
    ))

    assert result.status == "partial"
    assert result.unmet_requests[0].diagnostic_code == "student_assignment_timeslot_collision"


def test_combined_section_has_shared_physical_capacity():
    result = solve_student_assignment(_input(
        requests=(
            _request(1, student_id=1, course_id=1, course_offering_id=11),
            _request(2, student_id=2, course_id=2, course_offering_id=22),
        ),
        sections=(_section(
            member_course_offering_ids=(11, 22), member_course_ids=(1, 2),
            capacity_max=1,
        ),),
    ))

    assert result.status == "partial"
    assert len(result.assignments) == 1


def test_hard_same_year_prerequisite_requires_semester_one_then_two():
    result = solve_student_assignment(_input(
        requests=(
            _request(1, course_id=1, course_offering_id=11),
            _request(2, course_id=2, course_offering_id=22),
        ),
        sections=(
            _section(1, semester=1, timeslot_id=101),
            _section(2, delivery_group_id=2, member_course_offering_ids=(22,), member_course_ids=(2,), semester=2, timeslot_id=202),
        ),
        hard_prerequisites=(CoursePrerequisiteDTO(course_id=2, prerequisite_id=1),),
    ))

    assert result.status == "complete"
    assert {(row.course_id, row.semester) for row in result.assignments} == {(1, 1), (2, 2)}


def test_soft_sequence_is_reported_when_both_courses_apply():
    result = solve_student_assignment(_input(
        requests=(
            _request(1, course_id=1, course_offering_id=11),
            _request(2, course_id=2, course_offering_id=22),
        ),
        sections=(
            _section(1, semester=1, timeslot_id=101),
            _section(2, delivery_group_id=2, member_course_offering_ids=(22,), member_course_ids=(2,), semester=2, timeslot_id=202),
        ),
        soft_sequence_preferences=(CourseSequencePreferenceDTO(earlier_course_id=1, later_course_id=2),),
    ))

    assert result.status == "complete"
    assert result.sequence_outcomes == ({
        "student_id": 1, "earlier_course_id": 1, "later_course_id": 2, "satisfied": True,
    },)


def test_difficulty_balance_prefers_a_less_imbalanced_semester_split():
    """Difficulty changes a soft preference only after request fulfillment."""

    result = solve_student_assignment(_input(
        requests=(
            _request(1, course_id=1, course_offering_id=11),
            _request(2, course_id=2, course_offering_id=22),
        ),
        sections=_semester_choice_sections(),
        section_utilization_balance_importance="not_important",
        student_semester_balance_importance="not_important",
        course_sequence_preferences_importance="not_important",
        difficulty_balance_importance="important",
        course_difficulties=(_difficulty(1, 80), _difficulty(2, 20, "science")),
    ))

    by_request = {item.request_id: item for item in result.assignments}
    assert result.status == "complete"
    assert by_request[1].semester != by_request[2].semester
    assert result.objective_components["difficulty_balance_penalty"] == 60


def test_category_diversity_splits_repeated_categories_when_feasible():
    result = solve_student_assignment(_input(
        requests=(
            _request(1, course_id=1, course_offering_id=11),
            _request(2, course_id=2, course_offering_id=22),
        ),
        sections=_semester_choice_sections(),
        section_utilization_balance_importance="not_important",
        student_semester_balance_importance="not_important",
        course_sequence_preferences_importance="not_important",
        course_category_diversity_importance="important",
        course_difficulties=(_difficulty(1, 50, "math"), _difficulty(2, 50, "math")),
    ))

    by_request = {item.request_id: item for item in result.assignments}
    assert by_request[1].semester != by_request[2].semester
    assert result.objective_components["course_category_diversity_penalty"] == 0


def test_difficulty_and_category_importance_resolve_a_real_soft_preference_tradeoff():
    """Counselor labels, rather than exposed weights, decide the winning tier."""

    fixed = FixedEnrollmentDTO(
        student_id=1, section_id=5, course_offering_id=33, course_id=3,
        semester=1, timeslot_id=103,
    )
    sections = _semester_choice_sections() + (
        _section(5, delivery_group_id=3, member_course_offering_ids=(33,), member_course_ids=(3,), semester=1, timeslot_id=103),
    )
    common = dict(
        requests=(
            _request(1, course_id=1, course_offering_id=11),
            _request(2, course_id=2, course_offering_id=22),
        ),
        sections=sections,
        fixed_enrollments=(fixed,),
        section_utilization_balance_importance="not_important",
        student_semester_balance_importance="not_important",
        course_sequence_preferences_importance="not_important",
        course_difficulties=(
            _difficulty(1, 90, "math"),
            _difficulty(2, 10, "math"),
            _difficulty(3, 100, "science"),
        ),
    )
    difficulty_first = solve_student_assignment(_input(
        **common,
        difficulty_balance_importance="extremely_important",
        course_category_diversity_importance="important",
    ))
    category_first = solve_student_assignment(_input(
        **common,
        difficulty_balance_importance="important",
        course_category_diversity_importance="extremely_important",
    ))

    assert {item.semester for item in difficulty_first.assignments} == {2}
    assert {item.semester for item in category_first.assignments} == {1, 2}
    assert difficulty_first.objective_components["difficulty_balance_penalty"] == 0
    assert category_first.objective_components["course_category_diversity_penalty"] == 0


def test_category_diversity_never_overrides_a_hard_semester_constraint():
    result = solve_student_assignment(_input(
        requests=(
            _request(1, course_id=1, course_offering_id=11),
            _request(2, course_id=2, course_offering_id=22),
        ),
        sections=(
            _section(1, semester=1, timeslot_id=101),
            _section(2, delivery_group_id=2, member_course_offering_ids=(22,), member_course_ids=(2,), semester=1, timeslot_id=102),
        ),
        section_utilization_balance_importance="not_important",
        student_semester_balance_importance="not_important",
        course_sequence_preferences_importance="not_important",
        course_category_diversity_importance="extremely_important",
        course_difficulties=(_difficulty(1, 50, "math"), _difficulty(2, 50, "math")),
    ))

    assert result.status == "complete"
    assert {item.semester for item in result.assignments} == {1}
    assert result.objective_components["course_category_diversity_penalty"] == 100


def test_locked_active_enrollment_cannot_be_moved_in_a_rerun():
    result = solve_student_assignment(_input(
        sections=(
            _section(1, capacity_max=1),
            _section(2, delivery_group_id=2, timeslot_id=202),
        ),
        fixed_enrollments=(FixedEnrollmentDTO(
            enrollment_id=101,
            student_id=1,
            section_id=1,
            course_offering_id=11,
            course_id=1,
            semester=1,
            timeslot_id=101,
            is_locked=True,
            is_in_scope=True,
            lock_ids=(41,),
        ),),
    ))

    assert result.assignments == ()
    assert result.unmet_requests[0].diagnostic_code == "student_assignment_locked_enrollment_blocks_request"
    assert result.unmet_requests[0].blocking_lock_id == 41


def test_group_lock_assigns_all_members_to_one_section_or_none():
    result = solve_student_assignment(_input(
        requests=(
            _request(1, student_id=1),
            _request(2, student_id=2, is_in_scope=False),
        ),
        sections=(
            _section(1, capacity_max=1),
            _section(2, delivery_group_id=2, timeslot_id=202, capacity_max=2),
        ),
        student_assignment_locks=(StudentAssignmentLockDTO(
            lock_id=51,
            lock_type="student_group_same_section",
            course_id=1,
            member_student_ids=(1, 2),
        ),),
    ))

    assert result.status == "complete"
    assert {row.section_id for row in result.assignments} == {2}


def test_priority_request_beats_ordinary_primary_for_one_remaining_seat():
    result = solve_student_assignment(_input(
        requests=(
            _request(1, student_id=1),
            _request(2, student_id=2),
        ),
        sections=(_section(capacity_max=1),),
        priority_request_ids=(2,),
        priority_request_limit=100,
    ))

    assert [row.request_id for row in result.assignments] == [2]
    assert result.objective_components["priority_primary_fulfilled"] == 1


def test_strong_schedule_preservation_penalizes_a_move_from_current_enrollment():
    movable = FixedEnrollmentDTO(
        enrollment_id=71,
        student_id=1,
        section_id=2,
        course_offering_id=11,
        course_id=1,
        semester=1,
        timeslot_id=202,
        is_in_scope=True,
    )
    values = dict(
        sections=(
            _section(1, capacity_max=2),
            _section(2, delivery_group_id=2, timeslot_id=202, capacity_max=2),
        ),
        fixed_enrollments=(movable,),
        section_utilization_balance_importance="not_important",
        student_semester_balance_importance="not_important",
        course_sequence_preferences_importance="not_important",
    )

    without_preservation = solve_student_assignment(_input(**values))
    with_strong_preservation = solve_student_assignment(_input(
        **values,
        schedule_preservation_level="strong",
    ))

    assert without_preservation.assignments[0].section_id == 1
    assert with_strong_preservation.assignments[0].section_id == 2
    assert with_strong_preservation.objective_components["schedule_preservation_move_penalty"] == 0


def test_unresolved_request_includes_a_stable_structured_reason_and_remediation():
    result = solve_student_assignment(_input(
        requests=(_request(course_id=9, course_offering_id=99),),
    ))

    unmet = result.unmet_requests[0]
    assert unmet.diagnostic_code == "student_assignment_no_active_placed_section"
    assert unmet.remediation_codes == ("student_assignment_requires_placed_section",)


def test_historical_enrollment_is_audit_context_not_capacity_or_timeslot_context():
    result = solve_student_assignment(_input(
        fixed_enrollments=(FixedEnrollmentDTO(
            enrollment_id=99,
            student_id=1,
            section_id=1,
            course_offering_id=11,
            course_id=1,
            semester=1,
            timeslot_id=101,
            is_historical=True,
        ),),
        sections=(_section(capacity_max=1),),
    ))

    assert result.status == "complete"
    assert result.assignments[0].section_id == 1


def test_active_lock_cost_and_section_review_facts_are_returned():
    result = solve_student_assignment(_input(
        requests=(
            _request(1, student_id=1),
            _request(2, student_id=2),
        ),
        sections=(
            _section(1, capacity_max=0, target_capacity=1),
            _section(2, delivery_group_id=2, timeslot_id=202, capacity_max=1, target_capacity=1),
        ),
        student_assignment_locks=(StudentAssignmentLockDTO(
            lock_id=61,
            lock_type="exact_student_section",
            student_id=1,
            course_id=1,
            section_id=1,
        ),),
    ))

    lock_cost = result.lock_costs[0]
    assert lock_cost.lock_id == 61
    assert lock_cost.unresolved_request_ids == (1,)
    assert lock_cost.attributable_request_count == 1
    assert result.seat_contention[0].section_id == 2
    assert result.seat_contention[0].competing_request_ids == (2,)
    assert result.section_balance_facts[0].diagnostic_code == "student_assignment_section_below_target_capacity"


def test_partial_scope_moves_only_in_scope_requests_and_preserves_out_of_scope_context():
    result = solve_student_assignment(_input(
        requests=(
            _request(1, student_id=1),
            _request(2, student_id=2),
        ),
        sections=(_section(1, capacity_max=1), _section(2, delivery_group_id=2, timeslot_id=202, capacity_max=1)),
        fixed_enrollments=(FixedEnrollmentDTO(
            enrollment_id=80, student_id=2, section_id=1, course_offering_id=11,
            course_id=1, semester=1, timeslot_id=101, is_in_scope=False,
        ),),
        scope=StudentAssignmentScopeDTO(
            scope_type="scoped", student_ids=(1,),
        ),
    ))

    assert {item.student_id for item in result.assignments} == {1}
    assert all(item.student_id != 2 for item in result.assignments)


def test_each_lock_type_is_a_hard_candidate_boundary():
    cases = (
        ("exact_student_section", {"student_id": 1, "course_id": 1, "section_id": 2}, 2),
        ("section_roster", {"section_id": 1}, 2),
        ("course_roster", {"course_id": 1}, None),
        ("whole_student_schedule", {"student_id": 1}, None),
        ("student_teacher_course", {"student_id": 1, "course_id": 1, "teacher_id": 7}, 2),
    )
    for lock_type, targets, expected_section in cases:
        result = solve_student_assignment(_input(
            sections=(_section(1, teacher_id=8), _section(2, delivery_group_id=2, timeslot_id=202, teacher_id=7)),
            student_assignment_locks=(StudentAssignmentLockDTO(
                lock_id=100 + len(lock_type), lock_type=lock_type, **targets,
            ),),
        ))
        if expected_section is None:
            assert result.assignments == ()
            assert result.unmet_requests[0].diagnostic_code == "student_assignment_locked_enrollment_blocks_request"
        else:
            assert result.assignments[0].section_id == expected_section


def test_all_schedule_preservation_levels_protect_a_current_movable_enrollment():
    movable = FixedEnrollmentDTO(
        enrollment_id=91, student_id=1, section_id=2, course_offering_id=11,
        course_id=1, semester=1, timeslot_id=202, is_in_scope=True,
    )
    for level in ("none", "slight", "moderate", "strong"):
        result = solve_student_assignment(_input(
            sections=(_section(1), _section(2, delivery_group_id=2, timeslot_id=202)),
            fixed_enrollments=(movable,),
            section_utilization_balance_importance="not_important",
            student_semester_balance_importance="not_important",
            course_sequence_preferences_importance="not_important",
            schedule_preservation_level=level,
        ))
        assert result.assignments
        if level == "none":
            assert result.assignments[0].section_id == 1
        else:
            assert result.assignments[0].section_id == 2


def test_teacher_lock_only_accepts_the_section_with_the_named_teacher():
    result = solve_student_assignment(_input(
        sections=(_section(1, teacher_id=7), _section(2, delivery_group_id=2, timeslot_id=202, teacher_id=8)),
        student_assignment_locks=(StudentAssignmentLockDTO(
            lock_id=201, lock_type="student_teacher_course", student_id=1,
            course_id=1, teacher_id=8,
        ),),
    ))

    assert result.assignments[0].section_id == 2


def test_unresolved_capacity_reason_identifies_the_competing_section_and_student():
    result = solve_student_assignment(_input(
        requests=(_request(1, student_id=1), _request(2, student_id=2)),
        sections=(_section(capacity_max=1),),
    ))

    unmet = next(item for item in result.unmet_requests if item.request_id == 2)
    assert unmet.diagnostic_code == "student_assignment_section_capacity_exhausted"
    assert unmet.blocking_section_id == 1
    assert unmet.blocking_student_id == 1


def test_candidate_ledger_records_final_capacity_elimination_without_a_second_solve():
    """A losing request retains its compatible section and final seat fact."""

    result = solve_student_assignment(_input(
        requests=(_request(1, student_id=1), _request(2, student_id=2)),
        sections=(_section(capacity_max=1),),
    ))

    ledger_by_request = {item.request_id: item for item in result.candidate_ledger}
    losing = ledger_by_request[2]
    assert losing.selection_state == "unresolved"
    assert losing.unresolved_reason_code == "student_assignment_section_capacity_exhausted"
    assert losing.static_candidate_count == 1
    assert losing.statically_eligible_candidate_count == 1
    assert losing.alternatives[0]["section_id"] == 1
    assert losing.alternatives[0]["final_rejections"] == [{
        "code": "student_assignment_section_capacity_exhausted",
        "phase": "final",
        "blocking_section_id": 1,
        "blocking_student_id": 1,
        "blocking_request_id": 1,
    }]


def test_candidate_ledger_bounds_rejected_options_but_reports_omission():
    """Target-scale evidence remains bounded even when a request has many choices."""

    result = solve_student_assignment(_input(
        sections=tuple(
            _section(index, timeslot_id=100 + index)
            for index in range(1, 9)
        ),
    ))

    ledger = result.candidate_ledger[0]
    assert ledger.static_candidate_count == 8
    assert ledger.recorded_rejected_candidate_count == 6
    assert ledger.omitted_rejected_candidate_count == 1
    assert ledger.selected_candidate["section_id"] == 1


def test_candidate_ledger_captures_study_focus_and_co_op_lock_eliminations():
    """Special commitments receive the same bounded evidence as course requests."""

    slots = _timeslots()
    study_slot = next(slot for slot in slots if slot.semester == 2 and slot.block == "C")
    result = solve_student_assignment(_input(
        requests=(_request(
            83, student_id=3, course_id=9, course_offering_id=99,
            delivery_kind="co_op", credit_value=2.0,
        ),),
        sections=(),
        timeslots=slots,
        schedule_commitment_requests=(
            StudentScheduleCommitmentRequestDTO(81, 1, "study"),
            StudentScheduleCommitmentRequestDTO(82, 2, "focus"),
        ),
        special_commitment_locks=(
            StudentSpecialCommitmentLockDTO(
                lock_id=81, lock_type="study_time", lock_mode="exact",
                schedule_commitment_request_id=81, timeslot_id=study_slot.id,
            ),
            StudentSpecialCommitmentLockDTO(
                lock_id=82, lock_type="focus_semester", lock_mode="exact",
                schedule_commitment_request_id=82, semester=1,
            ),
            StudentSpecialCommitmentLockDTO(
                lock_id=83, lock_type="co_op_time", lock_mode="exact",
                course_request_id=83, semester=1, co_op_block_pair="a_b",
            ),
        ),
    ))

    ledger_by_request = {item.request_id: item for item in result.candidate_ledger}
    for request_id, candidate_kind in ((81, "study_time"), (82, "focus_semester"), (83, "co_op_block_pair")):
        entry = ledger_by_request[request_id]
        assert entry.selection_state == "selected"
        assert entry.selected_candidate["candidate_kind"] == candidate_kind
        assert any(
            item["static_rejections"]
            and item["static_rejections"][0]["code"]
            == "student_assignment_special_commitment_lock_blocks_request"
            for item in entry.alternatives
        )


def test_requested_study_occupies_one_block_without_becoming_a_course_assignment():
    result = solve_student_assignment(_input(
        requests=(),
        sections=(),
        timeslots=_timeslots(),
        schedule_commitment_requests=(StudentScheduleCommitmentRequestDTO(
            request_id=71, student_id=1, commitment_type="study",
        ),),
    ))

    assert result.status == "complete"
    assert result.assignments == ()
    study = result.commitment_assignments[0]
    assert study.commitment_kind == "study"
    assert len(study.occupancy) == 2
    assert {segment for _timeslot_id, segment in study.occupancy} == {
        "first_half", "second_half",
    }


def test_study_difficulty_contribution_uses_occupied_timeslot_semester():
    """A timeslot identity must never be interpreted as a semester value."""

    result = solve_student_assignment(
        _input(
            requests=(),
            sections=(_section(timeslot_id=101, semester=1),),
            fixed_enrollments=(FixedEnrollmentDTO(
                enrollment_id=91,
                student_id=1,
                section_id=1,
                course_offering_id=11,
                course_id=1,
                semester=1,
                timeslot_id=101,
                is_locked=True,
            ),),
            timeslots=(
                TimeSlotDTO(101, 1, 1, "A"),
                # Deliberately make the Study slot ID unlike either semester.
                TimeSlotDTO(102, 1, 1, "B"),
            ),
            schedule_commitment_requests=(
                StudentScheduleCommitmentRequestDTO(2, 1, "study"),
            ),
            course_difficulties=(
                CourseDifficultyDTO(1, "math", 100, None, 100, "test"),
            ),
            difficulty_balance_importance="important",
            time_limit_seconds=2.0,
        ),
        use_hard_feasibility_bootstrap=False,
    )

    assert result.status == "complete"
    assert result.objective_components["difficulty_balance_penalty"] == 101
    assert result.optimization_facts["quality"]["stage_2"]["difficulty_balance"][
        "reconstruction_delta"
    ] == 0


def test_study_exact_lock_uses_the_counselor_selected_block():
    slots = _timeslots()
    locked_slot = next(slot for slot in slots if slot.semester == 2 and slot.block == "C")
    result = solve_student_assignment(_input(
        requests=(), sections=(), timeslots=slots,
        schedule_commitment_requests=(StudentScheduleCommitmentRequestDTO(
            request_id=72, student_id=1, commitment_type="study",
        ),),
        special_commitment_locks=(StudentSpecialCommitmentLockDTO(
            lock_id=1, lock_type="study_time", lock_mode="exact",
            schedule_commitment_request_id=72, timeslot_id=locked_slot.id,
            semester=2,
        ),),
    ))

    assert {timeslot_id for timeslot_id, _segment in result.commitment_assignments[0].occupancy} == {
        locked_slot.id,
    }


def test_focus_reserves_all_school_blocks_and_excludes_semester_balance():
    slots = _timeslots()
    first_semester_section = _section(timeslot_id=slots[0].id, semester=1)
    result = solve_student_assignment(_input(
        requests=(_request(1, student_id=1),),
        sections=(first_semester_section,),
        timeslots=slots,
        schedule_commitment_requests=(StudentScheduleCommitmentRequestDTO(
            request_id=73, student_id=1, commitment_type="focus",
        ),),
        special_commitment_locks=(StudentSpecialCommitmentLockDTO(
            lock_id=2, lock_type="focus_semester", lock_mode="exact",
            schedule_commitment_request_id=73, semester=2,
        ),),
    ))

    assert result.status == "complete"
    focus = result.commitment_assignments[0]
    assert focus.commitment_kind == "focus"
    assert {timeslot_id for timeslot_id, _segment in focus.occupancy} == {
        slot.id for slot in slots if slot.semester == 2
    }
    assert result.assignments[0].semester == 1
    assert result.objective_components["student_semester_balance_penalty"] == 0


def test_co_op_is_one_two_credit_paired_block_commitment_not_a_section_enrollment():
    slots = _timeslots()
    result = solve_student_assignment(_input(
        requests=(_request(
            74, course_id=9, course_offering_id=99, delivery_kind="co_op",
            credit_value=2.0,
        ),),
        sections=(), timeslots=slots,
        special_commitment_locks=(StudentSpecialCommitmentLockDTO(
            lock_id=3, lock_type="co_op_time", lock_mode="exact",
            course_request_id=74, semester=1, co_op_block_pair="a_b",
        ),),
    ))

    assert result.status == "complete"
    assert result.assignments == ()
    co_op = result.commitment_assignments[0]
    assert co_op.commitment_kind == "co_op"
    assert {timeslot_id for timeslot_id, _segment in co_op.occupancy} == {
        slot.id for slot in slots if slot.semester == 1 and slot.block in {"A", "B"}
    }


def test_half_semester_pair_can_share_a_block_without_a_student_collision():
    result = solve_student_assignment(_input(
        requests=(
            _request(
                75, course_id=1, course_offering_id=11, duration="half_semester",
                credit_value=0.5, half_semester_segment="first_half", paired_half_course_id=2,
            ),
            _request(
                76, course_id=2, course_offering_id=22, duration="half_semester",
                credit_value=0.5, half_semester_segment="second_half", paired_half_course_id=1,
            ),
        ),
        sections=(
            _section(
                1, timeslot_id=101, half_semester_segment="first_half",
                half_semester_pair_key="pair:1",
            ),
            _section(
                2, delivery_group_id=2, member_course_offering_ids=(22,), member_course_ids=(2,),
                timeslot_id=101, half_semester_segment="second_half",
                half_semester_pair_key="pair:1",
            ),
        ),
        section_utilization_balance_importance="not_important",
        student_semester_balance_importance="not_important",
        course_sequence_preferences_importance="not_important",
    ))

    assert result.status == "complete"
    assert {item.section_id for item in result.assignments} == {1, 2}
    assert result.review_items == ()


def test_unpaired_half_semester_course_is_assigned_then_flagged_for_review():
    result = solve_student_assignment(_input(
        requests=(_request(
            77, course_id=1, course_offering_id=11, duration="half_semester",
            credit_value=0.5, half_semester_segment="first_half", paired_half_course_id=2,
        ),),
        sections=(_section(half_semester_segment="first_half"),),
    ))

    assert result.status == "complete"
    assert result.review_items[0].code == "student_assignment_half_semester_unallocated_opposite_half"


def test_unallocated_school_time_is_reviewed_without_creating_study():
    """A genuine gap is review evidence, never an implicit student commitment."""

    slots = _timeslots()
    first_slot = next(slot for slot in slots if slot.semester == 1 and slot.block == "A")
    result = solve_student_assignment(_input(
        sections=(_section(timeslot_id=first_slot.id),),
        timeslots=slots,
    ))

    review_items = [
        item for item in result.review_items
        if item.code == "student_assignment_unallocated_school_time"
    ]
    assert review_items
    assert all(item.detail["recognized_commitment"] is False for item in review_items)
    assert all(item.detail["has_requested_study"] is False for item in review_items)
    assert all(item.detail["timeslot_id"] != first_slot.id for item in review_items)


def test_unallocated_school_time_does_not_duplicate_an_explicit_alternate_review():
    """An alternate is recorded demand context, not an inferred Study period."""

    slots = _timeslots()
    first_slot = next(slot for slot in slots if slot.semester == 1 and slot.block == "A")
    result = solve_student_assignment(_input(
        sections=(_section(timeslot_id=first_slot.id),),
        timeslots=slots,
        student_ids_with_alternate_requests=(1,),
    ))

    assert not any(
        item.code == "student_assignment_unallocated_school_time"
        for item in result.review_items
    )


def test_unpaired_half_course_uses_its_specific_review_instead_of_generic_gap():
    """Counselors should see one precise half-course warning for the missing half."""

    slots = _timeslots()
    first_slot = next(slot for slot in slots if slot.semester == 1 and slot.block == "A")
    result = solve_student_assignment(_input(
        requests=(_request(
            77, course_id=1, course_offering_id=11, duration="half_semester",
            credit_value=0.5, half_semester_segment="first_half", paired_half_course_id=2,
        ),),
        sections=(_section(
            timeslot_id=first_slot.id, half_semester_segment="first_half",
        ),),
        timeslots=slots,
    ))

    assert any(
        item.code == "student_assignment_half_semester_unallocated_opposite_half"
        for item in result.review_items
    )
    assert not any(
        item.code == "student_assignment_unallocated_school_time"
        and item.detail["timeslot_id"] == first_slot.id
        and "second_half" in item.detail["unallocated_half_segments"]
        for item in result.review_items
    )


def test_half_semester_online_keeps_full_term_supervision_and_flags_unused_half():
    result = solve_student_assignment(_input(
        requests=(_request(
            78, course_id=1, course_offering_id=11, delivery_kind="online",
            duration="half_semester", credit_value=0.5,
            half_semester_segment="first_half", paired_half_course_id=2,
        ),),
        sections=(_section(-1, timeslot_id=101),),
    ))

    assignment = result.assignments[0]
    assert assignment.section_id is None
    assert assignment.online_supervision_session_id == 1
    assert assignment.half_semester_segment == "first_half"
    assert {item.code for item in result.review_items} == {
        "student_assignment_half_semester_unallocated_opposite_half",
        "student_assignment_online_half_semester_unused_supervision_half",
    }
    ledger = result.candidate_ledger[0]
    assert ledger.selected_candidate["candidate_kind"] == "online_supervision_session"
    assert ledger.selected_candidate["online_supervision_session_id"] == 1
    assert ledger.review_item_codes == (
        "student_assignment_half_semester_unallocated_opposite_half",
        "student_assignment_online_half_semester_unused_supervision_half",
    )


def test_fixed_half_semester_online_enrollment_blocks_both_supervision_halves():
    """A rerun may not reuse the unused academic half of an occupied online seat."""

    result = solve_student_assignment(_input(
        requests=(_request(
            79, course_id=1, course_offering_id=11, duration="half_semester",
            credit_value=0.5, half_semester_segment="second_half",
        ),),
        sections=(
            _section(-1, delivery_group_id=-1, timeslot_id=101),
            _section(
                2, delivery_group_id=2, timeslot_id=101,
                half_semester_segment="second_half",
            ),
        ),
        fixed_enrollments=(FixedEnrollmentDTO(
            enrollment_id=80, student_id=1, section_id=-1, course_offering_id=99,
            course_id=99, semester=1, timeslot_id=101,
            half_semester_segment="first_half", delivery_kind="online",
            is_in_scope=False,
        ),),
    ))

    assert result.status == "partial"
    assert result.unmet_requests[0].diagnostic_code == "student_assignment_timeslot_collision"


def test_unknown_solver_outcome_is_failed_not_reported_as_infeasible(monkeypatch):
    """A bounded search timeout cannot be presented as a proof of impossibility."""

    monkeypatch.setattr(
        student_assignment_module,
        "_solve_lexicographically",
        lambda *_args, **_kwargs: (None, cp_model.UNKNOWN),
    )

    result = solve_student_assignment(_input())

    assert result.status == "failed"
    assert result.solver_outcome == "unknown"


def test_complete_hard_feasibility_seed_transfers_to_full_model():
    """A shared hard prefix can provide a full-model-validated incumbent."""

    hard_model = cp_model.CpModel()
    selected = hard_model.NewBoolVar("enroll_1_1")
    alternate = hard_model.NewBoolVar("enroll_1_2")
    hard_model.Add(selected + alternate <= 1)
    seed_model, seed_solver, indexes, status = (
        student_assignment_solver.solve_complete_hard_feasibility_seed(
            hard_model,
            ((selected, alternate),),
            1.0,
        )
    )

    assert status in {cp_model.OPTIMAL, cp_model.FEASIBLE}
    assert seed_solver is not None
    assert seed_solver.Value(seed_model.GetIntVarFromProtoIndex(selected.Index())) + seed_solver.Value(
        seed_model.GetIntVarFromProtoIndex(alternate.Index())
    ) == 1

    full_model = hard_model.Clone()
    derived = full_model.NewIntVar(0, 1, "derived_full_model_value")
    full_model.Add(derived == full_model.GetIntVarFromProtoIndex(selected.Index()))
    validated = student_assignment_solver.validate_complete_hard_feasibility_seed(
        full_model,
        seed_model,
        seed_solver,
        indexes,
        1.0,
    )

    assert validated is not None


def test_complete_hard_feasibility_seed_rejects_an_empty_required_group():
    """An unresolved required source may never be converted into a fake seed."""

    _model, seed_solver, indexes, status = (
        student_assignment_solver.solve_complete_hard_feasibility_seed(
            cp_model.CpModel(),
            ((),),
            1.0,
        )
    )

    assert status == cp_model.INFEASIBLE
    assert seed_solver is None
    assert indexes == ()


def test_unknown_hard_feasibility_seed_drops_unsuccessful_model_clone():
    """A timeout cannot keep a large, unusable feasibility clone alive."""

    model = cp_model.CpModel()
    selected = model.NewBoolVar("enroll_1_1")

    class _UnknownSolver:
        def Solve(self, _model):
            return cp_model.UNKNOWN

    with patch.object(student_assignment_solver, "new_solver", return_value=_UnknownSolver()):
        seed_model, seed_solver, indexes, status = (
            student_assignment_solver.solve_complete_hard_feasibility_seed(
                model,
                ((selected,),),
                1.0,
            )
        )

    assert seed_model is None
    assert seed_solver is None
    assert indexes == (selected.Index(),)
    assert status == cp_model.UNKNOWN


def test_hard_feasibility_seed_rejects_a_required_timing_capacity_bottleneck(monkeypatch):
    """Annual seats alone cannot satisfy a required distinct-block timetable.

    Three students each need the same four Semester-1 courses. Courses 1 and 3
    force B and C respectively, so every student needs an A-block seat from
    Course 2 or Course 4. Their combined A capacity is two, although every
    course has enough annual seats. A complete seed must expose this hard
    timing/capacity contradiction rather than report a false complete result.
    """

    requests = tuple(
        _request(
            request_id=(student_id - 1) * 4 + course_id,
            student_id=student_id,
            course_id=course_id,
            course_offering_id=course_id * 10,
            is_mandatory=True,
        )
        for student_id in (1, 2, 3)
        for course_id in (1, 2, 3, 4)
    )
    sections = (
        _section(
            1, delivery_group_id=1, member_course_offering_ids=(10,),
            member_course_ids=(1,), timeslot_id=102, capacity_max=3,
        ),
        _section(
            2, delivery_group_id=2, member_course_offering_ids=(20,),
            member_course_ids=(2,), timeslot_id=101, capacity_max=1,
        ),
        _section(
            3, delivery_group_id=2, member_course_offering_ids=(20,),
            member_course_ids=(2,), timeslot_id=104, capacity_max=3,
        ),
        _section(
            4, delivery_group_id=3, member_course_offering_ids=(30,),
            member_course_ids=(3,), timeslot_id=103, capacity_max=3,
        ),
        _section(
            5, delivery_group_id=4, member_course_offering_ids=(40,),
            member_course_ids=(4,), timeslot_id=101, capacity_max=1,
        ),
        _section(
            6, delivery_group_id=4, member_course_offering_ids=(40,),
            member_course_ids=(4,), timeslot_id=104, capacity_max=3,
        ),
    )
    seed_statuses = []
    original_seed_solver = student_assignment_module._solve_complete_hard_feasibility_seed

    def record_seed_status(*args, **kwargs):
        outcome = original_seed_solver(*args, **kwargs)
        seed_statuses.append(outcome[3])
        return outcome

    monkeypatch.setattr(
        student_assignment_module,
        "_solve_complete_hard_feasibility_seed",
        record_seed_status,
    )

    result = solve_student_assignment(_input(requests=requests, sections=sections))

    assert seed_statuses == [cp_model.INFEASIBLE]
    assert result.status == "partial"
    assert len(result.assignments) < len(requests)


def test_complete_hard_feasibility_seed_is_retained_when_first_objective_times_out(monkeypatch):
    """A complete CP-SAT seed remains usable when improvement finds no incumbent."""

    captured = {}

    def retain_seed(_model, _objectives, _time_limit, **kwargs):
        captured["seed"] = kwargs["validated_seed_solver"]
        return kwargs["validated_seed_solver"], cp_model.UNKNOWN

    monkeypatch.setattr(student_assignment_module, "_solve_lexicographically", retain_seed)

    result = solve_student_assignment(_input(
        requests=(_request(is_mandatory=True),),
    ))

    assert captured["seed"] is not None
    assert result.status == "complete"
    assert result.solver_outcome == "unknown"
    assert tuple(item.request_id for item in result.assignments) == (1,)


class _ControlledSolver:
    """Small CP-SAT stand-in for deterministic orchestration timeout tests."""

    def __init__(self, status, value=0):
        self.status = status
        self.value = value
        self.solve_calls = 0

    def Solve(self, _model):
        self.solve_calls += 1
        return self.status

    def Value(self, _expression):
        return self.value


def test_later_lexicographic_timeout_returns_the_prior_valid_incumbent(monkeypatch):
    """A lower-priority timeout must not erase a higher-priority candidate."""

    model = cp_model.CpModel()
    mandatory = model.NewBoolVar("mandatory")
    primary = model.NewBoolVar("primary")
    incumbent = _ControlledSolver(cp_model.OPTIMAL, value=0)
    timed_out = _ControlledSolver(cp_model.UNKNOWN)
    solvers = iter((incumbent, timed_out))
    monkeypatch.setattr(
        student_assignment_solver,
        "new_solver",
        lambda *_args, **_kwargs: next(solvers),
    )

    solver, outcome = student_assignment_solver.solve_lexicographically(
        model,
        (mandatory, primary),
        1.0,
    )

    assert solver is incumbent
    assert outcome == cp_model.UNKNOWN
    assert incumbent.solve_calls == 1
    assert timed_out.solve_calls == 1


def test_diagnostic_lexicographic_replay_retains_equal_or_worse_incumbent(
    monkeypatch,
):
    model = cp_model.CpModel()
    objective = model.NewIntVar(0, 2, "objective")
    incumbent = _ControlledSolver(cp_model.OPTIMAL, value=0)
    replacement = _ControlledSolver(cp_model.OPTIMAL, value=1)
    later_replacement = _ControlledSolver(cp_model.OPTIMAL, value=1)
    solvers = iter((replacement, later_replacement))
    monkeypatch.setattr(
        student_assignment_solver,
        "new_solver",
        lambda *_args, **_kwargs: next(solvers),
    )

    returned_solver, outcome = student_assignment_solver.solve_lexicographically(
        model,
        (objective, objective),
        1.0,
        validated_seed_solver=incumbent,
        retain_incumbent_on_non_improvement=True,
    )

    assert returned_solver is incumbent
    assert outcome == cp_model.FEASIBLE
    assert replacement.solve_calls == 1


def test_lexicographic_solver_skips_constant_objective_slots(monkeypatch):
    """An empty priority tier has no value and must not trigger a cold solve."""

    model = cp_model.CpModel()
    mandatory = model.NewBoolVar("mandatory")
    solver = _ControlledSolver(cp_model.OPTIMAL, value=0)
    monkeypatch.setattr(
        student_assignment_solver,
        "new_solver",
        lambda *_args, **_kwargs: solver,
    )

    returned_solver, outcome = student_assignment_solver.solve_lexicographically(
        model,
        (0, mandatory),
        1.0,
    )

    assert returned_solver is solver
    assert outcome == cp_model.FEASIBLE
    assert solver.solve_calls == 1


def test_all_constant_objectives_still_return_a_reviewable_feasibility_result(monkeypatch):
    """A fully protected rerun has no decisions but is valid fixed context."""

    model = cp_model.CpModel()
    solver = _ControlledSolver(cp_model.OPTIMAL)
    monkeypatch.setattr(
        student_assignment_solver,
        "new_solver",
        lambda *_args, **_kwargs: solver,
    )

    returned_solver, outcome = student_assignment_solver.solve_lexicographically(
        model,
        (0, 0),
        1.0,
    )

    assert returned_solver is solver
    assert outcome == cp_model.OPTIMAL
    assert solver.solve_calls == 1


def test_lexicographic_infeasibility_without_an_incumbent_remains_infeasible(monkeypatch):
    model = cp_model.CpModel()
    mandatory = model.NewBoolVar("mandatory")
    solver = _ControlledSolver(cp_model.INFEASIBLE)
    monkeypatch.setattr(
        student_assignment_solver,
        "new_solver",
        lambda *_args, **_kwargs: solver,
    )

    returned_solver, outcome = student_assignment_solver.solve_lexicographically(
        model,
        (mandatory,),
        1.0,
    )

    assert returned_solver is None
    assert outcome == cp_model.INFEASIBLE
