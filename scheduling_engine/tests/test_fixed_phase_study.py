import json

import pytest

from scheduling_engine.student_assignment.fixed_phase_study import (
    FixedPhaseArtifactWriter,
    FixedPhaseStudyContract,
    create_lineage,
    hash_tree,
    sha256_file,
)


def _contract():
    return FixedPhaseStudyContract(
        lineage_id="test-lineage",
        source_path="source.json.gz",
        source_file_sha256="source-sha",
        source_fingerprint="source-fp",
        materialized_source_fingerprint="materialized-fp",
        input_fingerprint="input-fp",
        model_fingerprint="model-fp",
    )


def test_lineage_creation_is_exclusive_and_sealed_files_are_excluded(tmp_path):
    root = create_lineage(
        tmp_path / "lineage",
        lineage_id="test-lineage",
        confirm_lineage_id="test-lineage",
    )
    writer = FixedPhaseArtifactWriter(
        root,
        contract=_contract(),
        branch_id="r16_only",
    )
    writer.write_manifest(status="preflight_passed")
    writer.write_state({"schema": "fixed_family_controller_state_v1"})
    writer.write_hash_manifest()
    writer.write_seal()

    hashes = hash_tree(root)

    assert "study_manifest.json" in hashes
    assert "SEALED" not in hashes
    with pytest.raises(FileExistsError):
        create_lineage(root, lineage_id="second")


def test_writer_persists_attempt_events_and_state_atomically(tmp_path):
    root = create_lineage(
        tmp_path / "lineage",
        lineage_id="test-lineage",
        confirm_lineage_id="test-lineage",
    )
    writer = FixedPhaseArtifactWriter(
        root,
        contract=_contract(),
        branch_id="hybrid",
    )
    event = {
        "schema": "fixed_family_attempt_event_v1",
        "event_type": "attempt_completed",
        "attempt": {"adopted": True, "gain": 6.0},
    }

    writer.record_event(event)
    writer.write_state({"attempt_count": 1}, status="running")

    attempt_path = root / "branches" / "hybrid" / "attempts" / "attempt_0001.json"
    assert json.loads(attempt_path.read_text(encoding="utf-8"))["attempt"]["gain"] == 6.0
    assert json.loads(
        (root / "branches" / "hybrid" / "branch_state.json").read_text(
            encoding="utf-8"
        )
    )["controller"]["attempt_count"] == 1


def test_writer_publishes_checkpoint_sidecar_and_seal_manifest(tmp_path):
    root = create_lineage(
        tmp_path / "lineage",
        lineage_id="test-lineage",
        confirm_lineage_id="test-lineage",
    )
    writer = FixedPhaseArtifactWriter(
        root,
        contract=_contract(),
        branch_id="r16_only",
    )

    checkpoint = writer.write_checkpoint(
        "incumbent_0001.json",
        {"source_fingerprint": "source-fp"},
    )
    checkpoint_path = (
        root / "branches" / "r16_only" / "checkpoints" / "incumbent_0001.json"
    )
    assert checkpoint["sha256"] == sha256_file(checkpoint_path)
    targeting = writer.write_targeting_snapshot(
        1,
        {"schema": "adaptive_targeting_snapshot_v1", "selected_student_ids": [1, 2]},
    )
    assert targeting["path"].endswith("attempt_0001_targeting.json.gz")
    assert targeting["compressed_bytes"] > 0
    writer.write_manifest(status="complete")
    writer.write_hash_manifest()
    seal = writer.write_seal()
    assert seal["artifact_hashes_sha256"] == sha256_file(
        root / "artifact_hashes.sha256"
    )

def test_lineage_rejects_mismatched_confirmation_and_forbidden_overlap(tmp_path):
    with pytest.raises(ValueError, match="confirmation"):
        create_lineage(
            tmp_path / "lineage",
            lineage_id="one",
            confirm_lineage_id="two",
        )

    historical = tmp_path / "historical"
    historical.mkdir()
    with pytest.raises(ValueError, match="forbidden"):
        create_lineage(
            historical / "child",
            lineage_id="one",
            confirm_lineage_id="one",
            historical_roots=(historical,),
        )
