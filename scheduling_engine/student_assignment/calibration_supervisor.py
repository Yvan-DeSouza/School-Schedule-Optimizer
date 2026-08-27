"""Process supervision for offline adaptive-calibration experiments.

This module is diagnostic infrastructure only.  It does not build scheduling
models, validate candidates, or decide whether a schedule is acceptable.  A
parent process supervises one short-lived JSON-producing worker so an
uncooperative model-construction or native-runtime operation cannot invalidate
matched policy budgets.  The caller remains responsible for retaining the
known validated incumbent when the worker is stopped.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
import signal
import subprocess
import time


EXECUTION_COMPLETED = "completed"
EXECUTION_SOFT_BUDGET_EXHAUSTED = "soft_budget_exhausted"
EXECUTION_HARD_DEADLINE_TERMINATED = "hard_deadline_terminated"
EXECUTION_RESOURCE_GUARD_TERMINATED = "resource_guard_terminated"
EXECUTION_WORKER_CRASHED = "worker_crashed"
EXECUTION_WORKER_PROTOCOL_ERROR = "worker_protocol_error"
EXECUTION_PARENT_CANCELLED = "parent_cancelled"


@dataclass(frozen=True)
class CalibrationExecutionProfile:
    """External limits for one offline calibration worker."""

    hard_wall_seconds: float = 1800.0
    termination_grace_seconds: float = 5.0
    max_process_tree_rss_bytes: int | None = None
    min_system_available_memory_bytes: int | None = None
    poll_interval_seconds: float = 0.25

    def __post_init__(self):
        if self.hard_wall_seconds <= 0:
            raise ValueError("hard_wall_seconds must be positive")
        if self.termination_grace_seconds < 0:
            raise ValueError("termination_grace_seconds cannot be negative")
        if self.max_process_tree_rss_bytes is not None and (
            self.max_process_tree_rss_bytes <= 0
        ):
            raise ValueError("max_process_tree_rss_bytes must be positive")
        if self.min_system_available_memory_bytes is not None and (
            self.min_system_available_memory_bytes < 0
        ):
            raise ValueError(
                "min_system_available_memory_bytes cannot be negative"
            )
        if self.poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")


TARGET_SCALE_CALIBRATION_EXECUTION_PROFILE = CalibrationExecutionProfile(
    hard_wall_seconds=1800.0,
    termination_grace_seconds=5.0,
    max_process_tree_rss_bytes=4 * 1024**3,
    min_system_available_memory_bytes=1536 * 1024**2,
    poll_interval_seconds=0.25,
)


@dataclass(frozen=True)
class SupervisedWorkerResult:
    """JSON-safe facts from one supervised worker execution."""

    execution_status: str
    payload: dict | None
    worker_pid: int | None
    worker_exit_code: int | None
    elapsed_seconds: float
    hard_deadline_elapsed_seconds: float | None = None
    resource_guard_facts: dict = field(default_factory=dict)
    resource_snapshot: dict = field(default_factory=dict)
    last_worker_phase: dict = field(default_factory=dict)
    cleanup: dict = field(default_factory=dict)
    protocol_error: str | None = None

    def to_dict(self):
        return asdict(self)


def _write_json_atomically(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    temporary.replace(path)


def _read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _psutil_module():
    try:
        import psutil
    except ImportError:
        return None
    return psutil


def process_tree_snapshot(pid):
    """Return one low-frequency process-tree/resource snapshot."""

    psutil = _psutil_module()
    if psutil is None:
        return {
            "available": False,
            "reason": "psutil_unavailable",
            "root_pid": int(pid),
            "process_count": None,
            "tree_rss_bytes": None,
            "tree_uss_bytes": None,
            "system_available_memory_bytes": None,
            "pids": [],
        }
    processes = []
    try:
        root = psutil.Process(int(pid))
        processes = [root, *root.children(recursive=True)]
    except (psutil.Error, OSError, ValueError):
        processes = []
    rss = 0
    uss = 0
    alive = 0
    for process in processes:
        try:
            rss += int(process.memory_info().rss)
            try:
                uss += int(process.memory_full_info().uss)
            except (AttributeError, psutil.Error, OSError):
                pass
            alive += 1
        except (psutil.Error, OSError):
            continue
    try:
        available = int(psutil.virtual_memory().available)
    except (psutil.Error, OSError, AttributeError):
        available = None
    return {
        "available": True,
        "root_pid": int(pid),
        "process_count": alive,
        "tree_rss_bytes": rss,
        "tree_uss_bytes": uss or None,
        "system_available_memory_bytes": available,
        "pids": [int(process.pid) for process in processes],
    }


def _descendants(pid):
    psutil = _psutil_module()
    if psutil is None:
        return []
    try:
        return list(psutil.Process(int(pid)).children(recursive=True))
    except (psutil.Error, OSError, ValueError):
        return []


def _remaining_processes(pid):
    psutil = _psutil_module()
    if psutil is None:
        return []
    processes = _descendants(pid)
    try:
        root = psutil.Process(int(pid))
        if root.is_running():
            processes.append(root)
    except (psutil.Error, OSError, ValueError):
        pass
    return processes


def terminate_process_tree(pid, *, grace_seconds=5.0):
    """Terminate a worker and descendants, escalating after the grace period."""

    started = time.monotonic()
    descendants = _descendants(pid)
    processes = [*descendants]
    psutil = _psutil_module()
    if psutil is not None:
        try:
            root = psutil.Process(int(pid))
            processes.append(root)
        except (psutil.Error, OSError, ValueError):
            pass
    else:
        processes = []

    for process in reversed(processes):
        try:
            process.terminate()
        except Exception:  # pragma: no cover - process may disappear mid-cleanup
            pass

    remaining = []
    if psutil is not None and processes:
        _gone, remaining = psutil.wait_procs(
            processes,
            timeout=max(0.0, float(grace_seconds)),
        )
    elif os.name == "nt":
        # The psutil path is used on the supported Windows benchmark host.  A
        # taskkill fallback keeps cleanup useful if psutil cannot be imported.
        subprocess.run(
            ["taskkill", "/PID", str(int(pid)), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    elif processes:
        try:
            os.kill(int(pid), signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass

    for process in list(remaining) + _remaining_processes(pid):
        try:
            process.kill()
        except Exception:  # pragma: no cover - process may disappear mid-cleanup
            pass
    if psutil is not None:
        remaining_after_kill = psutil.wait_procs(
            list(_remaining_processes(pid)),
            timeout=max(0.1, float(grace_seconds)),
        )[1]
    else:
        remaining_after_kill = []
    return {
        "requested_pid": int(pid),
        "initial_process_count": len(processes),
        "remaining_process_count": len(remaining_after_kill),
        "cleanup_elapsed_seconds": time.monotonic() - started,
        "descendants_clean": not remaining_after_kill,
    }


def _cleanup_observed_descendants(root_pid, observed_pids, *, grace_seconds):
    """Clean descendants seen before a normally exiting root disappeared."""

    started = time.monotonic()
    candidates = []
    psutil = _psutil_module()
    if psutil is not None:
        for pid in sorted(set(observed_pids) - {int(root_pid)}):
            try:
                process = psutil.Process(int(pid))
                if process.is_running():
                    candidates.append(process)
            except (psutil.Error, OSError, ValueError):
                continue
    for process in candidates:
        try:
            process.terminate()
        except Exception:  # pragma: no cover - process may disappear
            pass
    remaining = []
    if psutil is not None and candidates:
        _gone, remaining = psutil.wait_procs(
            candidates,
            timeout=max(0.0, float(grace_seconds)),
        )
        for process in remaining:
            try:
                process.kill()
            except Exception:  # pragma: no cover - process may disappear
                pass
        if remaining:
            remaining = psutil.wait_procs(
                remaining,
                timeout=max(0.1, float(grace_seconds)),
            )[1]
    return {
        "requested_pid": int(root_pid),
        "initial_process_count": len(candidates),
        "remaining_process_count": len(remaining),
        "cleanup_elapsed_seconds": time.monotonic() - started,
        "descendants_clean": not remaining,
    }


def _launch_worker(command, *, cwd=None, env=None):
    kwargs = {
        "cwd": cwd,
        "env": env,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(list(command), **kwargs)


def _read_worker_phase(path):
    if not path or not Path(path).exists():
        return {}
    try:
        value = _read_json(path)
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def supervise_json_worker(
    command,
    *,
    output_path,
    execution_profile=None,
    status_path=None,
    cwd=None,
    env=None,
    cancel_requested=None,
):
    """Run one JSON worker under a hard wall and optional resource guard."""

    profile = execution_profile or CalibrationExecutionProfile()
    output_path = Path(output_path)
    status_path = Path(status_path) if status_path else None
    # A caller may reuse a result path across trials.  A previous successful
    # payload must never be mistaken for this worker's output after a crash or
    # hard termination.
    for stale_path in (output_path, status_path):
        if stale_path is not None:
            try:
                stale_path.unlink(missing_ok=True)
            except OSError:
                pass
    started = time.monotonic()
    process = _launch_worker(command, cwd=cwd, env=env)
    worker_pid = int(process.pid)
    resource_guard_facts = {}
    last_snapshot = {}
    execution_status = None
    cleanup = {}
    observed_pids = {worker_pid}
    while True:
        elapsed = time.monotonic() - started
        return_code = process.poll()
        if return_code is not None:
            if elapsed > profile.hard_wall_seconds:
                execution_status = EXECUTION_HARD_DEADLINE_TERMINATED
                cleanup = terminate_process_tree(
                    worker_pid,
                    grace_seconds=profile.termination_grace_seconds,
                )
                break
            break
        last_snapshot = process_tree_snapshot(worker_pid)
        observed_pids.update(last_snapshot.get("pids") or ())
        if cancel_requested is not None and cancel_requested():
            execution_status = EXECUTION_PARENT_CANCELLED
            cleanup = terminate_process_tree(
                worker_pid,
                grace_seconds=profile.termination_grace_seconds,
            )
            break
        rss_limit = profile.max_process_tree_rss_bytes
        available_limit = profile.min_system_available_memory_bytes
        if (
            rss_limit is not None
            and last_snapshot.get("tree_rss_bytes") is not None
            and last_snapshot["tree_rss_bytes"] > rss_limit
        ):
            execution_status = EXECUTION_RESOURCE_GUARD_TERMINATED
            resource_guard_facts = {
                "trigger": "max_process_tree_rss_bytes",
                "limit": rss_limit,
                "measurement": last_snapshot.get("tree_rss_bytes"),
                "snapshot": dict(last_snapshot),
                "elapsed_seconds": elapsed,
            }
            cleanup = terminate_process_tree(
                worker_pid,
                grace_seconds=profile.termination_grace_seconds,
            )
            break
        if (
            available_limit is not None
            and last_snapshot.get("system_available_memory_bytes") is not None
            and last_snapshot["system_available_memory_bytes"] < available_limit
        ):
            execution_status = EXECUTION_RESOURCE_GUARD_TERMINATED
            resource_guard_facts = {
                "trigger": "min_system_available_memory_bytes",
                "limit": available_limit,
                "measurement": last_snapshot.get(
                    "system_available_memory_bytes"
                ),
                "snapshot": dict(last_snapshot),
                "elapsed_seconds": elapsed,
            }
            cleanup = terminate_process_tree(
                worker_pid,
                grace_seconds=profile.termination_grace_seconds,
            )
            break
        if elapsed >= profile.hard_wall_seconds:
            execution_status = EXECUTION_HARD_DEADLINE_TERMINATED
            cleanup = terminate_process_tree(
                worker_pid,
                grace_seconds=profile.termination_grace_seconds,
            )
            break
        time.sleep(
            min(profile.poll_interval_seconds,
                max(0.001, profile.hard_wall_seconds - elapsed))
        )

    elapsed = time.monotonic() - started
    last_phase = _read_worker_phase(status_path)
    if execution_status is None:
        cleanup = _cleanup_observed_descendants(
            worker_pid,
            observed_pids,
            grace_seconds=profile.termination_grace_seconds,
        )
        return_code = process.returncode
        if return_code != 0:
            execution_status = EXECUTION_WORKER_CRASHED
        elif not output_path.exists():
            execution_status = EXECUTION_WORKER_PROTOCOL_ERROR
        else:
            try:
                payload = _read_json(output_path)
            except (OSError, ValueError, TypeError) as error:
                return SupervisedWorkerResult(
                    execution_status=EXECUTION_WORKER_PROTOCOL_ERROR,
                    payload=None,
                    worker_pid=worker_pid,
                    worker_exit_code=return_code,
                    elapsed_seconds=elapsed,
                    resource_guard_facts=resource_guard_facts,
                    resource_snapshot=dict(last_snapshot),
                    last_worker_phase=last_phase,
                    cleanup=cleanup,
                    protocol_error=f"malformed worker JSON: {error}",
                )
            if not isinstance(payload, dict) or not payload.get(
                "output_protocol_complete"
            ):
                return SupervisedWorkerResult(
                    execution_status=EXECUTION_WORKER_PROTOCOL_ERROR,
                    payload=None,
                    worker_pid=worker_pid,
                    worker_exit_code=return_code,
                    elapsed_seconds=elapsed,
                    resource_guard_facts=resource_guard_facts,
                    resource_snapshot=dict(last_snapshot),
                    last_worker_phase=last_phase,
                    cleanup=cleanup,
                    protocol_error="worker output is incomplete",
                )
            execution_status = EXECUTION_COMPLETED
            return SupervisedWorkerResult(
                execution_status=execution_status,
                payload=payload,
                worker_pid=worker_pid,
                worker_exit_code=return_code,
                elapsed_seconds=elapsed,
                resource_guard_facts=resource_guard_facts,
                resource_snapshot=dict(last_snapshot),
                last_worker_phase=last_phase,
                cleanup=cleanup,
            )

    return SupervisedWorkerResult(
        execution_status=execution_status,
        payload=None,
        worker_pid=worker_pid,
        worker_exit_code=process.returncode,
        elapsed_seconds=elapsed,
        hard_deadline_elapsed_seconds=elapsed
        if execution_status == EXECUTION_HARD_DEADLINE_TERMINATED
        else None,
        resource_guard_facts=resource_guard_facts,
        resource_snapshot=dict(last_snapshot),
        last_worker_phase=last_phase,
        cleanup=cleanup,
        protocol_error=None,
    )


__all__ = [
    "CalibrationExecutionProfile",
    "EXECUTION_COMPLETED",
    "EXECUTION_HARD_DEADLINE_TERMINATED",
    "EXECUTION_PARENT_CANCELLED",
    "EXECUTION_RESOURCE_GUARD_TERMINATED",
    "EXECUTION_SOFT_BUDGET_EXHAUSTED",
    "EXECUTION_WORKER_CRASHED",
    "EXECUTION_WORKER_PROTOCOL_ERROR",
    "SupervisedWorkerResult",
    "TARGET_SCALE_CALIBRATION_EXECUTION_PROFILE",
    "process_tree_snapshot",
    "supervise_json_worker",
    "terminate_process_tree",
]
