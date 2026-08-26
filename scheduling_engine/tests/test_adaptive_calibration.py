"""Contracts for matched offline adaptive-search calibration support."""

from dataclasses import replace
import gzip
import json
from types import SimpleNamespace

import pytest

from scheduling_engine.dto import TimeSlotDTO
import scheduling_engine.student_assignment.adaptive_runtime as adaptive_runtime
from scheduling_engine.realistic_student_assignment_validation import (
    build_realistic_quality_tradeoff_fixture,
)
from scheduling_engine.student_assignment.adaptive_calibration import (
    CALIBRATION_FIXED_CYCLES,
    CALIBRATION_PROFILES,
    CALIBRATION_SESSION_OVERRIDES,
    apply_calibration_profile,
    build_calibration_policy,
    profile_fingerprint,
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
