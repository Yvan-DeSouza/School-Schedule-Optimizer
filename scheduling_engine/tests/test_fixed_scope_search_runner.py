"""Non-heavy contract, telemetry, resume, and supervisor tests."""

import json
from pathlib import Path
import subprocess
import sys

import pytest

from scheduling_engine import benchmark_r16_fixed_scope_search_semantics as runner


def _contract():
    scopes = (
        ("ia13", (1, 2, 3, 4)),
        ("ia32", (5, 6, 7, 8)),
        ("ia51", (9, 10, 11, 12)),
        ("ia52", (9, 10, 11, 12)),
        ("top60", (13, 14, 15, 16)),
        ("top27", (17, 18, 19, 20)),
    )
    contract = {
        "schema": "r16_fixed_scope_search_semantics_contract_v1",
        "experiment_id": "test",
        "objective_semantics_version": "v2",
        "objective_importance_scores": {
            "course_category_diversity": 6,
            "course_sequence_preferences": 6,
            "difficulty_balance": 6,
            "section_utilization_balance": 6,
            "student_semester_balance": 6,
        },
        "source_cells": [
            {"id": name, "scope": list(scope),
             "scope_fingerprint": runner.scope_fingerprint(scope),
             "materialized_source_decision_fingerprint": "test"}
            for name, scope in scopes
        ],
        "treatments": [
            "control_first_qualifying",
            "minimum_coordination",
            "iterative_strict_bound_refinement",
            "direct_exact_v2_optimization",
        ],
        "repeat_count": 3,
        "total_cell_count": 72,
        "seed": 101,
        "search_workers": 8,
        "validation_workers": 1,
        "search_budget_seconds": 300,
        "validation_allowance_seconds": 180,
        "neighborhood_radius": 16,
        "max_changed_students": 4,
        "max_changed_source_decisions": 16,
        "code_identity": {
            "prepared_from_git_head": "test-preparation-base",
            "fingerprinted_files": ["scheduling_engine/student_assignment/core.py"],
            "implementation_fingerprint": "test-implementation",
        },
    }
    contract["cell_order"] = runner.expected_cell_order(contract)
    contract["decision_criteria"] = {"practical_gain_points": 12}
    contract["contract_payload_fingerprint"] = runner.contract_payload_fingerprint(contract)
    return contract


def _valid_result(cell):
    return {
        "schema": runner.RESULT_SCHEMA,
        "cell": cell,
        "scope_fingerprint": "scope",
        "model_fingerprint": "model",
        "source": {"sha256": "source", "source_decision_fingerprint": "decisions"},
        "treatment": cell["treatment"],
        "repeat": cell["repeat"],
        "validation": {"classification": "not_attempted_no_complete_candidate"},
        "full_target_population_embedded": False,
    }


def test_frozen_contract_shape_and_balanced_order_are_deterministic():
    contract = _contract()

    assert runner.validate_contract(contract) is True
    assert contract["cell_order"] == runner.expected_cell_order(contract)
    assert len({row["cell_id"] for row in contract["cell_order"]}) == 72
    assert all(
        sum(row["source_cell_id"] == source and row["treatment"] == treatment
            for row in contract["cell_order"]) == 3
        for source in (row["id"] for row in contract["source_cells"])
        for treatment in contract["treatments"]
    )


def test_result_validity_requires_compact_identity_and_never_full_population(tmp_path):
    cell = _contract()["cell_order"][0]
    path = tmp_path / "result.json"
    runner.atomic_write(path, _valid_result(cell))
    assert runner.completed_valid_result(path, cell)

    payload = _valid_result(cell)
    payload["full_target_population_embedded"] = True
    runner.atomic_write(path, payload)
    assert not runner.completed_valid_result(path, cell)


@pytest.mark.parametrize(
    "change,expected",
    [
        ({}, True),
        ({"validation_status": "unknown"}, False),
        ({"unmet_request_count": 1}, False),
        ({"validated_candidate_fingerprint": "different"}, False),
        ({"after_value": 100}, False),
        ({"stage2_facts": {}}, False),
    ],
)
def test_authority_is_unchanged_full_validation_plus_strict_gain(change, expected):
    facts = {
        "validation_status": "complete",
        "unmet_request_count": 0,
        "stage2_facts": {"alternate_seed_validated": True},
        "expected_candidate_fingerprint": "candidate",
        "validated_candidate_fingerprint": "candidate",
        "before_value": 100,
        "after_value": 88,
    }
    facts.update(change)
    assert runner.candidate_is_authoritative(**facts) is expected


