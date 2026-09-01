import json
import threading
import time

import pytest

import scheduling_engine.benchmark_policy_generalization as policy_study


def _manifest(*, policies=None, seeds=None, scenarios=None):
    policies = tuple(policies or policy_study.PARALLEL_POLICY_STUDY_POLICIES)
    seeds = tuple(seeds or policy_study.PARALLEL_POLICY_STUDY_SEEDS)
    scenarios = tuple(scenarios or ("reference_target",))
    return {
        "schema": policy_study.PARALLEL_POLICY_STUDY_SCHEMA,
        "study_id": policy_study.PARALLEL_POLICY_STUDY_ID,
        "policies": list(policies),
        "seeds": list(seeds),
        "scenario_ids": list(scenarios),
        "scenarios": {
            scenario: {
                "benchmark_directory": "unused",
                "input_fingerprint": "input",
                "source_seed_fingerprint": "seed",
            }
            for scenario in scenarios
        },
        "policy_fingerprints": {
            policy: f"fingerprint-{policy}" for policy in policies
        },
        "budget_contract": {"protocol_version": "test"},
        "results": {},
        "batches": [],
        "concurrency_history": [],
    }


def test_parallel_policy_contract_has_four_cells_and_one_solver_worker():
    contract = policy_study.parallel_policy_budget_contract()

    assert contract["policies"] == [
        "adaptive_balanced",
        "adaptive_student_pressure_biased",
        "adaptive_utilization_biased",
        "fixed_cycle",
    ]
    assert "stateless_role" not in contract["policies"]
    assert contract["cp_sat_workers_per_trial"] == 1
    assert contract["default_max_parallel_trials"] == 2
    assert contract["progressive_concurrency"] == [2, 3, 4]


def test_parallel_policy_fingerprints_distinguish_policy_variants():
    fingerprints = {
        policy_study.parallel_policy_fingerprint(policy)
        for policy in policy_study.PARALLEL_POLICY_STUDY_POLICIES
    }

    assert len(fingerprints) == 4


def test_parallel_cell_forwards_one_cp_sat_worker_and_shared_seed(monkeypatch):
    manifest = _manifest(
        policies=("adaptive_student_pressure_biased",),
        seeds=(202,),
    )
    captured = {}

    def fake_trial(**kwargs):
        captured.update(kwargs)
        return {
            "execution_status": "completed",
            "candidate_complete": True,
            "final_substantive_value": 12,
        }

    monkeypatch.setattr(policy_study, "run_supervised_calibration_trial", fake_trial)
    payload = policy_study.execute_parallel_policy_cell(
        manifest=manifest,
        scenario_id="reference_target",
        policy="adaptive_student_pressure_biased",
        seed=202,
    )

    assert captured["worker_count"] == 1
    assert captured["cp_sat_random_seed"] == 202
    assert captured["profile"] == "balanced"
    assert captured["startup_aware"] is True
    assert payload["adaptive_policy_variant"] == "student_pressure_biased"
    assert payload["policy_fingerprint"] == "fingerprint-adaptive_student_pressure_biased"


def test_parallel_concurrency_qualification_requires_measured_headroom():
    payload = {
        "execution_status": "completed",
        "supervision": {"cleanup": {"descendants_clean": True}},
    }
    qualified = policy_study.qualify_parallel_concurrency(
        {
            "per_trial_peak_tree_rss_bytes": {"a": 1 * 1024**3, "b": 2 * 1024**3},
            "minimum_available_memory_bytes": 8 * 1024**3,
            "swap_growth_bytes": 0,
            "logical_cpu_count": 16,
            "max_cpu_percent": 50,
        },
        completed_payloads=(payload, payload),
    )
    assert qualified["qualified"] is True
    assert qualified["projected_available_after_one_more_trial_bytes"] == 6 * 1024**3

    rejected = policy_study.qualify_parallel_concurrency(
        {
            "per_trial_peak_tree_rss_bytes": {"a": 3 * 1024**3},
            "minimum_available_memory_bytes": 4 * 1024**3,
            "swap_growth_bytes": 0,
            "logical_cpu_count": 16,
            "max_cpu_percent": 50,
        },
        completed_payloads=(payload,),
    )
    assert rejected["qualified"] is False
    assert "projected_memory_reserve_below_2_gib" in rejected["reasons"]


def test_parallel_batch_runs_at_most_requested_process_count_and_does_not_persist(
    monkeypatch,
):
    manifest = _manifest(
        policies=("adaptive_balanced",),
        seeds=(101, 202, 303),
    )
    active = 0
    peak_active = 0
    active_lock = threading.Lock()

    def fake_snapshot(_pid):
        return {
            "tree_rss_bytes": 1 * 1024**3,
            "tree_uss_bytes": 512 * 1024**2,
            "system_available_memory_bytes": 8 * 1024**3,
            "pids": [int(_pid)],
        }

    def fake_execute(**kwargs):
        nonlocal active, peak_active
        kwargs["worker_started_callback"](1000 + int(kwargs["seed"]))
        with active_lock:
            active += 1
            peak_active = max(peak_active, active)
        time.sleep(0.35)
        with active_lock:
            active -= 1
        return {
            "execution_status": "completed",
            "candidate_complete": True,
            "supervision": {"cleanup": {"descendants_clean": True}},
        }

    monkeypatch.setattr(policy_study, "process_tree_snapshot", fake_snapshot)
    monkeypatch.setattr(policy_study, "execute_parallel_policy_cell", fake_execute)
    monkeypatch.setattr(
        policy_study,
        "_parallel_resource_snapshot",
        policy_study._parallel_resource_snapshot,
    )

    result = policy_study.run_parallel_policy_batch(
        manifest=manifest,
        max_parallel_trials=2,
        cells=(
            {"scenario_id": "reference_target", "policy": "adaptive_balanced", "seed": 101},
            {"scenario_id": "reference_target", "policy": "adaptive_balanced", "seed": 202},
            {"scenario_id": "reference_target", "policy": "adaptive_balanced", "seed": 303},
        ),
    )

    assert peak_active == 2
    assert len(result["payloads"]) == 3
    assert all(
        payload["parallel_execution"]["parent_owned_manifest"]
        for payload in result["payloads"]
    )
    assert manifest["results"] == {}


