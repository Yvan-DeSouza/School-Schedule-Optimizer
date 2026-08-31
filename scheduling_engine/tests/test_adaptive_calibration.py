"""Contracts for matched offline adaptive-search calibration support."""

from dataclasses import replace
import gzip
import json
from types import SimpleNamespace

import pytest

from scheduling_engine.dto import TimeSlotDTO
import scheduling_engine.benchmark_adaptive_calibration as benchmark_calibration
import scheduling_engine.student_assignment.stage2_benchmark as stage2_benchmark
from scheduling_engine.benchmark_adaptive_calibration import (
    _write_validated_supervised_branch,
)
import scheduling_engine.student_assignment.adaptive_runtime as adaptive_runtime
from scheduling_engine.realistic_student_assignment_validation import (
    build_realistic_quality_tradeoff_fixture,
)
from scheduling_engine.student_assignment.adaptive_calibration import (
    CALIBRATION_FIXED_CYCLES,
    CALIBRATION_PROFILES,
    CALIBRATION_SESSION_OVERRIDES,
    STARTUP_AWARE_MAX_OPERATOR_SECONDS,
    STARTUP_AWARE_SESSION_OVERRIDES,
    STARTUP_AWARE_TOTAL_POLICY_SECONDS,
    apply_calibration_profile,
    build_calibration_trial_record,
    build_calibration_policy,
    profile_fingerprint,
)
from scheduling_engine.benchmark_policy_generalization import (
    STARTUP_AWARE_POLICIES,
    STARTUP_AWARE_SEEDS,
    _sha256_file,
    summarize_startup_aware_study,
    startup_aware_policy_budget_contract,
)
from scheduling_engine.student_assignment.adaptive_search import AdaptiveOperatorSpec
from scheduling_engine.student_assignment.core import run_substantive_soft_tier_probe
from scheduling_engine.student_assignment.stage2_benchmark import (
    read_diagnostic_branch_checkpoint,
    validate_diagnostic_branch_checkpoint,
    write_diagnostic_branch_checkpoint,
)
from scheduling_engine.student_assignment.runtime import (
    semantic_student_assignment_input_fingerprint,
)


def _v2_data():
    data = replace(
        build_realistic_quality_tradeoff_fixture(),
        timeslots=tuple(
            TimeSlotDTO(
                id=slot_id,
                academic_year_id=1,
                semester=1 if slot_id <= 3 else 2,
                block=("A", "B", "C", "D")[(slot_id - 1) % 4],
            )
            for slot_id in range(1, 7)
        ),
    )
    return apply_calibration_profile(
        data,
        "balanced",
    )


def test_target_scale_calibration_uses_shared_validation_boundary():
    assert (
        benchmark_calibration._calibration_validation_time_limit(
            SimpleNamespace(time_limit_seconds=20.0)
        )
        == 60.0
    )
    assert (
        benchmark_calibration._calibration_validation_time_limit(
            SimpleNamespace(time_limit_seconds=90.0)
        )
        == 90.0
    )
    assert (
        benchmark_calibration._calibration_validation_time_limit(
            SimpleNamespace(time_limit_seconds=20.0),
            180.0,
        )
        == 180.0
    )


def test_calibration_profiles_are_explicit_and_fingerprinted():
    assert set(CALIBRATION_PROFILES) == {
        "balanced",
        "student_quality_heavy",
        "utilization_heavy",
        "difficulty_category_heavy",
        "sequence_heavy",
    }
    assert len(profile_fingerprint("balanced")) == 64
    assert profile_fingerprint("balanced") != profile_fingerprint("utilization_heavy")
    with pytest.raises(ValueError, match="Unknown calibration profile"):
        profile_fingerprint("not-a-profile")


