"""Eight-worker R16/S4 jackpot repeatability calibration.

This is a research-only harness.  It runs exactly three historical TOP
R16/S4 states, three sequential fresh-process repeats per state, seed 101,
and eight CP-SAT workers.  It is not a target-policy comparison and never
touches production wiring or historical lineage directories.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import threading
import time
from statistics import mean, median

try:
    import psutil
except ImportError:  # pragma: no cover - the study preflight requires it
    psutil = None

from .benchmark_r16_target_selection_screen import (
    EXPECTED_INPUT,
    EXPECTED_MODEL,
    EXPECTED_SOURCE_SHA256,
    EXPECTED_VALUE,
    EXPECTED_ASSIGNMENTS,
    EXPECTED_COMMITMENTS,
    EXPECTED_GROUPS,
    PRIOR_STUDY_ROOT,
    REFERENCE_TARGET,
    SOURCE_PATH,
    atomic_write,
    cell_identity,
    load_states,
    read_diagnostic_branch_checkpoint,
    sha256_file,
    source_decision_fingerprint,
    read_json,
    run_cell,
)


SCREEN_ROOT = Path(
    r"C:\Users\desou\research_runs\v2_r16_target_selection_screen_main_20260907T110000Z_778899aa"
)
FORENSIC_AUDIT_ROOT = Path(
    r"C:\Users\desou\research_runs\v2_r16_target_selection_forensic_followup_20260908d"
)
FORENSIC_INPUT_ROOT = Path(
    r"C:\Users\desou\research_runs\v2_r16_r4_targeting_hint_forensics_20260907_20260907_155839_a0bc36dd"
)
WORKERS = 8
VALIDATION_WORKERS = 1
SEED = 101
SEARCH_SECONDS = 300.0
VALIDATION_SECONDS = 180.0
PARENT_SECONDS = SEARCH_SECONDS + VALIDATION_SECONDS
JACKPOT_ATTEMPTS = (27, 36, 63)


def write_text(path, text):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(text, encoding="utf-8")


def write_code_identity(root):
    code = Path(root) / "code"
    code.mkdir(parents=True, exist_ok=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, check=False)
    status = subprocess.run(["git", "status", "--short"], capture_output=True, check=False)
    diff = subprocess.run(["git", "diff", "--binary", "HEAD"], capture_output=True, check=False)
    (code / "git_head.txt").write_bytes(head.stdout)
    (code / "git_status.txt").write_bytes(status.stdout)
    (code / "tracked_diff.patch").write_bytes(diff.stdout)
    fingerprints = {}
    for relative in (
        "scheduling_engine/benchmark_r16_jackpot_calibration.py",
        "scheduling_engine/benchmark_r16_target_selection_screen.py",
        "scheduling_engine/r16_target_selection_followup.py",
        "docs/STUDENT_ASSIGNMENT_TARGET_SELECTION.md",
        "docs/STUDENT_ASSIGNMENT_HINT_STRATEGY.md",
    ):
        path = Path.cwd() / relative
        if path.exists():
            fingerprints[relative] = sha256_file(path)
    atomic_write(code / "code_fingerprints.json", fingerprints)
    atomic_write(code / "dependency_versions.json", {
        "python": sys.version,
        "platform": platform.platform(),
        "psutil": getattr(psutil, "__version__", None),
    })


def append_resource_sample(path, *, root_pid, context):
    sample = {
        "schema": "r16_jackpot_calibration_resource_sample_v1",
        "utc_timestamp": time.time(),
        "monotonic_timestamp": time.monotonic(),
        **context,
    }
    if psutil is None:
        sample["psutil_available"] = False
    else:
        pids = {int(root_pid)}
        pending = [int(root_pid)]
        while pending:
            parent = pending.pop()
            try:
                children = psutil.Process(parent).children(recursive=False)
            except (psutil.Error, OSError):
                children = []
            for child in children:
                if child.pid not in pids:
                    pids.add(child.pid)
                    pending.append(child.pid)
        rss = uss = vms = threads = 0
        live = []
        for pid in sorted(pids):
            try:
                process = psutil.Process(pid)
                memory = process.memory_full_info()
                rss += int(memory.rss)
                uss += int(getattr(memory, "uss", 0) or 0)
                vms += int(memory.vms)
                threads += int(process.num_threads())
                live.append(pid)
            except (psutil.Error, OSError):
                continue
        sample.update({
            "psutil_available": True,
            "root_pid": int(root_pid),
            "descendant_pids": live,
            "tree_rss_bytes": rss,
            "tree_uss_bytes": uss,
            "tree_vms_bytes": vms,
            "thread_count": threads,
            "available_memory_bytes": int(psutil.virtual_memory().available),
        })
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(sample, sort_keys=True) + "\n")
        stream.flush()


def supervise_repeat(command, *, resource_path, context):
    process = subprocess.Popen(command, cwd=Path.cwd(), stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
    stop = threading.Event()
    def sample_loop():
        append_resource_sample(resource_path, root_pid=process.pid, context=context)
        while not stop.wait(5.0):
            append_resource_sample(resource_path, root_pid=process.pid, context=context)
    sampler = threading.Thread(target=sample_loop, name="calibration-resource-monitor", daemon=True)
    sampler.start()
    stdout, stderr = process.communicate()
    stop.set()
    sampler.join(timeout=6.0)
    append_resource_sample(resource_path, root_pid=process.pid, context={**context, "terminal": True})
    return process.returncode, stdout, stderr


def json_load(path):
    return read_json(path)


def verify_screen_seal(root):
    manifest = Path(root) / "artifact_hashes.sha256"
    sealed = Path(root) / "SEALED"
    if not manifest.exists() or not sealed.exists():
        return False
    digest = sha256_file(manifest)
    text = sealed.read_text(encoding="utf-8").strip()
    try:
        expected = json.loads(text)["artifact_hashes_sha256"]
    except json.JSONDecodeError:
        expected = text
    if digest != expected:
        return False
    for line in manifest.read_text(encoding="utf-8").splitlines():
        expected_file, relative = line.split("  ", 1)
        path = Path(root) / relative
        if not path.exists() or sha256_file(path) != expected_file:
            return False
    return True


def seal(root):
    root = Path(root)
    manifest = root / "artifact_hashes.sha256"
    lines = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name in {"artifact_hashes.sha256", "SEALED"}:
            continue
        lines.append(f"{sha256_file(path)}  {path.relative_to(root).as_posix()}")
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    digest = sha256_file(manifest)
    (root / "SEALED").write_text(digest + "\n", encoding="utf-8")
    if not verify_screen_seal(root):
        raise RuntimeError("calibration seal verification failed")


def package_existing_calibration(source_root, package_root):
    """Create a no-solver provenance addendum for an already sealed run."""
    source_root = Path(source_root).resolve()
    package_root = Path(package_root).resolve()
    if not verify_screen_seal(source_root):
        raise RuntimeError("source calibration is not sealed")
    if package_root.exists():
        raise FileExistsError(package_root)
    package_root.mkdir(parents=True, exist_ok=False)
    (package_root / "lineage.lock").write_text(package_root.name + "\n", encoding="utf-8")
    for source in source_root.rglob("*"):
        if not source.is_file() or source.name in {"artifact_hashes.sha256", "SEALED"}:
            continue
        relative = source.relative_to(source_root)
        destination = package_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    write_code_identity(package_root)
    preflight = json_load(source_root / "preflight.json")
    resource_path = package_root / "resource_samples.jsonl"
    samples = [{
        "schema": "r16_jackpot_calibration_resource_sample_v1",
        "sample_kind": "preflight",
        "coverage": "posthoc_boundary_only",
        "source": "sealed_calibration_preflight",
        "available_memory_bytes": preflight.get("available_memory_bytes"),
        "competing_processes": preflight.get("competing_processes", []),
    }]
    for event_path in sorted(source_root.glob("repeats/attempt_*/repeat_*/supervisor_event.json")):
        event = json_load(event_path)
        parts = event_path.parts
        samples.append({
            "schema": "r16_jackpot_calibration_resource_sample_v1",
            "sample_kind": "post_repeat_boundary",
            "coverage": "posthoc_boundary_only",
            "source": str(event_path.relative_to(source_root)),
            "attempt": next((int(part.split("_")[1]) for part in parts if part.startswith("attempt_")), None),
            "repeat": next((int(part.split("_")[1]) for part in parts if part.startswith("repeat_")), None),
            "available_memory_bytes": event.get("available_memory_after"),
            "wall_seconds": event.get("wall_seconds"),
            "returncode": event.get("returncode"),
        })
    write_text(resource_path, "".join(json.dumps(sample, sort_keys=True) + "\n" for sample in samples))
    atomic_write(package_root / "analysis" / "calibration_completion_audit.json", {
        "schema": "r16_jackpot_calibration_completion_audit_v1",
        "sealed_source_lineage": str(source_root),
        "sealed_source_verified": True,
        "solver_rerun": False,
        "code_identity_capture": "posthoc_addendum_capture; not a substitute for a pre-launch code snapshot",
        "resource_capture": "posthoc preflight and per-repeat boundary facts; no five-second samples can be reconstructed",
        "semantic_move_diff": "screen and calibration candidate source namespaces are not aligned for exact historical request/destination overlap",
        "no_alternative_schedule_outcome_inferred": True,
    })
    atomic_write(package_root / "study_manifest.json", {
        "schema": "r16_jackpot_calibration_completion_package_v1",
        "status": "sealed_posthoc_addendum",
        "sealed_source_lineage": str(source_root),
        "sealed_source_manifest_sha256": sha256_file(source_root / "artifact_hashes.sha256"),
        "solver_rerun": False,
        "resource_sampling_coverage": "posthoc_boundary_only",
        "code_identity_capture": "posthoc",
    })
    seal(package_root)
    return package_root


def competing_processes():
    if psutil is None:
        return ["psutil_unavailable"]
    matches = []
    current = os.getpid()
    for process in psutil.process_iter(["pid", "name", "cmdline"]):
        if process.info["pid"] == current:
            continue
        command = " ".join(process.info.get("cmdline") or []).lower()
        name = (process.info.get("name") or "").lower()
        is_python = name in {"python.exe", "pythonw.exe", "pypy.exe", "pypy3.exe"}
        if "cp-sat" in command or "celery" in command or (
            is_python and any(token in command for token in ("benchmark_r16", "benchmark_fixed_phase"))
        ):
            matches.append({"pid": process.info["pid"], "command": command})
    return matches


def create_root(parent, lineage_id):
    root = Path(parent).resolve() / lineage_id
    if root.exists():
        raise FileExistsError(root)
    if root == PRIOR_STUDY_ROOT.resolve() or PRIOR_STUDY_ROOT.resolve() in root.parents:
        raise ValueError("calibration output may not be inside historical study")
    root.mkdir(parents=True, exist_ok=False)
    (root / "lineage.lock").write_text(lineage_id + "\n", encoding="utf-8")
    for name in ("source", "states", "repeats", "analysis", "report"):
        (root / name).mkdir(exist_ok=False)
    return root


def prepare(root):
    root = Path(root)
    if not verify_screen_seal(SCREEN_ROOT):
        raise RuntimeError("sealed one-worker screen failed integrity gate")
    if not verify_screen_seal(PRIOR_STUDY_ROOT):
        raise RuntimeError("sealed historical study failed integrity gate")
    if not verify_screen_seal(FORENSIC_AUDIT_ROOT):
        raise RuntimeError("sealed forensic follow-up failed integrity gate")
    source_hash = sha256_file(SOURCE_PATH)
    if source_hash != EXPECTED_SOURCE_SHA256:
        raise RuntimeError("authoritative source hash mismatch")
    from .student_assignment.stage2_benchmark import read_durable_stage2_benchmark
    data = read_durable_stage2_benchmark(REFERENCE_TARGET)["data"]
    states = [state for state in load_states() if int(state["attempt_index"]) in JACKPOT_ATTEMPTS and state["branch"] == "r16_only"]
    if len(states) != 3:
        raise RuntimeError(f"expected three jackpot states, got {len(states)}")
    for state in states:
        checkpoint = PRIOR_STUDY_ROOT / state["checkpoint_path"]
        branch = read_diagnostic_branch_checkpoint(checkpoint, data=data)
        actual = source_decision_fingerprint(tuple(branch["source_decisions"]))
        expected = state.get("source_fingerprint_before") or state.get("source_fingerprint")
        if expected and actual != expected:
            raise RuntimeError(
                f"jackpot attempt {state['attempt_index']} source fingerprint mismatch"
            )
    shutil.copyfile(SOURCE_PATH, root / "source" / SOURCE_PATH.name)
    source_hash_copy = sha256_file(root / "source" / SOURCE_PATH.name)
    atomic_write(root / "source" / "source_identity.json", {
        "schema": "r16_jackpot_calibration_source_identity_v1",
        "source_path": str(SOURCE_PATH), "source_sha256": source_hash_copy,
        "expected_source_sha256": EXPECTED_SOURCE_SHA256, "input_fingerprint": EXPECTED_INPUT,
        "model_fingerprint": EXPECTED_MODEL, "source_value": EXPECTED_VALUE,
        "assignment_count": EXPECTED_ASSIGNMENTS, "special_commitment_count": EXPECTED_COMMITMENTS,
        "required_decision_group_count": EXPECTED_GROUPS, "unmet_request_count": 0,
    })
    atomic_write(root / "states" / "calibration_states.json", {
        "schema": "r16_jackpot_calibration_states_v1",
        "states": states, "historical_lineage": str(PRIOR_STUDY_ROOT),
        "fixed_targeting": "historical_top_individual_scope",
    })
    atomic_write(root / "experiment_contract.json", {
        "schema": "r16_eight_worker_jackpot_calibration_contract_v1",
        "states": list(JACKPOT_ATTEMPTS), "repeats_per_state": 3, "cells": 9,
        "operator": "targeted_utilization_r16_s4", "radius": 16, "changed_student_cap": 4,
        "target_policy": "fixed_historical_top_individual_scope", "worker_count": WORKERS,
        "validation_worker_count": VALIDATION_WORKERS, "seed": SEED,
        "requested_search_seconds": SEARCH_SECONDS, "requested_validation_seconds": VALIDATION_SECONDS,
        "parent_time_limit_seconds": PARENT_SECONDS, "objective_semantics_version": "v2",
        "objective_profile": "balanced", "hints": "current incumbent-derived hints unchanged",
        "strict_improvement_authority_unchanged": True, "production_wiring_changed": False,
        "large_held_out_study_run": False, "created_at_utc": time.time(),
    })
    atomic_write(root / "preflight.json", {
        "schema": "r16_jackpot_calibration_preflight_v1",
        "source_sha256": source_hash_copy, "screen_seal_verified": True,
        "historical_seal_verified": True, "competing_processes": competing_processes(),
        "python": sys.version, "platform": platform.platform(), "pid": os.getpid(),
        "available_memory_bytes": psutil.virtual_memory().available if psutil else None,
    })
    write_code_identity(root)
    write_text(root / "resource_samples.jsonl", "")
    return states


def run_one_with_data(root, attempt, repeat):
    # Importing the benchmark data is intentionally done inside the fresh
    # repeat process.  This avoids carrying a model or decoded source from a
    # prior repeat while keeping the exact runner boundary shared.
    from .student_assignment.stage2_benchmark import read_durable_stage2_benchmark
    data = read_durable_stage2_benchmark(REFERENCE_TARGET)["data"]
    states = json_load(Path(root) / "states" / "calibration_states.json")["states"]
    state = next(state for state in states if int(state["attempt_index"]) == attempt)
    cell_root = Path(root) / "repeats" / f"attempt_{attempt:03d}" / f"repeat_{repeat:02d}"
    cell_root.mkdir(parents=True, exist_ok=False)
    atomic_write(cell_root / "process_identity.json", {
        "schema": "r16_jackpot_calibration_process_identity_v1", "pid": os.getpid(),
        "python": sys.version, "platform": platform.platform(), "workers": WORKERS,
        "seed": SEED, "started_monotonic": time.monotonic(),
    })
    row = run_cell(data, state, seed=SEED, policy="top_individual", output_root=cell_root,
                   search_seconds=SEARCH_SECONDS, validation_seconds=VALIDATION_SECONDS,
                   worker_count=WORKERS, fixed_scope=tuple(state["executed_scope"]),
                   parent_time_limit_seconds=PARENT_SECONDS)
    generated = next((cell_root / "cells").iterdir())
    target = cell_root / "result"
    generated.rename(target)
    (cell_root / "cells").rmdir()
    atomic_write(cell_root / "result_summary.json", row)
    return row


def summarize(root):
    historical = {}
    with (FORENSIC_INPUT_ROOT / "attempt_master.csv").open(newline="", encoding="utf-8") as stream:
        for row in csv.DictReader(stream):
            if row.get("branch") == "r16_only" and int(row["attempt_index"]) in JACKPOT_ATTEMPTS:
                historical[int(row["attempt_index"])] = row
    rows = []
    for path in sorted((Path(root) / "repeats").glob("attempt_*/repeat_*/result_summary.json")):
        row = json_load(path)
        attempt = int(row["attempt_index"])
        result_dir = path.parent / "result"
        result = json_load(result_dir / "solver_result.json")
        cell_manifest = json_load(result_dir / "cell_manifest.json")
        inner = (result.get("attempt") or {}).get("inner_probe_summaries") or [{}]
        probe = inner[0]
        hist = historical[attempt]
        scope = sorted(int(value) for value in row.get("scope", []))
        scope_fingerprint = hashlib.sha256(
            json.dumps(scope, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        hint_path = result_dir / "target_hint_snapshot.json.gz"
        rows.append({
            "attempt": attempt, "repeat": int(path.parent.name.split("_")[-1]), "cell_id": row["cell_id"],
            "checkpoint_path": next(state["checkpoint_path"] for state in json_load(Path(root) / "states" / "calibration_states.json")["states"] if int(state["attempt_index"]) == attempt),
            "source_fingerprint_before": row.get("source_fingerprint_before"),
            "scope_fingerprint": scope_fingerprint,
            "hint_fingerprint": sha256_file(hint_path),
            "historical_gain": float(hist["authoritative_gain"]), "gain": float(row.get("gain", 0) or 0),
            "candidate_discovery_gain": float(row.get("candidate_discovery_gain", 0) or 0),
            "historical_candidate_fingerprint": hist.get("source_fingerprint_after"),
            "candidate_fingerprint": result.get("candidate_authority", {}).get("candidate_source_fingerprint"),
            "candidate_fingerprint_match": result.get("candidate_authority", {}).get("candidate_source_fingerprint") == hist.get("source_fingerprint_after"),
            "scope": row.get("scope"), "historical_scope": json.loads(hist["canonical_scope"]),
            "worker_count": result.get("worker_count"), "seed": row.get("seed"),
            "requested_search_limit_seconds": cell_manifest.get("search_seconds"),
            "effective_search_limit_seconds": probe.get("effective_search_limit_seconds", cell_manifest.get("search_seconds")),
            "candidate_found": row.get("candidate_found"), "candidate_validated": row.get("candidate_validated"), "adopted": row.get("adopted"),
            "validation_classification": row.get("validation_classification"),
            "solver_wall_seconds": row.get("solver_wall_seconds"), "attempt_wall_seconds": row.get("attempt_wall_seconds"),
            "validation_effective_seconds": probe.get("validation_effective_time_limit_seconds"),
            "validation_wall_seconds": probe.get("validation_elapsed_seconds"),
            "changed_student_count": row.get("changed_student_count"), "changed_source_decision_count": row.get("changed_source_decision_count"),
            "affected_section_ids": probe.get("affected_section_ids", []),
            "component_deltas": probe.get("component_deltas", {}),
            "candidate_file_persisted": (result_dir / "candidate.json.gz").exists(),
            "semantic_move_diff_available": False,
            "status": row.get("status"), "branches": probe.get("branches"), "conflicts": probe.get("conflicts"),
            "no_alternative_schedule_inferred": True,
        })
    atomic_write(Path(root) / "analysis" / "calibration_summary.json", {"schema": "r16_jackpot_calibration_summary_v1", "rows": rows})
    with (Path(root) / "analysis" / "eight_worker_jackpot_calibration.csv").open("w", newline="", encoding="utf-8") as stream:
        fields = list(rows[0]) if rows else []
        writer = csv.DictWriter(stream, fieldnames=fields); writer.writeheader(); writer.writerows(rows)
    by_state = {}
    for row in rows:
        by_state.setdefault(row["attempt"], []).append(row)
    state_summary = {}
    for attempt, group in by_state.items():
        gains = [row["gain"] for row in group]
        state_summary[str(attempt)] = {"historical_gain": group[0]["historical_gain"], "min": min(gains), "median": median(gains), "max": max(gains), "mean": mean(gains), "exact_candidate_matches": sum(row["candidate_fingerprint_match"] for row in group), "ge_60": sum(gain >= 60 for gain in gains), "ge_90": sum(gain >= 90 for gain in gains), "candidate_fingerprints": [row["candidate_fingerprint"] for row in group], "move_breadths": [row["changed_student_count"] for row in group]}
    exact = sum(row["candidate_fingerprint_match"] for row in rows)
    jackpot = sum(row["gain"] >= row["historical_gain"] for row in rows)
    overall = {"state_summary": state_summary, "repeat_count": len(rows), "exact_candidate_match_count": exact, "jackpot_reproduction_count": jackpot, "ge_60_count": sum(row["gain"] >= 60 for row in rows), "ge_90_count": sum(row["gain"] >= 90 for row in rows), "reproduction_rate": jackpot / len(rows) if rows else None}
    atomic_write(Path(root) / "analysis" / "calibration_classification.json", {"schema": "r16_jackpot_calibration_classification_v1", "overall": overall, "state_classifications": {str(attempt): "A_HIGHLY_REPRODUCIBLE_EIGHT_WORKER_JACKPOT" if info["exact_candidate_matches"] >= 2 else "B_EIGHT_WORKER_HIGH_UPSIDE_BUT_VARIABLE" if info["max"] >= 60 else "C_HISTORICAL_JACKPOT_NOT_REPRODUCED" for attempt, info in state_summary.items()}})
    lines = ["# Eight-worker jackpot calibration", "", "Nine sequential fresh-process repeats used seed 101, eight optimization workers, the exact historical TOP scopes, the existing R16/S4 operator, current incumbent hints, a 300-second CP-SAT ceiling, and a corrected 480-second parent budget reserving 180 seconds for independent full-model validation.", ""]
    for attempt in JACKPOT_ATTEMPTS:
        info = state_summary.get(str(attempt), {})
        lines.append(f"- Attempt {attempt}: historical +{info.get('historical_gain')}; repeats min/median/max {info.get('min')}/{info.get('median')}/{info.get('max')}; exact candidate fingerprints {info.get('exact_candidate_matches')}/3; >=60 {info.get('ge_60')}/3; >=90 {info.get('ge_90')}/3.")
    lines += ["", f"Across all repeats, exact candidate fingerprint matches: {exact}/{len(rows)}; historical-gain-or-better reproductions: {jackpot}/{len(rows)}. No alternative schedule outcome is inferred from a nonmatching repeat.", "", "The worker/jackpot conclusion is determined descriptively from these distributions: repeated jackpot-level outcomes indicate high reproducibility; mixed high and weak outcomes indicate high upside but variance; consistently weak outcomes indicate non-reproduction. Contract comparability remains separately audited in the forensic lineage."]
    write_text(Path(root) / "report" / "eight_worker_jackpot_calibration.md", "\n".join(lines) + "\n")
    return rows, overall


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--parent", type=Path, default=Path(r"C:\Users\desou\research_runs"))
    parser.add_argument("--lineage-id", required=True)
    parser.add_argument("--confirm-lineage-id", required=True)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--run-one-attempt", type=int)
    parser.add_argument("--run-one-repeat", type=int)
    args = parser.parse_args(argv)
    if args.confirm_lineage_id != args.lineage_id:
        raise RuntimeError("lineage confirmation mismatch")
    if args.run_one_attempt is not None:
        if args.run_one_repeat is None:
            raise RuntimeError("--run-one-repeat required")
        run_one_with_data(args.parent / args.lineage_id, args.run_one_attempt, args.run_one_repeat)
        return 0
    root = create_root(args.parent, args.lineage_id)
    states = prepare(root)
    if not args.run:
        print(json.dumps({"root": str(root), "prepared": True}, sort_keys=True)); return 0
    for state in states:
        attempt = int(state["attempt_index"])
        for repeat in (1, 2, 3):
            if competing_processes():
                raise RuntimeError("competing solver/research process detected")
            start = time.monotonic()
            command = [sys.executable, "-m", "scheduling_engine.benchmark_r16_jackpot_calibration", "--parent", str(args.parent), "--lineage-id", args.lineage_id, "--confirm-lineage-id", args.lineage_id, "--run-one-attempt", str(attempt), "--run-one-repeat", str(repeat)]
            returncode, stdout, stderr = supervise_repeat(
                command,
                resource_path=root / "resource_samples.jsonl",
                context={"attempt": attempt, "repeat": repeat},
            )
            repeat_path = root / "repeats" / f"attempt_{attempt:03d}" / f"repeat_{repeat:02d}"
            atomic_write(repeat_path / "supervisor_event.json", {
                "schema": "r16_jackpot_calibration_supervisor_event_v1",
                "returncode": returncode,
                "wall_seconds": time.monotonic() - start,
                "stdout": stdout,
                "stderr": stderr,
                "available_memory_after": psutil.virtual_memory().available if psutil else None,
                "resource_cadence_seconds": 5.0,
            })
            if returncode != 0:
                raise RuntimeError(f"calibration repeat failed: {attempt}/{repeat}: {stderr}")
            if psutil:
                time.sleep(2)
    rows, overall = summarize(root)
    atomic_write(root / "study_manifest.json", {
        "schema": "r16_jackpot_calibration_manifest_v1",
        "lineage_id": args.lineage_id,
        "status": "complete",
        "cells": len(rows),
        "classification": overall,
        "forensic_lineage": str(FORENSIC_AUDIT_ROOT),
        "screen_lineage": str(SCREEN_ROOT),
        "contract": {
            "worker_count": WORKERS,
            "validation_worker_count": VALIDATION_WORKERS,
            "seed": SEED,
            "search_seconds": SEARCH_SECONDS,
            "validation_seconds": VALIDATION_SECONDS,
            "parent_seconds": PARENT_SECONDS,
        },
    })
    seal(root)
    print(json.dumps({"root": str(root), "cells": len(rows), "classification": overall}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