def test_parallel_batch_rejects_unqualified_three_trial_request():
    with pytest.raises(ValueError, match="prior measured qualification"):
        policy_study.run_parallel_policy_batch(
            manifest=_manifest(),
            max_parallel_trials=3,
            cells=({"scenario_id": "reference_target", "policy": "adaptive_balanced", "seed": 101},),
        )


def test_parallel_batch_exposes_parent_cancellation_to_each_supervised_cell(
    monkeypatch,
):
    manifest = _manifest(
        policies=("adaptive_balanced",),
        seeds=(101,),
    )
    observed = []

    def fake_execute(**kwargs):
        observed.append(kwargs["cancel_requested"]())
        return {
            "execution_status": "parent_cancelled",
            "candidate_complete": False,
            "supervision": {"cleanup": {"descendants_clean": True}},
        }

    monkeypatch.setattr(policy_study, "execute_parallel_policy_cell", fake_execute)
    result = policy_study.run_parallel_policy_batch(
        manifest=manifest,
        max_parallel_trials=2,
        cells=({"scenario_id": "reference_target", "policy": "adaptive_balanced", "seed": 101},),
        cancel_requested=lambda: True,
    )

    assert observed == [True]
    assert result["payloads"][0]["execution_status"] == "parent_cancelled"


def test_parallel_study_expands_only_after_a_qualified_batch(monkeypatch, tmp_path):
    manifest = _manifest(
        policies=("adaptive_balanced",),
        seeds=(101, 202, 303),
    )
    manifest_path = tmp_path / "study_manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    requested = []
    qualification_flags = []

    def fake_batch(**kwargs):
        requested.append(kwargs["max_parallel_trials"])
        qualification_flags.append(
            kwargs["qualified_for_requested_parallelism"]
        )
        payloads = [
            {
                "scenario_id": cell["scenario_id"],
                "policy": cell["policy"],
                "seed": cell["seed"],
                "execution_status": "completed",
                "candidate_complete": True,
            }
            for cell in kwargs["cells"]
        ]
        return {
            "payloads": payloads,
            "resource": {},
            "qualification": {
                "qualified": len(requested) == 1,
                "reasons": [] if len(requested) == 1 else ["test_stop"],
            },
        }

    monkeypatch.setattr(policy_study, "run_parallel_policy_batch", fake_batch)
    monkeypatch.setattr(
        policy_study,
        "_persist_parallel_policy_result",
        lambda _directory, value, payload: value["results"].update({
            policy_study._result_filename(
                payload["scenario_id"], payload["policy"], payload["seed"]
            ): {"status": "completed"}
        }) or {"filename": "test.json"},
    )

    result = policy_study.run_parallel_policy_study(
        tmp_path,
        max_parallel_trials=4,
    )

    assert requested == [2, 3]
    assert qualification_flags == [True, True]
    assert result["concurrency_history"] == [
        {"completed_parallel_trials": 2, "qualified_for_next_slot": True, "reasons": []},
        {"completed_parallel_trials": 3, "qualified_for_next_slot": False, "reasons": ["test_stop"]},
    ]
    assert requested[-1] == 3


def test_parallel_study_returns_to_last_qualified_level_after_failed_expansion(
    monkeypatch, tmp_path
):
    manifest = _manifest(
        policies=policy_study.PARALLEL_POLICY_STUDY_POLICIES,
        seeds=(101, 202, 303),
    )
    manifest_path = tmp_path / "study_manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    requested = []
    qualification_flags = []

    def fake_batch(**kwargs):
        requested.append(kwargs["max_parallel_trials"])
        qualification_flags.append(
            kwargs["qualified_for_requested_parallelism"]
        )
        payloads = [
            {
                "scenario_id": cell["scenario_id"],
                "policy": cell["policy"],
                "seed": cell["seed"],
                "execution_status": "completed",
                "candidate_complete": True,
            }
            for cell in kwargs["cells"]
        ]
        call_number = len(requested)
        return {
            "payloads": payloads,
            "resource": {},
            "qualification": {
                "qualified": call_number <= 2,
                "reasons": [] if call_number <= 2 else ["test_stop"],
            },
        }

    monkeypatch.setattr(policy_study, "run_parallel_policy_batch", fake_batch)
    monkeypatch.setattr(
        policy_study,
        "_persist_parallel_policy_result",
        lambda _directory, value, payload: value["results"].update({
            policy_study._result_filename(
                payload["scenario_id"], payload["policy"], payload["seed"]
            ): {"status": "completed"}
        }) or {"filename": "test.json"},
    )

    result = policy_study.run_parallel_policy_study(
        tmp_path,
        max_parallel_trials=4,
    )

    assert requested == [2, 3, 4, 3]
    assert qualification_flags == [True, True, True, True]
    assert result["concurrency_history"][-2:] == [
        {"completed_parallel_trials": 4, "qualified_for_next_slot": False, "reasons": ["test_stop"]},
        {"completed_parallel_trials": 3, "qualified_for_next_slot": False, "reasons": ["test_stop"]},
    ]