def test_calibration_trial_record_preserves_solver_configuration_metadata():
    data = _v2_data()
    initial_result = SimpleNamespace(
        status="complete",
        objective_components={},
        assignments=(),
        unmet_requests=(),
        commitment_assignments=(),
    )
    record = SimpleNamespace(
        attempts=(),
        decisions=(),
        policy_selection_seconds=0.0,
        operator_execution_seconds=0.0,
        finalization_seconds=0.0,
        external_overrun_seconds=0.0,
        elapsed_seconds=0.0,
        phase_timings={},
        final_components={},
        resource={},
    )
    result = SimpleNamespace(
        record=record,
        result=initial_result,
        source_decisions=(),
    )

    trial = build_calibration_trial_record(
        data,
        initial_result=initial_result,
        initial_source_decisions=(),
        policy="adaptive",
        profile="balanced",
        result=result,
        total_time_limit_seconds=60,
        per_operator_time_limit_seconds=30,
        worker_count=1,
        cp_sat_random_seed=101,
        cp_sat_max_deterministic_time_seconds=12.5,
    )

    assert trial.cp_sat_random_seed == 101
    assert trial.cp_sat_max_deterministic_time_seconds == 12.5
    assert trial.final_source_decision_fingerprint is None


def test_calibration_trial_record_preserves_inner_probe_summaries():
    data = _v2_data()
    initial_result = SimpleNamespace(
        status="complete",
        objective_components={},
        assignments=(),
        unmet_requests=(),
        commitment_assignments=(),
    )
    record = SimpleNamespace(
        attempts=({
            "operator": "targeted_r4_s2",
            "inner_probe_summaries": ({
                "status": "unknown",
                "candidate_validated": True,
                "starting_incumbent_value": 100,
                "candidate_substantive_value": 94,
            },),
        },),
        decisions=(),
        policy_selection_seconds=0.0,
        operator_execution_seconds=0.0,
        finalization_seconds=0.0,
        external_overrun_seconds=0.0,
        elapsed_seconds=0.0,
        phase_timings={},
        final_objective_vector=(),
        final_components={},
        resource={},
    )
    result = SimpleNamespace(record=record, result=initial_result, source_decisions=())

    trial = build_calibration_trial_record(
        data,
        initial_result=initial_result,
        initial_source_decisions=(),
        policy="adaptive",
        profile="balanced",
        result=result,
        total_time_limit_seconds=60,
        per_operator_time_limit_seconds=30,
        worker_count=1,
    )

    assert trial.attempts[0]["inner_probe_summaries"][0]["candidate_validated"] is True


def test_calibration_trial_record_carries_terminal_source_state(monkeypatch):
    """The supervised payload can preserve an adopted terminal incumbent."""

    data = _v2_data()
    source_decisions = ((("course", 1), (2, 3)),)
    initial_result = SimpleNamespace(
        status="complete",
        objective_components={},
        assignments=(),
        unmet_requests=(),
        commitment_assignments=(),
    )
    record = SimpleNamespace(
        attempts=(),
        decisions=(),
        policy_selection_seconds=0.0,
        operator_execution_seconds=0.0,
        finalization_seconds=0.0,
        external_overrun_seconds=0.0,
        elapsed_seconds=0.0,
        phase_timings={},
        final_components={},
        final_objective_vector=(-9, 8),
        resource={},
    )
    result = SimpleNamespace(
        record=record,
        result=initial_result,
        source_decisions=source_decisions,
    )
    monkeypatch.setattr(
        stage2_benchmark,
        "semantic_stage1_seed_source_fingerprint",
        lambda *args, **kwargs: "terminal-fingerprint",
    )

    trial = build_calibration_trial_record(
        data,
        initial_result=initial_result,
        initial_source_decisions=source_decisions,
        policy="adaptive",
        profile="balanced",
        result=result,
        total_time_limit_seconds=60,
        per_operator_time_limit_seconds=30,
        worker_count=1,
    )

    assert trial.final_source_decisions == source_decisions
    assert trial.final_source_decision_fingerprint == "terminal-fingerprint"
    assert trial.final_objective_vector == (-9, 8)


