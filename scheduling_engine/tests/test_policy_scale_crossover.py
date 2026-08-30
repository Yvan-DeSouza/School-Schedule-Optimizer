import hashlib
import json
from pathlib import Path


STUDY_DIRECTORY = (
    Path(__file__).parents[1]
    / "benchmarks"
    / "student_assignment"
    / "v2_policy_scale_crossover_20260830"
)

GATE_RESULT_FILENAMES = (
    "reference_target_targeted_r4_s2_workers1_seed101.json",
    "reference_target_targeted_r4_s2_workers8_seed101.json",
)

CONTINUATION_RESULT_FILENAMES = (
    "reference_target_targeted_r4_s2_startup120_workers1_seed101.json",
    "reference_target_targeted_r4_s2_horizon300_workers1_seed101.json",
    "reference_target_targeted_r4_s2_horizon300_workers8_seed101.json",
    "special_pressure_target_targeted_r4_s2_horizon180_workers1_seed101.json",
    "special_pressure_target_targeted_r4_s2_horizon360_workers1_seed101.json",
)

REPEATABILITY_RESULT_BINDINGS = {
    "reference_target_targeted_r4_s2_cp300_validation180_workers1_seed202.json": (
        "f56b5c0d5b745d919a57281a2f1e49959b4b23d8feb9486eda3c81afd8bb7906",
        "f5cfd15465bab1815ad21a3565236f1ff383e8ffff82a4c783ab62ff410c9fb1",
        202,
        42750,
        42672,
    ),
    "reference_target_targeted_r4_s2_cp300_validation180_workers1_seed303.json": (
        "f56b5c0d5b745d919a57281a2f1e49959b4b23d8feb9486eda3c81afd8bb7906",
        "f5cfd15465bab1815ad21a3565236f1ff383e8ffff82a4c783ab62ff410c9fb1",
        303,
        42750,
        42672,
    ),
    "special_pressure_target_targeted_r4_s2_cp300_validation180_workers1_seed202.json": (
        "93d4bdf208027d50e51842290d77b7f77c6a92af724a7bd99ce6b78519bc0ca5",
        "1866d7dca8dddb65a93f7eb1eb6935a329098c49da17c7897edc4a027c72cd46",
        202,
        43626,
        43548,
    ),
    "special_pressure_target_targeted_r4_s2_cp300_validation180_workers1_seed303.json": (
        "93d4bdf208027d50e51842290d77b7f77c6a92af724a7bd99ce6b78519bc0ca5",
        "1866d7dca8dddb65a93f7eb1eb6935a329098c49da17c7897edc4a027c72cd46",
        303,
        43626,
        43548,
    ),
}

CONTINUATION_BINDINGS = {
    "reference_target_targeted_r4_s2_startup120_workers1_seed101.json": (
        "f56b5c0d5b745d919a57281a2f1e49959b4b23d8feb9486eda3c81afd8bb7906",
        "f5cfd15465bab1815ad21a3565236f1ff383e8ffff82a4c783ab62ff410c9fb1",
        (204, 604),
    ),
    "reference_target_targeted_r4_s2_horizon300_workers1_seed101.json": (
        "f56b5c0d5b745d919a57281a2f1e49959b4b23d8feb9486eda3c81afd8bb7906",
        "f5cfd15465bab1815ad21a3565236f1ff383e8ffff82a4c783ab62ff410c9fb1",
        (204, 604),
    ),
    "reference_target_targeted_r4_s2_horizon300_workers8_seed101.json": (
        "f56b5c0d5b745d919a57281a2f1e49959b4b23d8feb9486eda3c81afd8bb7906",
        "f5cfd15465bab1815ad21a3565236f1ff383e8ffff82a4c783ab62ff410c9fb1",
        (204, 604),
    ),
    "special_pressure_target_targeted_r4_s2_horizon180_workers1_seed101.json": (
        "93d4bdf208027d50e51842290d77b7f77c6a92af724a7bd99ce6b78519bc0ca5",
        "1866d7dca8dddb65a93f7eb1eb6935a329098c49da17c7897edc4a027c72cd46",
        (1054, 554),
    ),
    "special_pressure_target_targeted_r4_s2_horizon360_workers1_seed101.json": (
        "93d4bdf208027d50e51842290d77b7f77c6a92af724a7bd99ce6b78519bc0ca5",
        "1866d7dca8dddb65a93f7eb1eb6935a329098c49da17c7897edc4a027c72cd46",
        (1054, 554),
    ),
}


def _read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def test_scale_crossover_lineage_is_bound_to_declared_target_and_source():
    manifest = _read_json(STUDY_DIRECTORY / "study_manifest.json")
    gate = manifest["target_worker_gate"]
    assert manifest["status"] == "completed_budget_separation_and_target_repeatability"
    assert gate["input_fingerprint"] == (
        "f56b5c0d5b745d919a57281a2f1e49959b4b23d8feb9486eda3c81afd8bb7906"
    )
    assert gate["source_seed_fingerprint"] == (
        "f5cfd15465bab1815ad21a3565236f1ff383e8ffff82a4c783ab62ff410c9fb1"
    )
    assert gate["worker_counts"] == [1, 8]
    assert gate["actual_selected_student_ids"] == [204, 604]
    assert manifest["final_classification"]["worker_count_effect"].startswith(
        "not_demonstrated"
    )


