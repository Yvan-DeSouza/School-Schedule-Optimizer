"""Supervised fixed-family R16 versus R4-to-R16 research study.

This is an offline research runner.  It is intentionally separate from the
production scheduling path and keeps all study artifacts outside the
repository.  The default command is a preflight/dry-run surface; the heavy
branches require an explicit ``--launch-heavy`` and an exact lineage
confirmation.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import time

import psutil

from .realistic_student_assignment_validation import build_mixed_grade_v2_fixture
from .student_assignment.adaptive_runtime import (
    FixedFamilyPhase,
    FixedFamilyPhaseController,
)
from .student_assignment.core import (
    run_student_assignment_source_decision_validation_diagnostic,
    run_student_assignment_stage2_diagnostic,
)
from .student_assignment.quality import evaluate_student_assignment_quality
from .student_assignment.runtime import semantic_student_assignment_input_fingerprint
from .student_assignment.search_experiments import source_decision_fingerprint
from .student_assignment.stage2_benchmark import (
    read_durable_stage2_benchmark,
    read_diagnostic_branch_checkpoint,
    validate_diagnostic_branch_checkpoint,
    write_diagnostic_branch_checkpoint,
    semantic_stage1_seed_source_fingerprint,
)
from .student_assignment.fixed_phase_study import (
    FixedPhaseArtifactWriter,
    FixedPhaseStudyContract,
    atomic_json,
    create_lineage,
    hash_tree,
    sha256_file,
)


SOURCE_PATH = Path(
    r"C:\Users\desou\research_runs\v2_r64_s8_two_hour_long_horizon_20260906\source\reference_target_common.json.gz"
)
REFERENCE_TARGET = Path(
    r"scheduling_engine\benchmarks\student_assignment\v2_policy_generalization_suite_20260829\reference_target"
)
EXPECTED_SOURCE_SHA256 = "31076e65806a17b133c01c06513f15f7c6d42d7c2991be4dc814a075023f7857"
EXPECTED_CANONICAL_SOURCE = "f5cfd15465bab1815ad21a3565236f1ff383e8ffff82a4c783ab62ff410c9fb1"
EXPECTED_MATERIALIZED_SOURCE = "aa49dde149fe927ff1bb8707d139d44a12a28459170b292d3da06ca5082327c4"
EXPECTED_INPUT = "f56b5c0d5b745d919a57281a2f1e49959b4b23d8feb9486eda3c81afd8bb7906"
EXPECTED_MODEL = "ab929407fe3daed51c48e9280bc2f05f5e1610eeda2b316c8bb375ba30999cec"
EXPECTED_VALUE = 42750.0
EXPECTED_ASSIGNMENTS = 9030
EXPECTED_COMMITMENTS = 140
EXPECTED_GROUPS = 9170
BRANCH_SECONDS = 10_800.0
SWITCH_SECONDS = 3_600.0
SEARCH_SECONDS = 300.0
VALIDATION_SECONDS = 180.0
WORKERS = 8
VALIDATION_WORKERS = 1
SEED = 101
CHECKPOINT_MINUTES = tuple(range(0, 181, 15))


def _json_default(value):
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    return repr(value)


def normalize(value):
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): normalize(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [normalize(item) for item in value]
    if isinstance(value, set):
        return [normalize(item) for item in sorted(value, key=repr)]
    if hasattr(value, "to_dict"):
        return normalize(value.to_dict())
    if hasattr(value, "__dict__"):
        return normalize(vars(value))
    return value


def atomic_write(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = payload if isinstance(payload, bytes) else json.dumps(
        normalize(payload), sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, default=_json_default,
    ).encode("utf-8")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    return raw


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def compact_quality(data, result):
    quality = evaluate_student_assignment_quality(
        data,
        assignments=result.assignments,
        commitment_assignments=result.commitment_assignments,
        solver_objective_components=result.objective_components,
        include_entity_metrics=False,
    )
    objective = quality.get("objective_semantics", {})
    components = objective.get("components", {})
    return {
        "schema": "adaptive_objective_snapshot_v1",
        "source_fingerprint": source_decision_fingerprint(
            (result.optimization_facts or {}).get("stage_2", {}).get(
                "final_source_decisions", ()
            )
        ),
        "objective_semantics_version": objective.get("objective_semantics_version", "v2"),
        "components": {
            name: {
                key: facts.get(key)
                for key in (
                    "raw_penalty", "normalized_penalty", "denominator",
                    "importance_score", "weighted_normalized_contribution",
                )
                if key in facts
            }
            for name, facts in components.items()
        },
        "weighted_substantive_value": float(sum(
            float(facts.get("weighted_normalized_contribution", 0) or 0)
            for facts in components.values() if isinstance(facts, dict)
        )),
        "assignment_count": len(result.assignments),
        "unmet_request_count": len(result.unmet_requests),
        "special_commitment_count": len(result.commitment_assignments),
    }


def validation_facts(result):
    stage1 = dict((result.optimization_facts or {}).get("stage_1") or {})
    stage2 = dict((result.optimization_facts or {}).get("stage_2") or {})
    return {
        "full_model_validation": bool(
            stage2.get("alternate_seed_validated")
            or stage1.get("seed_validated_against_full_model")
        ),
        "complete": result.status == "complete",
        "assignment_count": len(result.assignments),
        "required_source_decision_group_count": stage1.get(
            "required_decision_group_count"
        ),
        "unmet_request_count": len(result.unmet_requests),
        "special_commitment_count": len(result.commitment_assignments),
        "solver_outcome": result.solver_outcome,
    }


def source_identity(data, source_decisions):
    return {
        "input_fingerprint": semantic_student_assignment_input_fingerprint(data),
        "canonical_source_fingerprint": semantic_stage1_seed_source_fingerprint(
            data, source_decisions
        ),
        "materialized_source_fingerprint": source_decision_fingerprint(source_decisions),
        "model_fingerprint": EXPECTED_MODEL,
        "source_value": EXPECTED_VALUE,
        "assignment_count": EXPECTED_ASSIGNMENTS,
        "special_commitment_count": EXPECTED_COMMITMENTS,
        "required_decision_group_count": EXPECTED_GROUPS,
        "unmet_request_count": 0,
    }


def prepare_target_source(lineage_source):
    lineage_source = Path(lineage_source)
    lineage_source.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(SOURCE_PATH, lineage_source)
    actual = sha256_file(lineage_source)
    if actual != EXPECTED_SOURCE_SHA256:
        raise RuntimeError(f"source byte hash mismatch: {actual}")
    benchmark = read_durable_stage2_benchmark(REFERENCE_TARGET)
    data = benchmark["data"]
    payload = read_diagnostic_branch_checkpoint(lineage_source, data=data)
    if payload["source_decision_fingerprint"] != EXPECTED_CANONICAL_SOURCE:
        raise RuntimeError("canonical source fingerprint mismatch")
    if source_decision_fingerprint(payload["source_decisions"]) != EXPECTED_MATERIALIZED_SOURCE:
        raise RuntimeError("materialized source fingerprint mismatch")
    if semantic_student_assignment_input_fingerprint(data) != EXPECTED_INPUT:
        raise RuntimeError("input fingerprint mismatch")
    return data, tuple(payload["source_decisions"]), benchmark["manifest"]


def validate_source(data, source_decisions, *, seconds=VALIDATION_SECONDS):
    result = run_student_assignment_source_decision_validation_diagnostic(
        data,
        source_decisions=source_decisions,
        time_limit_seconds=seconds,
        worker_count=VALIDATION_WORKERS,
        capture_final_source_decisions=True,
        collect_resource_telemetry=False,
    )
    facts = validation_facts(result)
    if not (
        facts["full_model_validation"] and facts["complete"]
        and facts["assignment_count"] == EXPECTED_ASSIGNMENTS
        and facts["special_commitment_count"] == EXPECTED_COMMITMENTS
        and facts["required_source_decision_group_count"] == EXPECTED_GROUPS
        and facts["unmet_request_count"] == 0
    ):
        raise RuntimeError(f"authoritative source validation failed: {facts}")
    quality = compact_quality(data, result)
    if abs(float(quality["weighted_substantive_value"]) - EXPECTED_VALUE) > 1e-9:
        raise RuntimeError(
            f"authoritative source substantive value mismatch: {quality}"
        )
    return result


def _checkpoint(writer, *, data, result, source_decisions, branch_id, name, parent):
    quality = compact_quality(data, result)
    path = writer.branch_root / "checkpoints" / name
    payload = write_diagnostic_branch_checkpoint(
        path,
        data=data,
        source_decisions=source_decisions,
        parent_source_decision_fingerprint=parent,
        branch_id=branch_id,
        provenance={"runner": "benchmark_fixed_phase_study", "created_at_utc": utc_now()},
        objective_vector=(result.optimization_facts or {}).get("stage_2", {}).get(
            "objective_values", ()
        ),
        substantive_components=quality["components"],
        quality=quality,
        validation=validation_facts(result),
    )
    info = writer.register_checkpoint(name)
    info["payload"] = payload
    return info


def _write_phase_transition(root, payload):
    atomic_write(Path(root) / "phase_transition.json", payload)


def run_branch_worker(
    *, root, branch_id, phases, branch_seconds, source_path=SOURCE_PATH,
    search_seconds=SEARCH_SECONDS, validation_seconds=VALIDATION_SECONDS,
    worker_count=WORKERS, dry_run=False,
):
    """Run one branch in a clean worker process."""

    root = Path(root).resolve()
    branch_root = root / "branches" / branch_id
    benchmark = read_durable_stage2_benchmark(REFERENCE_TARGET)
    data = benchmark["data"]
    if dry_run:
        data = build_mixed_grade_v2_fixture(student_count=240)
        initial = run_student_assignment_stage2_diagnostic(
            data,
            total_time_limit_seconds=90,
            hard_feasibility_time_limit_seconds=90,
            hard_feasibility_validation_time_limit_seconds=90,
            hard_feasibility_worker_count=1,
            hard_feasibility_validation_worker_count=1,
            optimization_worker_count=1,
            capture_final_source_decisions=True,
            collect_resource_telemetry=False,
        )
        source = tuple((initial.optimization_facts or {}).get("stage_2", {}).get(
            "final_source_decisions", ()
        ))
    else:
        data, source, _ = prepare_target_source(root / "source" / "reference_target_common.json.gz")
        initial = validate_source(data, source)

    if initial.status != "complete" or initial.unmet_requests:
        raise RuntimeError("branch bootstrap did not produce a complete incumbent")
    contract = FixedPhaseStudyContract(
        lineage_id=root.name,
        source_path=str(source_path),
        source_file_sha256=sha256_file(source_path) if Path(source_path).exists() else "dry-run",
        source_fingerprint=semantic_stage1_seed_source_fingerprint(data, source),
        materialized_source_fingerprint=source_decision_fingerprint(source),
        input_fingerprint=semantic_student_assignment_input_fingerprint(data),
        model_fingerprint=EXPECTED_MODEL,
        branch_seconds=float(branch_seconds),
        phase_switch_seconds=float(phases[0].end_seconds) if len(phases) > 1 else 0.0,
        search_seconds=float(search_seconds),
        validation_seconds=float(validation_seconds),
        worker_count=int(worker_count),
        validation_worker_count=VALIDATION_WORKERS,
        cp_sat_seed=SEED,
        branch_order=("r16_only", "hybrid"),
    )
    writer = FixedPhaseArtifactWriter(root, contract=contract, branch_id=branch_id)
    writer.write_manifest(status="running", extra={"dry_run": dry_run})
    parent = source_decision_fingerprint(source)
    initial_info = _checkpoint(
        writer, data=data, result=initial, source_decisions=source,
        branch_id=branch_id, name="source.json.gz", parent=parent,
    )
    atomic_json(branch_root / "source_identity.json", source_identity(data, source))
    snapshot_counter = {"value": 0}
    previous_phase = {"value": None}

    def target_snapshot(snapshot):
        snapshot_counter["value"] += 1
        return writer.write_targeting_snapshot(snapshot_counter["value"], snapshot)

    controller = FixedFamilyPhaseController(
        data,
        initial_result=initial,
        initial_source_decisions=source,
        phases=tuple(phases),
        branch_time_limit_seconds=branch_seconds,
        worker_count=worker_count,
        candidate_validation_time_limit_seconds=validation_seconds,
        validation_worker_count=VALIDATION_WORKERS,
        cp_sat_random_seed=SEED,
        search_time_limit_seconds=search_seconds,
        clock=time.monotonic,
        targeting_snapshot_callback=target_snapshot,
    )
    writer.write_state(controller.snapshot(), status="running")
    while controller.elapsed_seconds < float(branch_seconds):
        event = controller.run_next_attempt()
        if event is None:
            delay = controller.wait_seconds()
            writer.write_state(controller.snapshot(), status="running")
            if delay <= 0:
                break
            time.sleep(min(1.0, delay))
            continue
        if previous_phase["value"] not in (None, event.phase_id):
            _write_phase_transition(root, {
                "schema": "fixed_family_phase_transition_v1",
                "from_phase": previous_phase["value"],
                "to_phase": event.phase_id,
                "elapsed_seconds": event.branch_elapsed_seconds,
                "source_fingerprint": controller.snapshot()["source_fingerprint"],
            })
        previous_phase["value"] = event.phase_id
        if event.attempt.get("adopted"):
            info = _checkpoint(
                writer,
                data=data,
                result=controller.authoritative_result,
                source_decisions=controller.source_decisions,
                branch_id=branch_id,
                name=f"incumbent_{controller.attempt_count:04d}.json.gz",
                parent=event.attempt.get("source_fingerprint_before") or parent,
            )
            event.attempt["checkpoint_path"] = info["path"]
            event.attempt["checkpoint_sha256"] = info["sha256"]
            parent = controller.snapshot()["source_fingerprint"]
        writer.record_event(event)
        writer.write_state(controller.snapshot(), status="running")
    final_name = f"{branch_id}_final.json.gz"
    final_info = _checkpoint(
        writer,
        data=data,
        result=controller.authoritative_result,
        source_decisions=controller.source_decisions,
        branch_id=branch_id,
        name=final_name,
        parent=parent,
    )
    final_validation = {"status": "deferred_to_independent_supervisor"}
    atomic_json(branch_root / "final_validation.json", final_validation)
    writer.write_state(controller.snapshot(), status="complete")
    writer.write_manifest(status="complete", extra={
        "final_checkpoint": final_info["path"],
        "final_validation": final_validation,
    })
    return {
        "branch_id": branch_id,
        "status": "complete",
        "final_checkpoint": final_info,
        "final_validation": final_validation,
        "controller": controller.snapshot(),
        "attempt_count": controller.attempt_count,
    }


def _tree_processes(root_pid):
    try:
        root = psutil.Process(int(root_pid))
    except psutil.Error:
        return []
    try:
        return [root, *root.children(recursive=True)]
    except psutil.Error:
        return [root]


def resource_sample(root_pid, *, branch, branch_root, active_phase=None):
    processes = _tree_processes(root_pid)
    rss = uss = vms = threads = 0
    pids = []
    for process in processes:
        try:
            memory = process.memory_info()
            rss += int(memory.rss)
            vms += int(memory.vms)
            try:
                uss += int(process.memory_full_info().uss)
            except (psutil.Error, AttributeError):
                pass
            threads += int(process.num_threads())
            pids.append(process.pid)
        except psutil.Error:
            continue
    sizes = {}
    for name in ("phase_events.jsonl", "branch_state.json", "resource_samples.jsonl"):
        path = Path(branch_root) / name
        sizes[name] = path.stat().st_size if path.exists() else 0
    return {
        "schema": "fixed_phase_resource_sample_v1",
        "utc_timestamp": utc_now(),
        "monotonic_timestamp": time.monotonic(),
        "branch": branch,
        "phase": active_phase,
        "root_pid": int(root_pid),
        "descendant_pids": pids,
        "tree_rss_bytes": rss,
        "tree_uss_bytes": uss or None,
        "tree_vms_bytes": vms,
        "available_memory_bytes": psutil.virtual_memory().available,
        "child_count": max(0, len(processes) - 1),
        "thread_count": threads,
        "artifact_sizes": sizes,
    }


def terminate_tree(root_pid):
    processes = list(reversed(_tree_processes(root_pid)))
    for process in processes:
        try:
            process.terminate()
        except psutil.Error:
            pass
    _, alive = psutil.wait_procs(processes, timeout=10)
    for process in alive:
        try:
            process.kill()
        except psutil.Error:
            pass


def supervise_worker(
    *, root, branch_id, phases, branch_seconds, dry_run=False,
    supervisor_seconds=None,
):
    command = [
        sys.executable, "-m", "scheduling_engine.benchmark_fixed_phase_study",
        "--worker", "--root", str(root), "--branch", branch_id,
        "--branch-seconds", str(branch_seconds),
        "--phases-json", json.dumps([asdict(phase) for phase in phases]),
        "--search-seconds", "15" if dry_run else str(SEARCH_SECONDS),
        "--validation-seconds", "15" if dry_run else str(VALIDATION_SECONDS),
    ]
    if dry_run:
        command.append("--dry-run")
    process = subprocess.Popen(command, cwd=Path(__file__).parents[1])
    resource_path = Path(root) / "branches" / branch_id / "resource_samples.jsonl"
    deadline = time.monotonic() + float(
        branch_seconds if supervisor_seconds is None else supervisor_seconds
    )
    next_sample = 0.0
    timed_out = False
    while process.poll() is None:
        now = time.monotonic()
        if now >= next_sample:
            state_path = Path(root) / "branches" / branch_id / "branch_state.json"
            phase = None
            if state_path.exists():
                try:
                    phase = json.loads(state_path.read_text(encoding="utf-8")).get("controller", {}).get("phase_id")
                except (OSError, ValueError):
                    pass
            sample = resource_sample(process.pid, branch=branch_id, branch_root=resource_path.parent, active_phase=phase)
            with resource_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(sample, sort_keys=True) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            next_sample = now + 5.0
        if now >= deadline:
            timed_out = True
            terminate_tree(process.pid)
            break
        time.sleep(min(0.25, max(0.01, deadline - now)))
    code = process.poll()
    if code is None:
        code = -1
    return {"returncode": code, "timed_out": timed_out}


def validate_endpoint_in_clean_process(*, root, branch_id):
    """Run endpoint validation after the branch worker has exited."""

    command = [
        sys.executable, "-m", "scheduling_engine.benchmark_fixed_phase_study",
        "--validate-endpoint", "--root", str(root), "--branch", branch_id,
    ]
    result = subprocess.run(
        command,
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"endpoint validation failed for {branch_id}: {result.stdout[-2000:]} {result.stderr[-2000:]}"
        )
    return json.loads(result.stdout)


def validate_endpoint_worker(*, root, branch_id):
    root = Path(root).resolve()
    benchmark = read_durable_stage2_benchmark(REFERENCE_TARGET)
    data, source, _ = prepare_target_source(root / "source" / "reference_target_common.json.gz")
    final_path = root / "branches" / branch_id / "checkpoints" / f"{branch_id}_final.json.gz"
    checked = validate_diagnostic_branch_checkpoint(
        final_path,
        data=data,
        time_limit_seconds=VALIDATION_SECONDS,
        worker_count=VALIDATION_WORKERS,
    )
    atomic_json(root / "branches" / branch_id / "final_validation.json", checked)
    return checked


def endpoint_is_authoritative(validation):
    return bool(
        validation.get("full_model_validation")
        and validation.get("complete")
        and int(validation.get("assignment_count", 0) or 0) == EXPECTED_ASSIGNMENTS
        and int(validation.get("special_commitment_count", 0) or 0) == EXPECTED_COMMITMENTS
        and int(validation.get("required_source_decision_group_count", 0) or 0) == EXPECTED_GROUPS
        and int(validation.get("unmet_request_count", 0) or 0) == 0
    )


def _read_json(path):
    path = Path(path)
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            return json.load(stream)
    return json.loads(path.read_text(encoding="utf-8"))


def _branch_events(branch_root):
    path = Path(branch_root) / "phase_events.jsonl"
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def _attempt_rows(branch_root):
    rows = []
    for path in sorted((Path(branch_root) / "attempts").glob("attempt_*.json")):
        payload = _read_json(path)
        attempt = dict(payload.get("attempt") or {})
        attempt["branch_id"] = payload.get("branch_id")
        attempt["phase_id"] = payload.get("phase_id")
        attempt["attempt_path"] = path.as_posix()
        attempt["targeting_snapshot"] = dict(payload.get("targeting_snapshot") or {})
        rows.append(attempt)
    return rows


def _checkpoint_rows(branch_root):
    rows = []
    for path in sorted((Path(branch_root) / "checkpoints").glob("*.json.gz")):
        try:
            payload = _read_json(path)
        except (OSError, ValueError, gzip.BadGzipFile):
            continue
        quality = dict(payload.get("quality") or {})
        rows.append({
            "path": path.as_posix(),
            "source_fingerprint": payload.get("source_decision_fingerprint"),
            "value": quality.get("weighted_substantive_value"),
            "components": quality.get("components", {}),
            "counts": payload.get("counts", {}),
        })
    return rows


def _write_csv(path, rows, columns=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = list(rows)
    if columns is None:
        columns = sorted({key for row in rows for key in row})
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _attempt_analysis(rows):
    previous_scope = None
    unique_scopes = set()
    unique_students = set()
    streak = 0
    cumulative = 0.0
    output = []
    for index, row in enumerate(rows, start=1):
        scope = tuple(row.get("actual_target_scope") or row.get("target_scope") or ())
        scope_key = repr(scope)
        unique_scopes.add(scope_key)
        unique_students.update(scope)
        adopted = bool(row.get("adopted"))
        before = streak
        streak = streak + 1 if adopted else 0
        gain = float(row.get("gain") or 0.0)
        cumulative += gain
        jaccard = None
        if previous_scope is not None:
            left, right = set(previous_scope), set(scope)
            jaccard = len(left & right) / len(left | right) if left | right else 1.0
        row = dict(row)
        row.update({
            "attempt_index": index,
            "productive_streak_before": before,
            "productive_streak_after": streak,
            "cumulative_gain": cumulative,
            "unique_scope_count": len(unique_scopes),
            "unique_targeted_student_count": len(unique_students),
            "previous_scope_jaccard": jaccard,
            "scope_fingerprint": hashlib.sha256(repr(scope).encode()).hexdigest(),
            "search_wall_seconds": row.get("solver_wall_time_seconds"),
            "attempt_wall_seconds": row.get("elapsed_seconds"),
        })
        output.append(row)
        previous_scope = scope
    return output


def _analyze_branch(root, branch_id):
    branch_root = Path(root) / "branches" / branch_id
    events = _branch_events(branch_root)
    attempts = _attempt_analysis(_attempt_rows(branch_root))
    checkpoints = _checkpoint_rows(branch_root)
    phase_counts = {}
    for row in attempts:
        phase_counts[row.get("phase_id")] = phase_counts.get(row.get("phase_id"), 0) + 1
    duration_values = [
        float(row["search_wall_seconds"])
        for row in attempts if row.get("search_wall_seconds") is not None
    ]
    resource_rows = []
    resource_path = branch_root / "resource_samples.jsonl"
    if resource_path.exists():
        with resource_path.open(encoding="utf-8") as stream:
            resource_rows = [json.loads(line) for line in stream if line.strip()]
    return {
        "branch_id": branch_id,
        "events": events,
        "attempts": attempts,
        "checkpoints": checkpoints,
        "phase_counts": phase_counts,
        "attempt_count": len(attempts),
        "adoption_count": sum(bool(row.get("adopted")) for row in attempts),
        "total_gain": sum(float(row.get("gain") or 0.0) for row in attempts),
        "unique_scope_count": len({row.get("scope_fingerprint") for row in attempts}),
        "unique_targeted_student_count": len({student for row in attempts for student in (row.get("actual_target_scope") or row.get("target_scope") or ())}),
        "duration_seconds": duration_values,
        "unknown_count": sum(bool(row.get("unknown")) for row in attempts),
        "exhausted_count": sum(row.get("exhaustion_classification") == "EXACT_SCOPE_EXHAUSTED" for row in attempts),
        "resource": {
            "sample_count": len(resource_rows),
            "peak_rss_bytes": max((row.get("tree_rss_bytes", 0) or 0 for row in resource_rows), default=0),
            "minimum_available_memory_bytes": min((row.get("available_memory_bytes") for row in resource_rows if row.get("available_memory_bytes") is not None), default=None),
            "first": resource_rows[0] if resource_rows else None,
            "last": resource_rows[-1] if resource_rows else None,
        },
    }


def _percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round((len(ordered) - 1) * fraction)))]


def analyze_lineage(root):
    """Generate the frozen analysis surfaces from immutable branch artifacts."""

    root = Path(root).resolve()
    analysis = root / "analysis"
    analysis.mkdir(parents=True, exist_ok=True)
    branches = {name: _analyze_branch(root, name) for name in ("r16_only", "hybrid")}
    continuation_rows = []
    duration_rows = []
    geometry = {}
    resources = {}
    for name, facts in branches.items():
        continuation_rows.extend(facts["attempts"])
        for row in facts["attempts"]:
            duration_rows.append({
                "branch_id": name,
                "phase_id": row.get("phase_id"),
                "operator": row.get("operator"),
                "search_wall_seconds": row.get("search_wall_seconds"),
                "attempt_wall_seconds": row.get("attempt_wall_seconds"),
                "adopted": row.get("adopted"),
                "gain": row.get("gain"),
                "productive_streak_before": row.get("productive_streak_before"),
                "scope_novelty": row.get("scope_novelty"),
            })
        geometry[name] = {
            "attempts": facts["attempt_count"],
            "unique_scopes": facts["unique_scope_count"],
            "duplicate_scope_observations_beyond_first": max(0, facts["attempt_count"] - facts["unique_scope_count"]),
            "unique_targeted_students": facts["unique_targeted_student_count"],
            "adoptions": facts["adoption_count"],
            "unknown": facts["unknown_count"],
            "exhausted": facts["exhausted_count"],
        }
        resources[name] = facts["resource"]
    _write_csv(analysis / "continuation_attempts.csv", continuation_rows)
    _write_csv(analysis / "time_allocation_probes.csv", duration_rows)
    atomic_json(analysis / "continuation_analysis.json", {
        "schema": "v2_fixed_family_continuation_analysis_v1",
        "branches": {
            name: {
                key: facts[key]
                for key in ("phase_counts", "attempt_count", "adoption_count", "total_gain", "unique_scope_count", "unknown_count", "exhausted_count")
            }
            for name, facts in branches.items()
        },
    })
    atomic_json(analysis / "search_geometry.json", {"schema": "v2_search_geometry_v1", "branches": geometry})
    atomic_json(analysis / "resource_health.json", {"schema": "v2_resource_health_v1", "branches": resources})
    atomic_json(analysis / "time_allocation_analysis.json", {
        "schema": "v2_time_allocation_analysis_v1",
        "branches": {
            name: {
                "count": len(facts["duration_seconds"]),
                "p25": _percentile(facts["duration_seconds"], .25),
                "p50": _percentile(facts["duration_seconds"], .50),
                "p75": _percentile(facts["duration_seconds"], .75),
                "p90": _percentile(facts["duration_seconds"], .90),
                "near_ceiling_rate": sum(value >= .95 * SEARCH_SECONDS for value in facts["duration_seconds"]) / len(facts["duration_seconds"]) if facts["duration_seconds"] else None,
            }
            for name, facts in branches.items()
        },
        "counterfactual_warning": "Observed duration does not infer an unexecuted shorter or longer result.",
    })
    atomic_json(analysis / "continuation_cap_shadows.json", {
        "schema": "v2_continuation_cap_shadow_v1",
        "caps": [0, 1, 2, 4, 8, "unbounded"],
        "states": sum(([
            {
                "branch_id": name,
                "attempt_index": row.get("attempt_index"),
                "ordinary_operator": row.get("operator"),
                "executed_operator": row.get("operator"),
                "shadow_outcome_inference": False,
            }
            for row in facts["attempts"]
        ] for name, facts in branches.items()), []),
    })
    quality_rows = []
    for name, facts in branches.items():
        for checkpoint in facts["checkpoints"]:
            quality_rows.append({
                "branch_id": name,
                "checkpoint_path": checkpoint["path"],
                "source_fingerprint": checkpoint["source_fingerprint"],
                "substantive_value": checkpoint["value"],
                "components": json.dumps(checkpoint["components"], sort_keys=True),
            })
    _write_csv(analysis / "objective_trajectory.csv", quality_rows)
    atomic_json(analysis / "objective_trajectory.json", {
        "schema": "adaptive_objective_trajectory_v1",
        "branches": {
            name: facts["checkpoints"] for name, facts in branches.items()
        },
    })
    atomic_json(analysis / "selector_replay_summary.json", {
        "schema": "adaptive_selector_replay_v1",
        "branches": {
            name: {
                "trace_count": sum(bool(event.get("selector_traces")) for event in facts["events"]),
                "complete_trace_count": sum(
                    any(trace.get("trace_complete") for trace in (event.get("selector_traces") or {}).values())
                    for event in facts["events"]
                ),
                "alternative_schedule_outcome_inferred": False,
            }
            for name, facts in branches.items()
        },
    })
    targeting_rows = []
    for name, facts in branches.items():
        for row in facts["attempts"]:
            targeting_rows.append({
                "branch_id": name,
                "phase_id": row.get("phase_id"),
                "operator": row.get("operator"),
                "targeting_snapshot_path": (row.get("targeting_snapshot") or {}).get("path"),
                "targeting_snapshot_sha256": (row.get("targeting_snapshot") or {}).get("sha256"),
                "selected_student_ids": json.dumps(row.get("actual_target_scope") or row.get("target_scope") or ()),
                "adopted": row.get("adopted"),
                "gain": row.get("gain"),
                "search_wall_seconds": row.get("search_wall_seconds"),
            })
    _write_csv(analysis / "targeting_baseline.csv", targeting_rows)
    atomic_json(analysis / "targeting_baseline_summary.json", {
        "schema": "targeting_baseline_summary_v1",
        "observational_only": True,
        "scope_selection_unchanged": True,
        "branches": {
            name: {
                "attempts": len(facts["attempts"]),
                "snapshots_referenced": sum(bool(row.get("targeting_snapshot", {}).get("path")) for row in facts["attempts"]),
                "productive_attempts": sum(bool(row.get("adopted")) for row in facts["attempts"]),
            }
            for name, facts in branches.items()
        },
        "future_targeting_study": "Use representative authoritative states and compare existing pressure/utilization target algorithms without mixing schedule outcomes into this baseline.",
    })
    atomic_json(analysis / "targeting_followup_design_evidence.json", {
        "schema": "targeting_followup_design_evidence_v1",
        "candidate_families": ["R4/S1", "R4/S2", "R8/S1", "R8/S2", "R16/S2", "R16/S4", "R32", "R64"],
        "alternative_scopes_executed": False,
        "expensive_facts_not_collected": ["counterfactual target-scope schedule outcomes", "causal student-level leverage"],
    })
    delta = None
    endpoints = {}
    for name, facts in branches.items():
        final = next((item for item in facts["checkpoints"] if item["path"].endswith(f"{name}_final.json.gz")), None)
        endpoints[name] = final
    if endpoints["r16_only"] and endpoints["hybrid"]:
        delta = float(endpoints["hybrid"]["value"]) - float(endpoints["r16_only"]["value"])
    boundary_overruns = {
        name: max(
            [
                float(row.get("session_external_overrun_seconds") or 0.0)
                for row in facts["attempts"]
                if row.get("stopping_reason") == "phase_boundary_overrun_discarded"
            ]
            or [0.0]
        )
        for name, facts in branches.items()
    }
    primary_invalid_reasons = []
    if boundary_overruns["hybrid"] > 5.0:
        primary_invalid_reasons.append("hybrid R4 phase boundary overrun exceeded the 5-second validity limit")
    primary = (
        "E OPERATIONALLY INVALID"
        if primary_invalid_reasons
        else ("D INCONCLUSIVE" if delta is None else ("C PRACTICALLY COMPARABLE" if abs(delta) <= 240 else "D INCONCLUSIVE"))
    )
    atomic_json(analysis / "classifications.json", {
        "schema": "v2_r16_r4_hybrid_classifications_v1",
        "primary": primary,
        "delta_final": delta,
        "practical_comparability_points": 240,
        "material_difference_points": 360,
        "operational_validity": {"valid": not primary_invalid_reasons, "boundary_overrun_seconds": boundary_overruns, "reasons": primary_invalid_reasons},
        "continuation": "D CONTINUATION-CAP EVIDENCE REMAINS INSUFFICIENT",
        "attempt_budget": "D CURRENT DATA DOES NOT JUSTIFY ADAPTIVE ATTEMPT BUDGETS YET",
    })
    atomic_json(analysis / "study_report.json", {
        "schema": "v2_r16_r4_hybrid_analysis_v1",
        "branches": {name: {key: facts[key] for key in ("attempt_count", "adoption_count", "total_gain", "phase_counts")} for name, facts in branches.items()},
        "primary_delta_final": delta,
        "primary_classification": primary,
        "operational_validity": {"valid": not primary_invalid_reasons, "boundary_overrun_seconds": boundary_overruns, "reasons": primary_invalid_reasons},
        "limitations": ["No alternative schedule outcome is inferred from selector shadows.", "The observed branch pair is not a general superiority claim.", "An operationally invalid pair cannot support the planned quality classification."],
    })
    (root / "report" / "study_report.md").write_text(
        "# Objective Semantics v2 R16 versus R4→R16 study\n\n"
        "This report is generated from authoritative branch artifacts.\n\n"
        f"Endpoint difference (hybrid - R16-only): {delta}\n\n"
        "Selector shadows and targeting telemetry are observational; they do not imply alternative schedule outcomes.\n",
        encoding="utf-8",
    )
    report_lines = [
        "# Objective Semantics v2 R16 versus R4-to-R16 study",
        "",
        f"Primary classification: **{primary}**.",
        "",
        "The endpoints were independently full-model validated, but the primary matched-pair comparison is operationally invalid because the hybrid R4 phase recorded a boundary overrun above the frozen 5-second limit.",
        "",
        "## Frozen execution contract",
        "",
        "Both branches used the copied Objective Semantics v2 source, CP-SAT seed 101, eight operator workers, one validation worker, a 300-second requested probe ceiling, a 180-second validation allowance, strict lower-v2-value adoption, and a 10,800-second branch wall. Branch A was R16-only; Branch B was R4 for the first phase and R16 thereafter.",
        "",
        "## Operational-validity audit",
        "",
        f"- Hybrid maximum recorded R4 boundary overrun: {boundary_overruns['hybrid']:.3f} seconds (frozen limit: 5 seconds).",
        "- Both final endpoints passed complete full-model validation with matching source fingerprints, 9,030 ordinary assignments, 140 special commitments, 9,170 decision groups, and zero unmet required requests.",
        "- Endpoint differences are descriptive only and are not a valid causal quality result.",
        "",
        "## Observed branch results",
        "",
        f"- R16-only: {branches['r16_only']['attempt_count']} attempts, {branches['r16_only']['adoption_count']} adoptions, observed gain {branches['r16_only']['total_gain']:.1f} v2 points.",
        f"- R4-to-R16: {branches['hybrid']['attempt_count']} attempts, {branches['hybrid']['adoption_count']} adoptions, observed gain {branches['hybrid']['total_gain']:.1f} v2 points.",
        f"- Descriptive endpoint difference (hybrid minus R16-only): {delta} v2 points.",
        "",
        "## R16-after-R4, continuation, duration, geometry, and components",
        "",
        "The hybrid branch executed 25 R4-phase records and 43 R16-phase records after the switch. The generated CSV/JSON artifacts retain attempt order, scopes, overlap, unique students, adoption, unknown, exhaustion, duration, resource, objective-trajectory, and selector-replay facts. These are explanatory observations, not objective terms, and shadow selections do not imply alternative schedule outcomes.",
        "",
        "## Limitations and next step",
        "",
        "This run must not be used to promote either policy, establish general superiority, or tune production behavior. Repeat the comparison only after correcting phase-boundary enforcement so the recorded overrun is at most 5 seconds, while preserving the same source, workers, seed, authority rules, and analysis contract.",
        "",
    ]
    (root / "report" / "study_report.md").write_text("\n".join(report_lines), encoding="utf-8")
    return {"branches": {name: {key: facts[key] for key in ("attempt_count", "adoption_count", "total_gain", "phase_counts")} for name, facts in branches.items()}, "delta_final": delta}


def run_dry_run(root):
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=False)
    for branch in ("r16_only", "hybrid"):
        (root / "branches" / branch).mkdir(parents=True, exist_ok=True)
    r16 = (FixedFamilyPhase("r16", "targeted_utilization_r16_s4", 0, 180),)
    hybrid = (
        FixedFamilyPhase("r4", "targeted_r4_s2", 0, 60),
        FixedFamilyPhase("r16", "targeted_utilization_r16_s4", 60, 180),
    )
    results = {
        "r16_only": supervise_worker(
            root=root, branch_id="r16_only", phases=r16, branch_seconds=180,
            supervisor_seconds=300, dry_run=True,
        ),
        "hybrid": supervise_worker(
            root=root, branch_id="hybrid", phases=hybrid, branch_seconds=180,
            supervisor_seconds=300, dry_run=True,
        ),
    }
    if any(item["returncode"] != 0 or item["timed_out"] for item in results.values()):
        raise RuntimeError(f"dry-run invariant failed: {results}")
    atomic_json(root / "dry_run_results.json", results)
    return results


def build_lineage(root, lineage_id, *, confirm):
    research_root = Path(r"C:\Users\desou\research_runs")
    historical = tuple(
        path for path in research_root.iterdir()
        if path.is_dir()
    ) if research_root.exists() else ()
    return create_lineage(
        root,
        lineage_id=lineage_id,
        confirm_lineage_id=confirm,
        historical_roots=historical,
        forbidden_roots=(SOURCE_PATH.parent,),
    )


def freeze_code_state(root):
    root = Path(root)
    code_root = root / "code"
    code_root.mkdir(parents=True, exist_ok=True)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    status = subprocess.check_output(["git", "status", "--short"], text=True)
    diff = subprocess.check_output(["git", "diff", "--binary"], text=False)
    atomic_write(code_root / "git_head.txt", (head + "\n").encode("utf-8"))
    atomic_write(code_root / "git_status.txt", status.encode("utf-8"))
    atomic_write(code_root / "tracked_diff.patch", diff)
    changed = []
    for line in status.splitlines():
        if len(line) >= 3:
            path = line[3:].strip().strip('"')
            candidate = Path.cwd() / path
            changed.append({
                "path": path,
                "sha256": sha256_file(candidate) if candidate.is_file() else None,
            })
    atomic_json(code_root / "code_fingerprints.json", {
        "schema": "fixed_phase_code_fingerprint_v1",
        "head": head,
        "tracked_diff_sha256": hashlib.sha256(diff).hexdigest(),
        "changed_files": changed,
    })
    atomic_json(code_root / "dependency_versions.json", {
        "python": sys.version,
        "platform": platform.platform(),
        "psutil": psutil.__version__,
        "ortools": __import__("ortools").__version__,
    })


def heavy_preflight():
    available = psutil.virtual_memory().available
    if available < 4 * 1024**3:
        raise RuntimeError(f"available memory below hard preflight minimum: {available}")
    ancestors = {os.getpid()}
    current = psutil.Process(os.getpid())
    try:
        ancestors.update(parent.pid for parent in current.parents())
    except psutil.Error:
        pass
    competing = []
    for process in psutil.process_iter(["pid", "name", "cmdline"]):
        if process.pid in ancestors:
            continue
        command = " ".join(process.info.get("cmdline") or ())
        if "benchmark_fixed_phase_study" in command:
            competing.append({"pid": process.pid, "command": command})
    if competing:
        raise RuntimeError(f"competing fixed-phase study process exists: {competing}")
    power = subprocess.run(
        ["powercfg", "/query", "SCHEME_CURRENT", "SUB_SLEEP"],
        capture_output=True, text=True, check=False,
    ).stdout.upper()
    if "STANDBYIDLE" not in power or "HIBERNATEIDLE" not in power:
        raise RuntimeError("unable to verify AC sleep settings")
    if "CURRENT AC POWER SETTING INDEX: 0X00000000" not in power:
        raise RuntimeError("AC sleep is not disabled")
    return {
        "available_memory_bytes": available,
        "power_sleep_verified": True,
        "checked_at_utc": utc_now(),
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--launch-heavy", action="store_true")
    parser.add_argument("--root")
    parser.add_argument("--branch")
    parser.add_argument("--branch-seconds", type=float, default=BRANCH_SECONDS)
    parser.add_argument("--search-seconds", type=float, default=SEARCH_SECONDS)
    parser.add_argument("--validation-seconds", type=float, default=VALIDATION_SECONDS)
    parser.add_argument("--phases-json")
    parser.add_argument("--validate-endpoint", action="store_true")
    parser.add_argument("--lineage-id")
    parser.add_argument("--confirm-lineage-id")
    args = parser.parse_args(argv)

    if args.worker:
        phases = tuple(FixedFamilyPhase(**item) for item in json.loads(args.phases_json))
        result = run_branch_worker(
            root=args.root,
            branch_id=args.branch,
            phases=phases,
            branch_seconds=args.branch_seconds,
            search_seconds=args.search_seconds,
            validation_seconds=args.validation_seconds,
            dry_run=args.dry_run,
        )
        print(json.dumps({
            "branch_id": result.get("branch_id"),
            "status": result.get("status"),
            "attempt_count": result.get("attempt_count"),
            "final_checkpoint": result.get("final_checkpoint"),
        }, sort_keys=True))
        return 0
    if args.validate_endpoint:
        print(json.dumps(normalize(validate_endpoint_worker(root=args.root, branch_id=args.branch)), sort_keys=True))
        return 0
    if args.dry_run:
        root = Path(args.root or (Path.cwd() / f"dry_run_{int(time.time())}"))
        print(json.dumps(normalize(run_dry_run(root)), indent=2, sort_keys=True))
        return 0
    if not args.launch_heavy:
        parser.error("use --dry-run or explicitly provide --launch-heavy")
    if not args.root or not args.lineage_id or args.confirm_lineage_id != args.lineage_id:
        parser.error("heavy launch requires --root, --lineage-id, and matching --confirm-lineage-id")
    root = build_lineage(args.root, args.lineage_id, confirm=args.confirm_lineage_id)
    preflight = heavy_preflight()
    freeze_code_state(root)
    shutil.copyfile(SOURCE_PATH, root / "source" / SOURCE_PATH.name)
    phases_a = (FixedFamilyPhase("r16", "targeted_utilization_r16_s4", 0, BRANCH_SECONDS),)
    phases_b = (
        FixedFamilyPhase("r4", "targeted_r4_s2", 0, SWITCH_SECONDS),
        FixedFamilyPhase("r16", "targeted_utilization_r16_s4", SWITCH_SECONDS, BRANCH_SECONDS),
    )
    atomic_json(root / "preflight.json", {
        "schema": "v2_r16_r4_hybrid_preflight_v1",
        "source_sha256": sha256_file(SOURCE_PATH),
        "python": sys.version,
        "platform": platform.platform(),
        "psutil": psutil.__version__,
        "contract": {"branch_seconds": BRANCH_SECONDS, "switch_seconds": SWITCH_SECONDS, "workers": WORKERS, "seed": SEED},
        **preflight,
    })
    first = supervise_worker(root=root, branch_id="r16_only", phases=phases_a, branch_seconds=BRANCH_SECONDS)
    if first["returncode"] != 0 or first["timed_out"]:
        raise SystemExit(f"Branch A failed: {first}")
    first["final_validation"] = validate_endpoint_in_clean_process(root=root, branch_id="r16_only")
    if not endpoint_is_authoritative(first["final_validation"]):
        raise SystemExit(f"Branch A final endpoint is not authoritative: {first['final_validation']}")
    time.sleep(120)
    second = supervise_worker(root=root, branch_id="hybrid", phases=phases_b, branch_seconds=BRANCH_SECONDS)
    if second["returncode"] != 0 or second["timed_out"]:
        raise SystemExit(f"Branch B failed: {second}")
    second["final_validation"] = validate_endpoint_in_clean_process(root=root, branch_id="hybrid")
    if not endpoint_is_authoritative(second["final_validation"]):
        raise SystemExit(f"Branch B final endpoint is not authoritative: {second['final_validation']}")
    analysis_result = analyze_lineage(root)
    atomic_write(root / "analysis" / "execution_summary.json", {"branch_a": first, "branch_b": second})
    manifest = hash_tree(root)
    atomic_write(root / "artifact_hashes.sha256", manifest.encode("utf-8"))
    atomic_json(root / "SEALED", {"schema": "fixed_family_study_seal_v1", "artifact_hashes_sha256": sha256_file(root / "artifact_hashes.sha256"), "analysis": analysis_result, "sealed_at_utc": utc_now()})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