def test_calibration_controls_use_named_existing_operator_families():
    assert CALIBRATION_FIXED_CYCLES["fixed_cycle"] == (
        "targeted_r4_s2",
        "targeted_utilization_r64_s8",
        "r2",
    )
    assert build_calibration_policy("adaptive")["selection_policy"] == "adaptive"
    assert build_calibration_policy("stateless_role")["selection_policy"] == (
        "stateless_role"
    )
    fixed = build_calibration_policy("fixed_cycle")
    assert fixed["selection_policy"] == "fixed_cycle"
    assert tuple(spec.name for spec in fixed["fixed_cycle"]) == (
        "targeted_r4_s2",
        "targeted_utilization_r64_s8",
        "r2",
    )
    r8 = build_calibration_policy("student_repair_r8_only")
    assert r8["selection_policy"] == "fixed_cycle"
    assert tuple(spec.name for spec in r8["fixed_cycle"]) == ("targeted_r8_s2",)


def test_startup_aware_policy_contract_is_fair_and_diagnostic_only():
    contract = startup_aware_policy_budget_contract()

    assert STARTUP_AWARE_POLICIES == ("adaptive", "stateless_role", "fixed_cycle")
    assert STARTUP_AWARE_SEEDS == (101, 202, 303)
    assert contract["worker_count"] == 1
    assert contract["per_operator_maximum_seconds"] == (
        STARTUP_AWARE_MAX_OPERATOR_SECONDS
    )
    assert contract["cumulative_policy_budget_seconds"] == (
        STARTUP_AWARE_TOTAL_POLICY_SECONDS
    )
    assert contract["full_model_validation_required"] is True
    assert contract["unvalidated_candidate_adoption"] is False
    assert contract["production_policy_wiring"] is False
    assert set(STARTUP_AWARE_SESSION_OVERRIDES) == set(
        CALIBRATION_SESSION_OVERRIDES
    )
    assert all(
        override["session_max_attempts"] == 1
        and override["session_time_limit_seconds"] == 300.0
        and override["per_attempt_cp_sat_limit_seconds"] == 300.0
        for override in STARTUP_AWARE_SESSION_OVERRIDES.values()
    )