def test_resume_skips_an_existing_valid_cell_without_launching(monkeypatch, tmp_path):
    contract = _contract()
    cell = contract["cell_order"][0]
    contract["cell_order"] = [cell]
    contract["total_cell_count"] = 1
    root = tmp_path / "lineage"
    result_path = root / "cells" / cell["cell_id"] / "valid_result.json"
    result_path.parent.mkdir(parents=True)
    runner.atomic_write(result_path, _valid_result(cell))
    runner.atomic_write(root / "runner_state.json", {
        "completed_valid_cells": [cell["cell_id"]],
        "invalid_executions": [],
    })
    monkeypatch.setattr(runner, "load_contract", lambda _path: contract)
    monkeypatch.setattr(
        runner, "supervise_cell",
        lambda *_args, **_kwargs: pytest.fail("valid cell was silently rerun"),
    )

    assert runner.run_grid(tmp_path / "unused.json", root) == 1
    events = (root / "logs" / "runner_events.jsonl").read_text(encoding="utf-8")
    assert "valid_cell_skipped_on_resume" in events


def test_external_monitor_streams_compact_resource_samples_and_cleans_process(tmp_path):
    execution = tmp_path / "execution"
    execution.mkdir()
    result_path = execution / "result.json"
    script = (
        "from pathlib import Path; import time; payload = bytearray(8 * 1024 * 1024); "
        "time.sleep(0.08); "
        f"Path({str(result_path)!r}).write_text('{{}}', encoding='utf-8')"
    )
    contract = {
        "cell_hard_wall_seconds": 2,
        "resource_contract": {
            "sample_interval_seconds": 0.02,
            "sleep_gap_seconds": 2,
            "max_tree_rss_bytes": 2**40,
            "min_available_memory_bytes": 1,
        },
    }

    event = runner.supervise_cell([sys.executable, "-c", script], execution, contract)

    assert event["classification"] == "completed"
    assert event["cleanup"]["descendants_clean"] is True
    assert event["peak_tree_rss_bytes"] > 8 * 1024 * 1024
    samples = (execution / "resource_samples.jsonl").read_text(encoding="utf-8").splitlines()
    assert samples
    assert all(len(line) < 4096 for line in samples)
    first = json.loads(samples[0])
    assert {"utc_timestamp", "phase", "tree_rss_bytes", "tree_uss_bytes",
            "system_available_memory_bytes", "process_count"} <= set(first)


def test_supervisor_cleans_an_observed_child_after_root_exits(tmp_path):
    execution = tmp_path / "execution"
    execution.mkdir()
    result_path = execution / "result.json"
    child_code = "import time; time.sleep(10)"
    script = (
        "from pathlib import Path; import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
        "time.sleep(0.12); "
        f"Path({str(result_path)!r}).write_text('{{}}', encoding='utf-8')"
    )
    contract = {
        "cell_hard_wall_seconds": 2,
        "resource_contract": {
            "sample_interval_seconds": 0.02,
            "sleep_gap_seconds": 2,
            "max_tree_rss_bytes": 2**40,
            "min_available_memory_bytes": 1,
        },
    }

    event = runner.supervise_cell([sys.executable, "-c", script], execution, contract)

    assert event["classification"] == "completed"
    assert len(event["observed_pids"]) >= 2
    assert event["cleanup"]["initial_process_count"] >= 1
    assert event["cleanup"]["descendants_clean"] is True


def test_default_contract_has_the_six_frozen_scopes_when_present():
    path = runner.REPOSITORY_ROOT / "research/contracts/r16_fixed_scope_search_semantics_v1.json"
    if not path.exists():
        pytest.skip("contract is added later in the preparation sequence")
    contract = runner.load_contract(path)
    assert {row["id"]: row["scope"] for row in contract["source_cells"]} == {
        "ia13": [141, 571, 741, 1221],
        "ia32": [76, 486, 676, 886],
        "ia51": [14, 84, 954, 1134],
        "ia52": [14, 84, 954, 1134],
        "top60": [67, 87, 597, 1392],
        "top27": [263, 273, 283, 1170],
    }


def test_code_identity_accepts_prepare_commit_descendant_when_clean():
    assert runner.code_identity_is_authorized(
        ancestry_ok=True,
        worktree_clean=True,
        implementation_fingerprint_matches=True,
    )


def test_code_identity_rejects_descendant_with_implementation_drift():
    assert not runner.code_identity_is_authorized(
        ancestry_ok=True,
        worktree_clean=True,
        implementation_fingerprint_matches=False,
    )


def test_code_identity_rejects_dirty_worktree():
    assert not runner.code_identity_is_authorized(
        ancestry_ok=True,
        worktree_clean=False,
        implementation_fingerprint_matches=True,
    )


def test_code_identity_rejects_non_descendant_lineage():
    assert not runner.code_identity_is_authorized(
        ancestry_ok=False,
        worktree_clean=True,
        implementation_fingerprint_matches=True,
    )


def test_execution_head_is_lineage_provenance_not_contract_identity():
    contract = _contract()
    identity = {
        "execution_git_head": "execution-head",
        "implementation_fingerprint": "test-implementation",
        "worktree_clean": True,
    }

    provenance = runner.execution_provenance(contract, identity)

    assert provenance["prepared_from_git_head"] == "test-preparation-base"
    assert provenance["execution_git_head"] == "execution-head"
    assert "execution_git_head" not in contract
    assert "execution_git_head" not in contract["code_identity"]
