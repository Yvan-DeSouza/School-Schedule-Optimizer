"""Tests for the offline calibration worker execution boundary."""

import json
from pathlib import Path
import sys

from scheduling_engine.student_assignment.calibration_supervisor import (
    CalibrationExecutionProfile,
    EXECUTION_COMPLETED,
    EXECUTION_HARD_DEADLINE_TERMINATED,
    EXECUTION_PARENT_CANCELLED,
    EXECUTION_RESOURCE_GUARD_TERMINATED,
    EXECUTION_WORKER_CRASHED,
    EXECUTION_WORKER_PROTOCOL_ERROR,
    SupervisedWorkerResult,
    supervise_json_worker,
)


def _python_worker(source, *arguments):
    return [sys.executable, "-c", source, *map(str, arguments)]


def test_supervisor_accepts_complete_worker_protocol(tmp_path):
    output = tmp_path / "result.json"
    status = tmp_path / "status.json"
    command = _python_worker(
        "import json, pathlib, sys; "
        "pathlib.Path(sys.argv[1]).write_text(json.dumps({'value': 7, "
        "'output_protocol_complete': True}), encoding='utf-8')",
        output,
    )

    result = supervise_json_worker(
        command,
        output_path=output,
        status_path=status,
        execution_profile=CalibrationExecutionProfile(
            hard_wall_seconds=2,
            termination_grace_seconds=0.2,
            poll_interval_seconds=0.02,
        ),
    )

    assert result.execution_status == EXECUTION_COMPLETED
    assert result.payload["value"] == 7
    assert result.payload["output_protocol_complete"] is True
    assert result.worker_exit_code == 0


def test_supervisor_hard_deadline_terminates_worker_and_descendant(tmp_path):
    output = tmp_path / "result.json"
    child_pid = tmp_path / "child.pid"
    command = _python_worker(
        "import pathlib, subprocess, sys, time; "
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid), encoding='utf-8'); "
        "time.sleep(30)",
        child_pid,
    )

    result = supervise_json_worker(
        command,
        output_path=output,
        execution_profile=CalibrationExecutionProfile(
            hard_wall_seconds=0.25,
            termination_grace_seconds=0.25,
            poll_interval_seconds=0.02,
        ),
    )

    assert result.execution_status == EXECUTION_HARD_DEADLINE_TERMINATED
    assert result.payload is None
    assert result.hard_deadline_elapsed_seconds is not None
    assert result.hard_deadline_elapsed_seconds < 1.5
    assert result.elapsed_seconds < 3.0
    assert result.cleanup["descendants_clean"] is True
    assert result.cleanup["remaining_process_count"] == 0
    if child_pid.exists():
        import psutil

        assert not psutil.pid_exists(int(child_pid.read_text(encoding="utf-8")))


def test_supervisor_resource_guard_terminates_without_candidate(tmp_path):
    output = tmp_path / "result.json"
    command = _python_worker(
        "import time; time.sleep(30)",
    )

    result = supervise_json_worker(
        command,
        output_path=output,
        execution_profile=CalibrationExecutionProfile(
            hard_wall_seconds=2,
            termination_grace_seconds=0.2,
            max_process_tree_rss_bytes=1,
            poll_interval_seconds=0.02,
        ),
    )

    assert result.execution_status == EXECUTION_RESOURCE_GUARD_TERMINATED
    assert result.payload is None
    assert result.resource_guard_facts["trigger"] == (
        "max_process_tree_rss_bytes"
    )
    assert result.cleanup["descendants_clean"] is True


def test_supervisor_distinguishes_crash_and_protocol_failures(tmp_path):
    crashed_output = tmp_path / "crashed.json"
    crashed = supervise_json_worker(
        _python_worker("raise SystemExit(9)"),
        output_path=crashed_output,
        execution_profile=CalibrationExecutionProfile(
            hard_wall_seconds=1,
            poll_interval_seconds=0.02,
        ),
    )
    assert crashed.execution_status == EXECUTION_WORKER_CRASHED
    assert crashed.payload is None

    malformed_output = tmp_path / "malformed.json"
    malformed = supervise_json_worker(
        _python_worker(
            "import pathlib, sys; pathlib.Path(sys.argv[1]).write_text('{', encoding='utf-8')",
            malformed_output,
        ),
        output_path=malformed_output,
        execution_profile=CalibrationExecutionProfile(
            hard_wall_seconds=1,
            poll_interval_seconds=0.02,
        ),
    )
    assert malformed.execution_status == EXECUTION_WORKER_PROTOCOL_ERROR
    assert malformed.payload is None

    incomplete_output = tmp_path / "incomplete.json"
    incomplete = supervise_json_worker(
        _python_worker(
            "import json, pathlib, sys; "
            "pathlib.Path(sys.argv[1]).write_text(json.dumps({'value': 1}), encoding='utf-8')",
            incomplete_output,
        ),
        output_path=incomplete_output,
        execution_profile=CalibrationExecutionProfile(
            hard_wall_seconds=1,
            poll_interval_seconds=0.02,
        ),
    )
    assert incomplete.execution_status == EXECUTION_WORKER_PROTOCOL_ERROR
    assert incomplete.payload is None


def test_supervisor_does_not_accept_stale_output_after_worker_crash(tmp_path):
    output = tmp_path / "result.json"
    output.write_text(
        json.dumps({"output_protocol_complete": True, "stale": True}),
        encoding="utf-8",
    )

    result = supervise_json_worker(
        _python_worker("raise SystemExit(11)"),
        output_path=output,
        execution_profile=CalibrationExecutionProfile(
            hard_wall_seconds=1,
            poll_interval_seconds=0.02,
        ),
    )

    assert result.execution_status == EXECUTION_WORKER_CRASHED
    assert result.payload is None


def test_supervisor_classifies_parent_cancellation(tmp_path):
    output = tmp_path / "result.json"
    result = supervise_json_worker(
        _python_worker("import time; time.sleep(30)"),
        output_path=output,
        execution_profile=CalibrationExecutionProfile(
            hard_wall_seconds=2,
            termination_grace_seconds=0.2,
            poll_interval_seconds=0.02,
        ),
        cancel_requested=lambda: True,
    )

    assert result.execution_status == EXECUTION_PARENT_CANCELLED
    assert result.payload is None
    assert result.cleanup["descendants_clean"] is True


def test_supervised_result_serialization_is_json_safe():
    result = SupervisedWorkerResult(
        execution_status=EXECUTION_HARD_DEADLINE_TERMINATED,
        payload=None,
        worker_pid=123,
        worker_exit_code=None,
        elapsed_seconds=1.0,
    )

    encoded = json.dumps(result.to_dict(), sort_keys=True)
    assert "hard_deadline_terminated" in encoded