def test_startup_aware_summary_verifies_artifacts_and_ranks_completed_cells(
    tmp_path,
):
    result_path = tmp_path / "results" / "reference_target_fixed_cycle_seed101.json"
    payload = {
        "scenario_id": "reference_target",
        "policy": "fixed_cycle",
        "seed": 101,
        "execution_status": "completed",
        "candidate_complete": True,
        "final_unmet_count": 0,
        "initial_substantive_value": 20,
        "final_substantive_value": 18,
        "final_components": {},
        "final_objective_vector": [],
        "final_assignment_count": 10,
        "final_special_commitment_count": 2,
        "final_source_decision_fingerprint": "terminal",
        "cell_elapsed_seconds": 4.0,
        "phase_timings": {"policy": {"total": 3.0}},
        "timing": {"total_elapsed_seconds": 3.0},
        "policy_accounting": {
            "cumulative_cp_sat_seconds": 1.0,
            "cumulative_validation_seconds": 0.5,
        },
        "resource": {"peak_tree_working_set_bytes": 100},
        "preparation": {
            "parent_branch_validation": {"full_model_validation": True}
        },
        "attempts": [],
    }
    result_path.parent.mkdir()
    result_path.write_text(json.dumps(payload), encoding="utf-8")
    manifest = {
        "study_id": "test-study",
        "source_lineage": "test-lineage",
        "budget_contract": {"protocol_version": "test"},
        "scenario_ids": ["reference_target"],
        "scenarios": {
            "reference_target": {
                "input_fingerprint": "input",
                "source_seed_fingerprint": "seed",
            }
        },
        "results": {
            result_path.name: {
                "path": str(result_path),
                "sha256": _sha256_file(result_path),
            }
        },
    }
    (tmp_path / "study_manifest.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )

    summary = summarize_startup_aware_study(tmp_path)

    assert summary["artifact_integrity"] == {
        "manifest_result_count": 1,
        "loaded_result_count": 1,
        "all_result_hashes_verified": True,
    }
    assert summary["scenario_winners"]["reference_target"]["policy"] == (
        "fixed_cycle"
    )
    assert summary["policy_summary"]["reference_target:fixed_cycle"][
        "best_final_value"
    ] == 18.0


def test_operator_result_forwards_specified_continuous_session(monkeypatch):
    calls = []

    def fake_session(*args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr(
        adaptive_runtime,
        "run_student_assignment_operator_session_diagnostic",
        fake_session,
    )
    spec = AdaptiveOperatorSpec(
        "targeted_r4_s2",
        4,
        2,
        True,
        2,
        "targeted_repair",
        session_time_limit_seconds=90,
        session_max_attempts=4,
        per_attempt_cp_sat_limit_seconds=15,
    )
    adaptive_runtime._operator_result(
        object(),
        spec,
        selected_student_ids=(7, 8),
        current_source_decisions=(("course", 1),),
        time_limit_seconds=60,
        worker_count=8,
        collect_resource_telemetry=False,
    )
    assert calls[0]["total_time_limit_seconds"] == 60
    assert calls[0]["max_attempts"] == 4
    assert calls[0]["per_attempt_time_limit_seconds"] == 15
    assert calls[0]["worker_count"] == 8
    assert calls[0]["target_policy"] == "dynamic"
    assert calls[0]["selected_student_ids"] == (7, 8)


def test_diagnostic_branch_round_trips_and_materializes_semantic_decisions(tmp_path):
    data = _v2_data()
    probe = run_substantive_soft_tier_probe(
        data,
        threshold=None,
        time_limit_seconds=5,
        worker_count=1,
    )
    assert probe.seed_validated is True
    path = tmp_path / "student_branch.json.gz"
    input_fingerprint = semantic_student_assignment_input_fingerprint(data)
    payload = write_diagnostic_branch_checkpoint(
        path,
        data=data,
        source_decisions=probe.seed_source_decisions,
        parent_source_decision_fingerprint="parent-fingerprint",
        branch_id="baseline",
        provenance={"operator": "stage1_seed"},
        objective_vector=probe.seed_objective_vector,
        substantive_components=probe.seed_component_values,
        validation={
            "full_model_validation": True,
            "complete": True,
            "assignment_count": None,
            "required_source_decision_group_count": None,
            "unmet_request_count": 0,
            "special_commitment_count": 0,
        },
    )
    loaded = read_diagnostic_branch_checkpoint(
        path,
        data=data,
        expected_input_fingerprint=input_fingerprint,
    )
    assert payload["schema"] == "student_assignment_diagnostic_branch_v1"
    assert loaded["branch_id"] == "baseline"
    assert loaded["source_decision_fingerprint"]
    assert (
        loaded["source_decision_fingerprint"]
        == payload["source_decision_fingerprint"]
    )
    assert loaded["canonical_source_decisions"]


def test_diagnostic_branch_requires_full_validation(tmp_path):
    data = _v2_data()
    probe = run_substantive_soft_tier_probe(
        data,
        threshold=None,
        time_limit_seconds=5,
        worker_count=1,
    )
    with pytest.raises(ValueError, match="full-model validation"):
        write_diagnostic_branch_checkpoint(
            tmp_path / "invalid.json.gz",
            data=data,
            source_decisions=probe.seed_source_decisions,
            parent_source_decision_fingerprint="parent-fingerprint",
            branch_id="invalid",
            provenance={},
            validation={"complete": True, "unmet_request_count": 0},
        )


def test_diagnostic_branch_rejects_stored_unmet_requests(tmp_path):
    data = _v2_data()
    probe = run_substantive_soft_tier_probe(
        data,
        threshold=None,
        time_limit_seconds=5,
        worker_count=1,
    )
    path = tmp_path / "unmet-branch.json.gz"
    payload = write_diagnostic_branch_checkpoint(
        path,
        data=data,
        source_decisions=probe.seed_source_decisions,
        parent_source_decision_fingerprint="parent-fingerprint",
        branch_id="unmet",
        provenance={},
        validation={
            "full_model_validation": True,
            "complete": True,
            "assignment_count": probe.seed_assignment_count,
            "unmet_request_count": 0,
        },
    )
    payload["validation"]["unmet_request_count"] = 1
    path.write_bytes(
        gzip.compress(json.dumps(payload, sort_keys=True).encode("utf-8"))
    )
    with pytest.raises(ValueError, match="unmet requests"):
        read_diagnostic_branch_checkpoint(path, data=data)


def test_supervised_branch_output_requires_strict_complete_result(tmp_path):
    data = _v2_data()
    probe = run_substantive_soft_tier_probe(
        data,
        threshold=None,
        time_limit_seconds=5,
        worker_count=1,
    )
    parent_path = tmp_path / "parent.json.gz"
    write_diagnostic_branch_checkpoint(
        parent_path,
        data=data,
        source_decisions=probe.seed_source_decisions,
        parent_source_decision_fingerprint="parent-fingerprint",
        branch_id="parent",
        provenance={},
        objective_vector=probe.seed_objective_vector,
        substantive_components=probe.seed_component_values,
        validation={
            "full_model_validation": True,
            "complete": True,
            "assignment_count": probe.seed_assignment_count,
            "unmet_request_count": 0,
            "special_commitment_count": 0,
        },
    )
    parent = read_diagnostic_branch_checkpoint(parent_path, data=data)
    source_decisions = [
        [list(key), list(value)]
        for key, value in probe.seed_source_decisions
    ]
    payload = {
        "execution_status": "completed",
        "candidate_complete": True,
        "initial_substantive_value": 10,
        "final_substantive_value": 9,
        "final_source_decisions": source_decisions,
        "final_objective_vector": [-1],
        "final_components": {},
        "final_assignment_count": probe.seed_assignment_count,
        "final_unmet_count": 0,
        "final_special_commitment_count": 0,
    }

    output_path = tmp_path / "derived.json.gz"
    derived = _write_validated_supervised_branch(
        output_path,
        data=data,
        parent_branch=parent,
        payload=payload,
        policy="student_repair_only",
        profile="balanced",
    )

    assert output_path.exists()
    assert derived["full_model_validated"] is True
    loaded = read_diagnostic_branch_checkpoint(output_path, data=data)
    assert loaded["parent_source_decision_fingerprint"] == parent[
        "source_decision_fingerprint"
    ]


def test_supervised_branch_output_does_not_persist_non_improvement(tmp_path):
    data = _v2_data()
    output_path = tmp_path / "not-written.json.gz"
    payload = {
        "execution_status": "completed",
        "candidate_complete": True,
        "initial_substantive_value": 10,
        "final_substantive_value": 10,
        "final_source_decisions": (((1,), (1,)),),
    }

    assert (
        _write_validated_supervised_branch(
            output_path,
            data=data,
            parent_branch={"source_decision_fingerprint": "parent"},
            payload=payload,
            policy="student_repair_only",
            profile="balanced",
        )
        is None
    )
    assert not output_path.exists()


def test_diagnostic_branch_is_revalidated_by_the_current_full_model(tmp_path):
    data = _v2_data()
    probe = run_substantive_soft_tier_probe(
        data,
        threshold=None,
        time_limit_seconds=5,
        worker_count=1,
    )
    path = tmp_path / "validated-branch.json.gz"
    write_diagnostic_branch_checkpoint(
        path,
        data=data,
        source_decisions=probe.seed_source_decisions,
        parent_source_decision_fingerprint="parent-fingerprint",
        branch_id="validated",
        provenance={"operator": "stage1_seed"},
        objective_vector=probe.seed_objective_vector,
        substantive_components=probe.seed_component_values,
        validation={
            "full_model_validation": True,
            "complete": True,
            "assignment_count": probe.seed_assignment_count,
            "required_source_decision_group_count": None,
            "unmet_request_count": 0,
            "special_commitment_count": 0,
        },
    )
    validation = validate_diagnostic_branch_checkpoint(
        path,
        data=data,
        time_limit_seconds=30,
        worker_count=1,
    )
    assert validation["validation"]["full_model_validation"] is True
    assert validation["validation"]["complete"] is True
    assert validation["validation"]["unmet_request_count"] == 0


def test_session_override_profiles_are_bounded_and_include_grade_families():
    assert CALIBRATION_SESSION_OVERRIDES["r2"]["session_max_attempts"] == 5
    assert CALIBRATION_SESSION_OVERRIDES["targeted_r4_s2"]["session_max_attempts"] == 5
    assert CALIBRATION_SESSION_OVERRIDES["grade_bounded_g10"]["session_max_attempts"] == 1


def test_supervised_preparation_elapsed_is_not_total_worker_lifetime(
    monkeypatch, tmp_path
):
    """Parent preparation telemetry must stop when the worker is launched."""

    data = _v2_data()
    result = SimpleNamespace(
        status="complete",
        solver_outcome="feasible",
        assignments=(),
        unmet_requests=(),
        commitment_assignments=(),
        objective_components={},
        optimization_facts={},
    )
    branch = {
        "source_decision_fingerprint": "branch-fingerprint",
        "source_decisions": (),
    }
    monkeypatch.setattr(
        benchmark_calibration,
        "read_durable_stage2_benchmark",
        lambda _path: {"data": data, "manifest": {"stage1": {}}},
    )
    monkeypatch.setattr(
        benchmark_calibration,
        "read_diagnostic_branch_checkpoint",
        lambda *args, **kwargs: branch,
    )
    monkeypatch.setattr(
        benchmark_calibration,
        "validate_diagnostic_branch_checkpoint",
        lambda *args, **kwargs: {
            "result": result,
            "validation": {"elapsed_seconds": 0.01},
        },
    )

    class FakeSupervision:
        execution_status = benchmark_calibration.EXECUTION_COMPLETED
        payload = {"timing": {}, "phase_timings": {}}
        worker_pid = 123
        worker_exit_code = 0
        elapsed_seconds = 0.25
        cleanup = {}

        def to_dict(self):
            return {"elapsed_seconds": self.elapsed_seconds}

    monkeypatch.setattr(
        benchmark_calibration,
        "supervise_json_worker",
        lambda *args, **kwargs: FakeSupervision(),
    )
    monkeypatch.setattr(
        benchmark_calibration,
        "run_matched_calibration_trial",
        lambda *args, **kwargs: SimpleNamespace(
            to_dict=lambda: {"timing": {}, "phase_timings": {}, "attempts": []},
        ),
    )

    payload = benchmark_calibration.run_supervised_calibration_trial(
        policy="adaptive",
        profile="balanced",
        benchmark_directory=tmp_path / "benchmark",
        branch_input=tmp_path / "branch.json.gz",
        total_time_limit_seconds=1,
        per_operator_time_limit_seconds=1,
        worker_count=1,
        hard_wall_seconds=1,
    )

    assert payload["preparation"]["elapsed_seconds"] == pytest.approx(
        payload["parent_preparation_seconds"]
    )


def test_supervised_worker_serializes_trial_record_with_branch_lineage(
    monkeypatch, tmp_path
):
    """The worker accepts the compact trial record returned by calibration."""

    data = _v2_data()
    source_decisions = ((('course', 1), (2, 3)),)
    branch = {
        "source_decision_fingerprint": "canonical-fingerprint",
        "source_decisions": source_decisions,
        "objective_vector": (-1, 2),
    }
    result = SimpleNamespace(
        status="complete",
        solver_outcome="feasible",
        assignments=(),
        unmet_requests=(),
        commitment_assignments=(),
        objective_components={},
        optimization_facts={},
    )
    written = {}

    monkeypatch.setattr(
        benchmark_calibration,
        "read_durable_stage2_benchmark",
        lambda _path: {"data": data, "manifest": {}},
    )
    monkeypatch.setattr(
        benchmark_calibration,
        "apply_calibration_profile",
        lambda value, _profile: value,
    )
    monkeypatch.setattr(
        benchmark_calibration,
        "read_diagnostic_branch_checkpoint",
        lambda *args, **kwargs: branch,
    )
    monkeypatch.setattr(
        benchmark_calibration,
        "validate_diagnostic_branch_checkpoint",
        lambda *args, **kwargs: {
            "result": result,
            "validation": {"full_model_validation": True, "complete": True},
        },
    )
    monkeypatch.setattr(
        benchmark_calibration,
        "_write_worker_phase",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        benchmark_calibration,
        "_write_worker_output",
        lambda path, payload: written.update(payload),
    )
    monkeypatch.setattr(
        benchmark_calibration,
        "run_matched_calibration_trial",
        lambda *args, **kwargs: SimpleNamespace(
            to_dict=lambda: {"attempts": [], "timing": {}, "phase_timings": {}},
        ),
    )

    args = SimpleNamespace(
        benchmark_directory=tmp_path / "benchmark",
        branch_input=tmp_path / "branch.json.gz",
        prepared_incumbent=None,
        worker_status=tmp_path / "status.json",
        worker_output=tmp_path / "result.json",
        policy="fixed_cycle",
        profile="balanced",
        total_seconds=1,
        per_operator_seconds=1,
        workers=1,
        validated_branch_output=None,
    )

    assert benchmark_calibration._run_supervised_worker(args) == 0
    assert written["final_source_decisions"] == [[["course", 1], [2, 3]]]
    assert written["final_source_decision_fingerprint"] == "canonical-fingerprint"
    assert written["final_objective_vector"] == [-1, 2]


def test_supervised_worker_preserves_terminal_trial_state_without_branch_path(
    monkeypatch, tmp_path
):
    """An adopted terminal state is not replaced by the parent checkpoint."""

    data = _v2_data()
    source_decisions = ((("course", 1), (2, 3)),)
    branch = {
        "source_decision_fingerprint": "parent-fingerprint",
        "source_decisions": ((("course", 9), (8, 7)),),
        "objective_vector": (-1, 2),
    }
    result = SimpleNamespace(
        status="complete",
        solver_outcome="feasible",
        assignments=(),
        unmet_requests=(),
        commitment_assignments=(),
        objective_components={},
        optimization_facts={},
    )
    written = {}

    monkeypatch.setattr(
        benchmark_calibration,
        "read_durable_stage2_benchmark",
        lambda _path: {"data": data, "manifest": {}},
    )
    monkeypatch.setattr(
        benchmark_calibration,
        "apply_calibration_profile",
        lambda value, _profile: value,
    )
    monkeypatch.setattr(
        benchmark_calibration,
        "read_diagnostic_branch_checkpoint",
        lambda *args, **kwargs: branch,
    )
    monkeypatch.setattr(
        benchmark_calibration,
        "validate_diagnostic_branch_checkpoint",
        lambda *args, **kwargs: {
            "result": result,
            "validation": {"full_model_validation": True, "complete": True},
        },
    )
    monkeypatch.setattr(
        benchmark_calibration,
        "semantic_stage1_seed_source_fingerprint",
        lambda *args, **kwargs: "trial-fingerprint",
    )
    monkeypatch.setattr(
        benchmark_calibration,
        "_write_worker_phase",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        benchmark_calibration,
        "_write_worker_output",
        lambda path, payload: written.update(payload),
    )
    monkeypatch.setattr(
        benchmark_calibration,
        "run_matched_calibration_trial",
        lambda *args, **kwargs: SimpleNamespace(
            to_dict=lambda: {
                "attempts": [],
                "timing": {},
                "phase_timings": {},
                "final_source_decisions": source_decisions,
                "final_source_decision_fingerprint": "stale-trial-fingerprint",
                "final_objective_vector": (-9, 8),
            },
        ),
    )

    args = SimpleNamespace(
        benchmark_directory=tmp_path / "benchmark",
        branch_input=tmp_path / "branch.json.gz",
        prepared_incumbent=None,
        worker_status=tmp_path / "status.json",
        worker_output=tmp_path / "result.json",
        policy="fixed_cycle",
        profile="balanced",
        total_seconds=1,
        per_operator_seconds=1,
        workers=1,
        validated_branch_output=None,
    )

    assert benchmark_calibration._run_supervised_worker(args) == 0
    assert written["final_source_decisions"] == [[['course', 1], [2, 3]]]
    assert written["final_source_decision_fingerprint"] == "trial-fingerprint"
    assert written["final_objective_vector"] == [-9, 8]


def test_parent_revalidation_metadata_is_not_overwritten_by_branch_write(
    monkeypatch, tmp_path
):
    """A parent-validated worker branch keeps its authority proof."""

    data = _v2_data()
    result = SimpleNamespace(
        status="complete",
        solver_outcome="feasible",
        assignments=(),
        unmet_requests=(),
        commitment_assignments=(),
        objective_components={},
        optimization_facts={},
    )
    branch = {
        "source_decision_fingerprint": "parent-fingerprint",
        "source_decisions": (),
    }
    worker_branch_path = tmp_path / "worker-branch.json.gz"
    branch_write_calls = []

    monkeypatch.setattr(
        benchmark_calibration,
        "read_durable_stage2_benchmark",
        lambda _path: {"data": data, "manifest": {}},
    )
    monkeypatch.setattr(
        benchmark_calibration,
        "apply_calibration_profile",
        lambda value, _profile: value,
    )
    monkeypatch.setattr(
        benchmark_calibration,
        "read_diagnostic_branch_checkpoint",
        lambda *args, **kwargs: branch,
    )
    monkeypatch.setattr(
        benchmark_calibration,
        "validate_diagnostic_branch_checkpoint",
        lambda *args, **kwargs: {
            "result": result,
            "validation": {
                "full_model_validation": True,
                "complete": True,
                "unmet_request_count": 0,
                "elapsed_seconds": 0.01,
            },
        },
    )

    class FakeSupervision:
        execution_status = benchmark_calibration.EXECUTION_COMPLETED
        payload = {"timing": {}, "phase_timings": {}, "attempts": []}
        worker_pid = 123
        worker_exit_code = 0
        elapsed_seconds = 0.25
        cleanup = {}

        def to_dict(self):
            return {"elapsed_seconds": self.elapsed_seconds}

    def fake_supervise(*args, **kwargs):
        worker_branch_path.touch()
        return FakeSupervision()

    monkeypatch.setattr(
        benchmark_calibration,
        "supervise_json_worker",
        fake_supervise,
    )
    monkeypatch.setattr(
        benchmark_calibration,
        "_write_validated_supervised_branch",
        lambda *args, **kwargs: branch_write_calls.append((args, kwargs)),
    )

    payload = benchmark_calibration.run_supervised_calibration_trial(
        policy="adaptive",
        profile="balanced",
        benchmark_directory=tmp_path / "benchmark",
        branch_input=tmp_path / "parent.json.gz",
        total_time_limit_seconds=1,
        per_operator_time_limit_seconds=1,
        worker_count=1,
        hard_wall_seconds=1,
        validated_branch_output=worker_branch_path,
    )

    assert payload["derived_branch"]["parent_revalidated_after_worker"] is True
    assert payload["derived_branch"]["full_model_validated"] is True
    assert branch_write_calls == []
