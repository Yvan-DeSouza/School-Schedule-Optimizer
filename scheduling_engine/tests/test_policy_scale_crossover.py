import hashlib
import json
from pathlib import Path


STUDY_DIRECTORY = (
    Path(__file__).parents[1]
    / "benchmarks"
    / "student_assignment"
    / "v2_policy_scale_crossover_20260830"
)


def _read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def test_scale_crossover_lineage_is_bound_to_declared_target_and_source():
    manifest = _read_json(STUDY_DIRECTORY / "study_manifest.json")
    gate = manifest["target_worker_gate"]
    assert manifest["status"] == "completed_artifact_forensics_and_seed101_worker_gate"
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
    for filename, expected in manifest["results"].items():
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


def test_scale_crossover_source_artifact_hashes_match_declared_lineage():
    manifest = _read_json(STUDY_DIRECTORY / "study_manifest.json")
    source_root = STUDY_DIRECTORY.parent / "v2_policy_generalization_suite_20260829"
    for relative_path, expected_hash in manifest["source_artifact_sha256"].items():
        source_path = source_root / relative_path
        assert source_path.exists()
        assert _sha256(source_path) == expected_hash