def test_scale_crossover_result_artifacts_match_manifest_and_gate():
    manifest = _read_json(STUDY_DIRECTORY / "study_manifest.json")
    gate = manifest["target_worker_gate"]
    for filename in GATE_RESULT_FILENAMES:
        expected = manifest["results"][filename]
        path = STUDY_DIRECTORY / "results" / filename
        result = _read_json(path)
        assert _sha256(path) == expected["sha256"]
        assert result["operator"] == gate["operator"]
        assert result["worker_count"] in gate["worker_counts"]
        assert result["cp_sat_random_seed"] == gate["cp_sat_random_seed"]
        assert result["selected_student_ids"] == gate["actual_selected_student_ids"]
        assert result["benchmark_manifest_fingerprint"] == gate["input_fingerprint"]
        assert result["source_seed_fingerprint"] == gate["source_seed_fingerprint"]
        assert result["candidate_validated"] is False
        assert result["candidate_adopted"] is False
        assert result["solver_status"] == "unknown"
        assert result["branches"] == 0
        assert result["conflicts"] == 0
        assert result["seed_validation"]["complete"] is True
        assert result["seed_validation"]["full_model_validation"] is True
        assert result["seed_validation"]["unmet_request_count"] == 0


def test_startup_characterization_artifacts_record_search_gate_and_validation():
    manifest = _read_json(STUDY_DIRECTORY / "study_manifest.json")
    gate = manifest["target_worker_gate"]
    for filename in CONTINUATION_RESULT_FILENAMES:
        expected = manifest["results"][filename]
        path = STUDY_DIRECTORY / "results" / filename
        result = _read_json(path)
        input_fingerprint, source_fingerprint, selected_students = (
            CONTINUATION_BINDINGS[filename]
        )
        assert _sha256(path) == expected["sha256"]
        assert result["benchmark_manifest_fingerprint"] == input_fingerprint
        assert result["source_seed_fingerprint"] == source_fingerprint
        assert result["operator"] == gate["operator"]
        assert tuple(result["selected_student_ids"]) == selected_students
        if result["execution_status"] == "hard_deadline_terminated":
            assert "attempts" not in result
            assert result["candidate_found"] is False
            assert result["candidate_validated"] is False
            continue
        attempt = result["attempts"][0]
        telemetry = attempt["search_start_telemetry"]
        assert telemetry["enabled"] is True
        if "startup120" in filename:
            assert result["solver_status"] == "unknown"
            assert result["candidate_found"] is False
            assert telemetry["search_started"] is False
            assert attempt["branches"] == 0
        else:
            assert telemetry["search_started"] is True
            assert telemetry["first_solution_found"] is True
            assert attempt["candidate_value"] is not None
        if "horizon360" in filename or "horizon300" in filename:
            assert result["candidate_validated"] is True
            assert result["candidate_adopted"] is True
        if "horizon180" in filename:
            assert result["candidate_validated"] is False
            assert result["validation_solver_outcome"] == "unknown"


def test_scale_crossover_source_artifact_hashes_match_declared_lineage():
    manifest = _read_json(STUDY_DIRECTORY / "study_manifest.json")
    source_root = STUDY_DIRECTORY.parent / "v2_policy_generalization_suite_20260829"
    for relative_path, expected_hash in manifest["source_artifact_sha256"].items():
        source_path = source_root / relative_path
        assert source_path.exists()
        assert _sha256(source_path) == expected_hash


def test_independent_validation_repeatability_artifacts_are_bound_and_validated():
    manifest = _read_json(STUDY_DIRECTORY / "study_manifest.json")
    repeatability = manifest["independent_validation_budget_repeatability"]
    configuration = repeatability["configuration"]
    assert configuration == {
        "operator": "targeted_r4_s2",
        "worker_count": 1,
        "cp_sat_random_seeds": [202, 303],
        "search_time_limit_seconds": 300.0,
        "candidate_validation_time_limit_seconds": 180.0,
        "session_time_limit_seconds": 900.0,
        "parent_hard_wall_seconds": 1200.0,
        "source_validation_worker_count": 1,
        "canonical_state_mutated": False,
    }
    for filename, binding in REPEATABILITY_RESULT_BINDINGS.items():
        expected_input, expected_source, expected_seed, start, candidate = binding
        expected = manifest["results"][filename]
        path = STUDY_DIRECTORY / "results" / filename
        result = _read_json(path)
        attempt = result["attempts"][0]
        assert _sha256(path) == expected["sha256"]
        assert result["benchmark_manifest_fingerprint"] == expected_input
        assert result["source_seed_fingerprint"] == expected_source
        assert result["cp_sat_random_seed"] == expected_seed
        assert result["starting_value"] == float(start)
        assert result["final_value"] == float(candidate)
        assert result["candidate_found"] is True
        assert result["candidate_validated"] is True
        assert result["candidate_adopted"] is True
        assert result["validation_classification"] == "validated"
        assert attempt["search_time_limit_seconds"] == 300.0
        assert attempt["candidate_validation_time_limit_seconds"] == 180.0
        assert attempt["validation_requested_time_limit_seconds"] <= 180.0
        assert attempt["remaining_stage2_budget_at_validation_start"] > 0
