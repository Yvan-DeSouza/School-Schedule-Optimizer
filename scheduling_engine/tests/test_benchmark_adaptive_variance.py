from types import SimpleNamespace

from scheduling_engine import benchmark_adaptive_variance as variance
from scheduling_engine.student_assignment.calibration_supervisor import (
    EXECUTION_COMPLETED,
    EXECUTION_HARD_DEADLINE_TERMINATED,
    SupervisedWorkerResult,
)


def _fake_benchmark():
    return {
        "data": SimpleNamespace(time_limit_seconds=30.0),
        "manifest": {
            "seed_source_decision_fingerprint": "seed-fingerprint",
            "counts": {
                "assignment_count": 10,
                "unmet_required_request_count": 0,
                "special_commitment_count": 2,
            },
            "stage1": {"substantive_value": 37596},
        },
    }


def test_supervised_variance_trial_returns_completed_worker_payload(monkeypatch):
    monkeypatch.setattr(
        variance,
        "read_durable_stage2_benchmark",
        lambda _directory: _fake_benchmark(),
    )
    monkeypatch.setattr(
        variance,
        "supervise_json_worker",
        lambda *args, **kwargs: SupervisedWorkerResult(
            execution_status=EXECUTION_COMPLETED,
            payload={"candidate_adopted": True, "value": 37590},
            worker_pid=12,
            worker_exit_code=0,
            elapsed_seconds=1.0,
        ),
    )

    result = variance.run_supervised_variance_trial(
        "benchmark",
        operator="targeted_utilization_r16_s4",
        selected_student_ids=(417, 360, 482, 25),
        total_time_limit_seconds=5,
        per_attempt_time_limit_seconds=2,
    )

    assert result["execution_status"] == EXECUTION_COMPLETED
    assert result["candidate_adopted"] is True
    assert result["value"] == 37590
    assert result["supervision"]["worker_pid"] == 12


def test_supervised_variance_trial_never_authorizes_terminated_worker(
    monkeypatch,
):
    monkeypatch.setattr(
        variance,
        "read_durable_stage2_benchmark",
        lambda _directory: _fake_benchmark(),
    )
    monkeypatch.setattr(
        variance,
        "semantic_student_assignment_input_fingerprint",
        lambda _data: "input-fingerprint",
    )
    monkeypatch.setattr(
        variance,
        "supervise_json_worker",
        lambda *args, **kwargs: SupervisedWorkerResult(
            execution_status=EXECUTION_HARD_DEADLINE_TERMINATED,
            payload=None,
            worker_pid=13,
            worker_exit_code=None,
            elapsed_seconds=5.1,
        ),
    )

    result = variance.run_supervised_variance_trial(
        "benchmark",
        operator="targeted_utilization_r16_s4",
        selected_student_ids=(417, 360, 482, 25),
        total_time_limit_seconds=5,
        per_attempt_time_limit_seconds=2,
    )

    assert result["execution_status"] == EXECUTION_HARD_DEADLINE_TERMINATED
    assert result["candidate_found"] is False
    assert result["candidate_validated"] is False
    assert result["candidate_adopted"] is False
    assert result["candidate_diagnostic"] == (
        "not_authoritative_after_worker_termination"
    )
    assert result["initial_substantive_value"] == 37596
