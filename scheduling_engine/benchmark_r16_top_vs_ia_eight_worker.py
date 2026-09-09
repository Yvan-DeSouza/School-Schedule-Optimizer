"""Production-like, one-transition R16/S4 TOP versus IA qualification.

This is a research-only runner.  Each cell starts in a fresh Python process
from the same authoritative historical checkpoint, executes exactly one
R16/S4 probe, and publishes its result before the next cell starts.  It does
not alter the ordinary scheduler, adaptive operator selection, hints,
Objective Semantics v2, hard constraints, or candidate authority.
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import statistics
import subprocess
import sys
import time
import traceback

try:
    import psutil
except ImportError:  # pragma: no cover - heavy preflight rejects this host
    psutil = None

from .benchmark_r16_target_selection_screen import (
    EXPECTED_ASSIGNMENTS,
    EXPECTED_COMMITMENTS,
    EXPECTED_GROUPS,
    EXPECTED_INPUT,
    EXPECTED_MODEL,
    EXPECTED_SOURCE_SHA256,
    EXPECTED_VALUE,
    PRIOR_STUDY_ROOT,
    REFERENCE_TARGET,
    SOURCE_PATH,
    atomic_write,
    read_json,
    run_cell,
    sha256_file,
)
from .student_assignment.runtime import semantic_student_assignment_input_fingerprint
from .student_assignment.search_experiments import source_decision_fingerprint
from .student_assignment.stage2_benchmark import (
    read_diagnostic_branch_checkpoint,
    read_durable_stage2_benchmark,
)
from .student_assignment.calibration_supervisor import (
    process_tree_snapshot,
    terminate_process_tree,
)


SCHEMA = "v2_r16_top_vs_ia_eight_worker_qualification_v1"
CELL_SCHEMA = "r16_top_vs_ia_qualification_cell_v1"
POLICIES = ("top_individual", "interaction_aware")
SEED = 101
WORKERS = 8
VALIDATION_WORKERS = 1
SEARCH_SECONDS = 300.0
VALIDATION_SECONDS = 180.0
PARENT_SECONDS = 720.0
TARGET_SCOPE_SIZE = 4
FORENSIC_ROOT = Path(
    r"C:\Users\desou\research_runs\v2_r16_scope_structure_forensics_20260908_9c4b71d8"
)
OUTER_PARENT = Path(r"C:\Users\desou\research_runs")
JACKPOT_ATTEMPTS = {27, 36, 60, 63}
COMPONENTS = (
    "course_category_diversity",
    "course_sequence_preferences",
    "difficulty_balance",
    "section_utilization_balance",
    "student_semester_load_balance",
)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def json_bytes(payload):
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        default=str,
    ).encode("utf-8")


def write_atomic(path, payload, *, compressed=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = gzip.compress(json_bytes(payload), mtime=0) if compressed else json_bytes(payload)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def read_payload(path):
    path = Path(path)
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            return json.load(stream)
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_bytes(raw):
    return hashlib.sha256(raw).hexdigest()


def verify_sealed(root):
    root = Path(root)
    manifest = root / "artifact_hashes.sha256"
    sealed = root / "SEALED"
    if not manifest.exists() or not sealed.exists():
        return {"classification": "not_sealed", "path": str(root)}
    try:
        seal = json.loads(sealed.read_text(encoding="utf-8"))
        expected = seal["artifact_hashes_sha256"]
    except (OSError, ValueError, KeyError):
        return {"classification": "invalid_seal", "path": str(root)}
    actual_manifest = sha256_file(manifest)
    mismatches = []
    for line in manifest.read_text(encoding="utf-8").splitlines():
        digest, relative = line.split("  ", 1)
        path = root / relative
        if not path.exists() or sha256_file(path) != digest:
            mismatches.append(relative)
    return {
        "classification": "sealed_and_verified" if actual_manifest == expected and not mismatches else "seal_mismatch",
        "path": str(root),
        "manifest_sha256": actual_manifest,
        "mismatches": mismatches,
        "entry_count": len(manifest.read_text(encoding="utf-8").splitlines()),
    }


def state_catalog():
    payload = read_payload(FORENSIC_ROOT / "held_out_scope_states.json")
    if payload.get("state_count") != 8 or payload.get("cell_count") != 48:
        raise RuntimeError("the eight-state forensic design is not the expected 48-cell contract")
    states = []
    for item in payload["states"]:
        state = dict(item)
        state["branch"] = "r16_only"
        state["historical_top_gain"] = 84.0 if int(state["attempt_index"]) == 60 else None
        states.append(state)
    return states


def source_rows_csv():
    path = FORENSIC_ROOT / "r16_8worker_scope_outcomes.csv"
    with path.open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def parse_json_cell(value, default=None):
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def trajectory_audit():
    rows = source_rows_csv()
    by_attempt = {int(row["attempt_index"]): row for row in rows}
    jackpot_expected = {
        27: {"previous": [263, 273, 283, 782], "current": [263, 273, 283, 1170], "gain": 114.0, "ranks": [6], "retained": 3},
        36: {"previous": [148, 540, 571, 1332], "current": [148, 540, 571, 840], "gain": 96.0, "ranks": [6], "retained": 3},
        60: {"previous": [67, 87, 592, 1392], "current": [67, 87, 597, 1392], "gain": 84.0, "ranks": [5], "retained": 3},
        63: {"previous": [179, 472, 592, 692], "current": [179, 283, 592, 730], "gain": 102.0, "ranks": [5, 6], "retained": 2},
    }
    details = []
    for attempt, expected in jackpot_expected.items():
        previous = parse_json_cell(by_attempt[attempt - 1]["target_scope"])
        current = parse_json_cell(by_attempt[attempt]["target_scope"])
        ranking = parse_json_cell(by_attempt[attempt - 1]["top_10_ranking"], [])
        rank_by_student = {int(row["student_id"]): row for row in ranking}
        previous_set, current_set = set(previous), set(current)
        entrants = sorted(current_set - previous_set)
        leavers = sorted(previous_set - current_set)
        incoming = [{
            "student_id": student,
            "previous_rank": int(rank_by_student[student]["rank"]),
            "previous_leverage": float(rank_by_student[student]["total_positive_leverage"]),
        } for student in entrants]
        outgoing = [{
            "student_id": student,
            "previous_rank": int(rank_by_student[student]["rank"]),
            "previous_leverage": float(rank_by_student[student]["total_positive_leverage"]),
        } for student in leavers]
        recent_gains = [
            float(by_attempt[index]["authoritative_gain"])
            for index in range(max(1, attempt - 5), attempt)
        ]
        near_tie = [
            {"rank": int(item["rank"]), "student_id": int(item["student_id"]), "leverage": float(item["total_positive_leverage"])}
            for item in ranking if 4 <= int(item["rank"]) <= 8
        ]
        actual = {
            "attempt": attempt,
            "previous_scope": previous,
            "current_scope": current,
            "retained_count": len(previous_set & current_set),
            "entrants": incoming,
            "leavers": outgoing,
            "incoming_ranks": [row["previous_rank"] for row in incoming],
            "incoming_minus_outgoing_leverage_delta": (
                sum(row["previous_leverage"] for row in incoming)
                - sum(row["previous_leverage"] for row in outgoing)
            ),
            "authoritative_gain": float(by_attempt[attempt]["authoritative_gain"]),
            "recent_1_attempt_gains": recent_gains[-1:],
            "recent_3_attempt_gains": recent_gains[-3:],
            "recent_5_attempt_gains": recent_gains[-5:],
            "repeated_scope_streak_before": int(by_attempt[attempt]["prior_scope_observations"]),
            "near_tie_rank_4_to_8": near_tie,
            "near_tie_band_leverage_width": (
                max(row["leverage"] for row in near_tie)
                - min(row["leverage"] for row in near_tie)
                if near_tie else 0.0
            ),
        }
        if actual["previous_scope"] != expected["previous"] or actual["current_scope"] != expected["current"]:
            raise RuntimeError(f"trajectory scope mismatch at attempt {attempt}: {actual}")
        if actual["retained_count"] != expected["retained"] or actual["incoming_ranks"] != expected["ranks"]:
            raise RuntimeError(f"trajectory rank/retention mismatch at attempt {attempt}: {actual}")
        if actual["authoritative_gain"] != expected["gain"]:
            raise RuntimeError(f"trajectory gain mismatch at attempt {attempt}: {actual}")
        details.append(actual)
    fresh = [row for row in rows if row["scope_novelty"] == "fresh_student_set"]
    repeated = [row for row in rows if row["scope_novelty"] == "repeated_student_set_new_incumbent"]
    aggregate = {
        "fresh_student_sets": {
            "count": len(fresh),
            "mean_gain": statistics.mean(float(row["authoritative_gain"]) for row in fresh),
            "median_gain": statistics.median(float(row["authoritative_gain"]) for row in fresh),
            "maximum_gain": max(float(row["authoritative_gain"]) for row in fresh),
            "jackpot_attempts": [int(row["attempt_index"]) for row in fresh if int(row["attempt_index"]) in JACKPOT_ATTEMPTS],
        },
        "repeated_student_sets_on_new_incumbent": {
            "count": len(repeated),
            "mean_gain": statistics.mean(float(row["authoritative_gain"]) for row in repeated),
            "median_gain": statistics.median(float(row["authoritative_gain"]) for row in repeated),
            "maximum_gain": max(float(row["authoritative_gain"]) for row in repeated),
            "at_least_30": sum(float(row["authoritative_gain"]) >= 30 for row in repeated),
            "at_least_60": sum(float(row["authoritative_gain"]) >= 60 for row in repeated),
        },
    }
    if aggregate["fresh_student_sets"]["count"] != 57 or aggregate["repeated_student_sets_on_new_incumbent"]["count"] != 13:
        raise RuntimeError(f"fresh/repeated population mismatch: {aggregate}")
    if round(aggregate["fresh_student_sets"]["mean_gain"], 2) != 18.95 or round(aggregate["repeated_student_sets_on_new_incumbent"]["mean_gain"], 2) != 10.15:
        raise RuntimeError(f"fresh/repeated gain mismatch: {aggregate}")
    return {"schema": "near_tie_scope_rotation_hypothesis_v1", "hypothesis": "R16 jackpot opportunity may sometimes emerge when a mostly stable high-pressure core is combined with different near-tied candidates just below the current top-four cutoff, particularly after the exact scope has produced only small improvements.", "jackpot_transitions": details, "aggregate": aggregate, "verified_from_authoritative_csv": True, "qualified_policy": False, "implementation_changed": False}


def validate_states(data, states):
    outcomes = {int(row["attempt_index"]): row for row in source_rows_csv()}
    verified = []
    for state in states:
        attempt = int(state["attempt_index"])
        checkpoint = PRIOR_STUDY_ROOT / state["checkpoint_path"]
        branch = read_diagnostic_branch_checkpoint(checkpoint, data=data)
        source = tuple(branch["source_decisions"])
        materialized = source_decision_fingerprint(source)
        expected_fp = state["source_fingerprint"]
        if materialized != expected_fp:
            raise RuntimeError(f"source fingerprint mismatch for state {attempt}")
        actual_scope = parse_json_cell(outcomes[attempt]["target_scope"])
        if sorted(actual_scope) != sorted(state["top_scope"]):
            raise RuntimeError(f"TOP scope mismatch for state {attempt}")
        historical_rows = [
            outcomes[index] for index in sorted(outcomes)
            if index < attempt
        ]
        prior_scopes = [
            parse_json_cell(row["target_scope"])
            for row in historical_rows
        ]
        exact_scope_observations = sum(
            set(scope) == set(actual_scope) for scope in prior_scopes
        )
        previous_scope = prior_scopes[-1] if prior_scopes else []
        ranking = parse_json_cell(historical_rows[-1]["top_10_ranking"], []) if historical_rows else []
        rank_by_student = {int(item["student_id"]): item for item in ranking}
        entrants = sorted(set(actual_scope) - set(previous_scope))
        leavers = sorted(set(previous_scope) - set(actual_scope))
        recent_gains = [
            float(row["authoritative_gain"])
            for row in historical_rows[-5:]
        ]
        state["trajectory_context"] = {
            "previous_top_scopes": {
                str(count): prior_scopes[-count:]
                for count in (1, 2, 3, 5)
            },
            "exact_scope_observations_before": exact_scope_observations,
            "retained_core_count_from_previous_scope": len(set(actual_scope) & set(previous_scope)),
            "entrants_from_previous_scope": entrants,
            "leavers_from_previous_scope": leavers,
            "entrant_previous_ranks": {
                str(student): rank_by_student.get(student, {}).get("rank")
                for student in entrants
            },
            "entrant_previous_leverage": {
                str(student): rank_by_student.get(student, {}).get("total_positive_leverage")
                for student in entrants
            },
            "leaver_previous_leverage": {
                str(student): rank_by_student.get(student, {}).get("total_positive_leverage")
                for student in leavers
            },
            "entrants_from_previous_ranks_5_to_8": [
                student for student in entrants
                if rank_by_student.get(student, {}).get("rank") in {5, 6, 7, 8}
            ],
            "recent_1_attempt_gains": recent_gains[-1:],
            "recent_3_attempt_gains": recent_gains[-3:],
            "recent_5_attempt_gains": recent_gains[-5:],
            "scope_freshness": outcomes[attempt].get("scope_novelty"),
            "stable_core_length": sum(
                1 for prior in reversed(prior_scopes)
                if set(prior) & set(actual_scope)
            ),
            "near_tie_rank_4_to_8": [
                item for item in ranking if 4 <= int(item["rank"]) <= 8
            ],
        }
        verified.append({
            "branch": "r16_only",
            "attempt_index": attempt,
            "checkpoint_path": str(checkpoint),
            "source_fingerprint": materialized,
            "source_value": float(outcomes[attempt]["source_value"]),
            "top_scope": actual_scope,
            "interaction_aware_scope": state["interaction_aware_scope"],
            "historical_top_gain": float(outcomes[attempt]["authoritative_gain"]),
            "known_top_jackpot_positive_control": attempt == 60,
            "trajectory_context": state["trajectory_context"],
        })
    return verified


def competing_processes():
    if psutil is None:
        return [{"reason": "psutil_unavailable"}]
    current = os.getpid()
    ancestors = {current}
    try:
        ancestors.update(parent.pid for parent in psutil.Process(current).parents())
    except psutil.Error:
        pass
    matches = []
    for process in psutil.process_iter(["pid", "name", "cmdline"]):
        if process.info["pid"] in ancestors:
            continue
        name = (process.info.get("name") or "").lower()
        command = " ".join(process.info.get("cmdline") or ()).lower()
        if name in {"python.exe", "pythonw.exe", "pypy.exe", "pypy3.exe"} and any(
            token in command for token in ("benchmark_r16", "student_assignment", "celery")
        ):
            matches.append({"pid": process.info["pid"], "command": command})
    return matches


def power_sleep_preflight():
    result = subprocess.run(
        ["powercfg", "/query", "SCHEME_CURRENT", "SUB_SLEEP"],
        capture_output=True, text=True, check=False,
    )
    text = result.stdout.upper()
    verified = (
        result.returncode == 0
        and "STANDBYIDLE" in text
        and "HIBERNATEIDLE" in text
        and text.count("CURRENT AC POWER SETTING INDEX: 0X00000000") >= 2
    )
    return {"powercfg_returncode": result.returncode, "power_sleep_verified": verified}


def code_identity(root):
    code = Path(root) / "code"
    code.mkdir(parents=True, exist_ok=True)
    head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False).stdout.strip()
    status = subprocess.run(["git", "status", "--short"], capture_output=True, text=True, check=False).stdout
    diff = subprocess.run(["git", "diff", "--binary", "HEAD"], capture_output=True, check=False).stdout
    (code / "git_head.txt").write_text(head + "\n", encoding="utf-8")
    (code / "git_status.txt").write_text(status, encoding="utf-8")
    (code / "tracked_diff.patch").write_bytes(diff)
    changed = []
    for line in status.splitlines():
        relative = line[3:].strip().strip('"') if len(line) >= 3 else ""
        path = Path.cwd() / relative
        changed.append({"path": relative, "sha256": sha256_file(path) if path.is_file() else None})
    write_atomic(code / "code_fingerprints.json", {
        "schema": "r16_top_vs_ia_code_identity_v1",
        "git_head": head,
        "tracked_diff_sha256": sha256_bytes(diff),
        "changed_files": changed,
    })
    write_atomic(code / "dependency_versions.json", {
        "python": sys.version,
        "platform": platform.platform(),
        "psutil": getattr(psutil, "__version__", None),
        "ortools": __import__("ortools").__version__,
    })


def create_root(parent, lineage_id):
    parent = Path(parent).resolve()
    root = parent / lineage_id
    if root.exists():
        raise FileExistsError(root)
    forbidden = [
        SOURCE_PATH.parent.resolve(),
        PRIOR_STUDY_ROOT.resolve(),
        FORENSIC_ROOT.resolve(),
    ]
    if any(root == path or path in root.parents for path in forbidden):
        raise ValueError(f"unsafe output lineage path: {root}")
    root.mkdir(parents=True, exist_ok=False)
    with (root / "lineage.lock").open("x", encoding="utf-8") as stream:
        stream.write(lineage_id + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    for name in ("source", "smoke", "cells", "worker_results", "logs", "analysis", "report"):
        (root / name).mkdir(exist_ok=False)
    return root


def prepare(root):
    if psutil is None:
        raise RuntimeError("psutil is required for the five-second supervisor")
    available = psutil.virtual_memory().available
    if available < 4 * 1024**3:
        raise RuntimeError(f"available memory below 4 GiB hard preflight minimum: {available}")
    processes = competing_processes()
    if processes:
        raise RuntimeError(f"competing solver/research processes detected: {processes}")
    power = power_sleep_preflight()
    if not power["power_sleep_verified"]:
        raise RuntimeError(f"AC sleep preflight failed: {power}")
    seal = verify_sealed(FORENSIC_ROOT)
    if seal["classification"] != "sealed_and_verified":
        raise RuntimeError(f"forensic source lineage failed integrity: {seal}")
    benchmark = read_durable_stage2_benchmark(REFERENCE_TARGET)
    data = benchmark["data"]
    input_fp = semantic_student_assignment_input_fingerprint(data)
    if input_fp != EXPECTED_INPUT:
        raise RuntimeError(f"input fingerprint mismatch: {input_fp}")
    source_hash = sha256_file(SOURCE_PATH)
    if source_hash != EXPECTED_SOURCE_SHA256:
        raise RuntimeError("authoritative source byte hash mismatch")
    states = state_catalog()
    verified_states = validate_states(data, states)
    trajectory = trajectory_audit()
    shutil.copyfile(SOURCE_PATH, root / "source" / SOURCE_PATH.name)
    copied_hash = sha256_file(root / "source" / SOURCE_PATH.name)
    if copied_hash != source_hash:
        raise RuntimeError("copied source hash mismatch")
    code_identity(root)
    write_atomic(root / "source" / "source_identity.json", {
        "schema": "r16_top_vs_ia_source_identity_v1",
        "source_path": str(SOURCE_PATH),
        "source_sha256": source_hash,
        "input_fingerprint": input_fp,
        "model_fingerprint": EXPECTED_MODEL,
        "source_value": EXPECTED_VALUE,
        "ordinary_assignments": EXPECTED_ASSIGNMENTS,
        "special_commitments": EXPECTED_COMMITMENTS,
        "required_decision_groups": EXPECTED_GROUPS,
        "unmet_request_count": 0,
    })
    write_atomic(root / "states" / "state_catalog.json", {"schema": "r16_qualification_state_catalog_v1", "states": verified_states})
    write_atomic(root / "trajectory_hypothesis_v1.json", trajectory)
    preflight = {
        "schema": "r16_top_vs_ia_preflight_v1",
        "created_at_utc": utc_now(),
        "available_memory_bytes": available,
        "power": power,
        "competing_processes": processes,
        "source_lineage": seal,
        "source_sha256": source_hash,
        "copied_source_sha256": copied_hash,
        "input_fingerprint": input_fp,
        "model_fingerprint": EXPECTED_MODEL,
        "state_count": len(verified_states),
        "expected_cells": 48,
        "trajectory_verified": True,
    }
    write_atomic(root / "preflight.json", preflight)
    write_atomic(root / "experiment_contract.json", {
        "schema": SCHEMA,
        "lineage_id": root.name,
        "states": [state["attempt_index"] for state in verified_states],
        "policies": list(POLICIES),
        "cells": 48,
        "repeats_per_policy": 3,
        "operator": "targeted_utilization_r16_s4",
        "radius": 16,
        "changed_student_cap": 4,
        "seed": SEED,
        "worker_count": WORKERS,
        "validation_worker_count": VALIDATION_WORKERS,
        "requested_search_seconds": SEARCH_SECONDS,
        "requested_validation_seconds": VALIDATION_SECONDS,
        "parent_time_limit_seconds": PARENT_SECONDS,
        "hints": "current complete incumbent-derived hints unchanged",
        "objective_semantics": "v2 balanced unchanged",
        "strict_full_model_validation": True,
        "strict_improvement_adoption": True,
        "dynamic_continuation": False,
        "state_60_role": "KNOWN_TOP_JACKPOT_POSITIVE_CONTROL",
        "policy_order": "alternate within state and invert starting policy by state",
        "smoke_gate_required": True,
        "production_wiring_changed": False,
    })
    write_atomic(root / "progress.json", {"schema": "r16_qualification_progress_v1", "completed_cells": 0, "expected_cells": 48, "status": "prepared"})
    return data, verified_states


def append_resource(root, sample):
    path = Path(root) / "resource_samples.jsonl"
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(sample, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def worker_command(root, *, state_attempt, policy, repeat, execution, output_root, search_seconds, validation_seconds, exact_hints, parent_time_limit_seconds):
    return [
        sys.executable, "-m", "scheduling_engine.benchmark_r16_top_vs_ia_eight_worker",
        "--child-cell", "--study-root", str(root), "--output-root", str(output_root),
        "--state-attempt", str(state_attempt), "--policy", policy,
        "--repeat", str(repeat), "--execution", str(execution),
        "--search-seconds", str(search_seconds), "--validation-seconds", str(validation_seconds),
        "--parent-time-limit-seconds", str(parent_time_limit_seconds),
        "--exact-hints" if exact_hints else "--no-exact-hints",
    ]


def remaining_pids(pids):
    if psutil is None:
        return []
    alive = []
    for pid in pids:
        try:
            if psutil.Process(int(pid)).is_running():
                alive.append(int(pid))
        except psutil.Error:
            pass
    return alive


def supervise_cell(root, *, state_attempt, policy, repeat, execution, output_root, search_seconds, validation_seconds, exact_hints, hard_wall_seconds, parent_time_limit_seconds):
    cell_key = f"attempt_{state_attempt:03d}_{policy}_repeat_{repeat:02d}"
    output_path = Path(root) / "worker_results" / f"{cell_key}__execution_{execution}.json"
    log_path = Path(root) / "logs" / f"{cell_key}__execution_{execution}.log"
    command = worker_command(
        root, state_attempt=state_attempt, policy=policy, repeat=repeat,
        execution=execution, output_root=output_root,
        search_seconds=search_seconds, validation_seconds=validation_seconds,
        exact_hints=exact_hints,
        parent_time_limit_seconds=parent_time_limit_seconds,
    )
    started_mono = time.monotonic()
    started_wall = time.time()
    process = None
    observed_pids = set()
    samples = []
    termination = None
    warning_count = 0
    sleep_gap = False
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            command, cwd=Path(__file__).parents[1], stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0,
        )
        observed_pids.add(process.pid)
        next_sample = 0.0
        last_mono = started_mono
        last_wall = started_wall
        while process.poll() is None:
            now_mono = time.monotonic()
            now_wall = time.time()
            if now_mono >= next_sample:
                snapshot = process_tree_snapshot(process.pid)
                observed_pids.update(snapshot.get("pids") or ())
                sample = {
                    "schema": "r16_top_vs_ia_resource_sample_v1",
                    "utc_timestamp": utc_now(),
                    "monotonic_timestamp": now_mono,
                    "wall_timestamp": now_wall,
                    "cell": cell_key,
                    "state_attempt": state_attempt,
                    "policy": policy,
                    "repeat": repeat,
                    "execution": execution,
                    "root_pid": process.pid,
                    "elapsed_seconds": now_mono - started_mono,
                    "wall_gap_seconds": (now_wall - last_wall) - (now_mono - last_mono),
                    **snapshot,
                }
                samples.append(sample)
                append_resource(root, sample)
                if sample["wall_gap_seconds"] > 5.0:
                    sleep_gap = True
                if (snapshot.get("system_available_memory_bytes") or 10**30) < 2 * 1024**3 or (snapshot.get("tree_rss_bytes") or 0) > 3 * 1024**3:
                    warning_count += 1
                if (snapshot.get("system_available_memory_bytes") or 10**30) < 1536 * 1024**2 or (snapshot.get("tree_rss_bytes") or 0) > 4 * 1024**3:
                    termination = "resource_guard_terminated"
                    terminate_process_tree(process.pid, grace_seconds=5.0)
                    break
                next_sample = now_mono + 5.0
                last_mono, last_wall = now_mono, now_wall
            if now_mono - started_mono >= hard_wall_seconds:
                termination = "hard_deadline_terminated"
                terminate_process_tree(process.pid, grace_seconds=5.0)
                break
            time.sleep(min(0.25, max(0.01, hard_wall_seconds - (now_mono - started_mono))))
    return_code = process.poll()
    if return_code is None:
        return_code = -1
    final_snapshot = process_tree_snapshot(process.pid)
    observed_pids.update(final_snapshot.get("pids") or ())
    remaining = remaining_pids(observed_pids)
    if remaining:
        terminate_process_tree(process.pid, grace_seconds=5.0)
        remaining = remaining_pids(observed_pids)
    completed = output_path.exists() and return_code == 0 and termination is None
    payload = read_payload(output_path) if output_path.exists() else None
    if payload is not None and not payload.get("output_protocol_complete"):
        completed = False
    event = {
        "schema": "r16_top_vs_ia_supervisor_event_v1",
        "cell": cell_key,
        "state_attempt": state_attempt,
        "policy": policy,
        "repeat": repeat,
        "execution": execution,
        "command": command,
        "started_utc": datetime.fromtimestamp(started_wall, timezone.utc).isoformat(),
        "ended_utc": utc_now(),
        "elapsed_seconds": time.monotonic() - started_mono,
        "returncode": return_code,
        "termination": termination,
        "completed": completed,
        "sleep_gap_detected": sleep_gap,
        "resource_warning_sample_count": warning_count,
        "observed_pids": sorted(observed_pids),
        "remaining_pids_after_cleanup": remaining,
        "log_path": str(log_path),
        "worker_result_path": str(output_path),
        "resource_sample_count": len(samples),
        "payload": payload,
    }
    write_atomic(Path(root) / "cells" / f"{cell_key}__execution_{execution}" / "supervisor_event.json", event)
    return event


def run_smoke(root, data, states):
    state_attempt = int(states[0]["attempt_index"])
    results = []
    for mode, exact in (("off", False), ("on", True)):
        event = supervise_cell(
            root, state_attempt=state_attempt, policy="top_individual", repeat=0,
            execution=1, output_root=Path(root) / "smoke" / mode,
            search_seconds=15.0, validation_seconds=180.0, exact_hints=exact,
            hard_wall_seconds=360.0, parent_time_limit_seconds=300.0,
        )
        results.append(event)
    smoke = {"schema": "r16_hint_identity_smoke_result_v1", "cells": results, "passed": False, "exact_identity_enabled_for_grid": False}
    if all(event["completed"] and event.get("payload") for event in results):
        rows = [event["payload"]["row"] for event in results]
        def telemetry(row):
            attempt = (row.get("inner_probe_summaries") or [{}])[0]
            return attempt.get("hint_telemetry") or {}
        off, on = telemetry(rows[0]), telemetry(rows[1])
        off_exact = (on.get("exact_identity") or {})
        identity_ok = bool(
            off.get("hint_vector_fingerprint")
            and on.get("hint_vector_fingerprint")
            and off["hint_vector_fingerprint"] == on["hint_vector_fingerprint"]
            and off_exact.get("mapping_rows")
            and off_exact.get("variable_index_unique")
            and off_exact.get("incumbent_value_complete")
            and off_exact.get("hint_value_complete")
            and off_exact.get("mapping_fingerprint")
        )
        comparable = {
            key: rows[0].get(key) == rows[1].get(key)
            for key in ("scope", "source_fingerprint_before", "candidate_found", "candidate_validated", "adopted", "gain", "status")
        }
        smoke.update({
            "passed": identity_ok and all(comparable.values()),
            "exact_identity_enabled_for_grid": identity_ok and all(comparable.values()),
            "comparison": comparable,
            "off_hint_telemetry": {key: off.get(key) for key in ("hint_vector_fingerprint", "hint_count", "source_variable_count")},
            "on_hint_telemetry": {key: on.get(key) for key in ("hint_vector_fingerprint", "hint_count", "source_variable_count")},
            "exact_identity": {key: off_exact.get(key) for key in ("row_count", "source_decision_count", "variable_index_unique", "incumbent_value_complete", "hint_value_complete", "mapping_fingerprint")},
            "same_semantic_source": rows[0].get("source_fingerprint_before") == rows[1].get("source_fingerprint_before"),
            "same_selected_scope": rows[0].get("scope") == rows[1].get("scope"),
        })
    write_atomic(Path(root) / "smoke" / "smoke_result.json", smoke)
    return smoke


def grid_order(states):
    for index, state in enumerate(states):
        first = "top_individual" if index % 2 == 0 else "interaction_aware"
        second = "interaction_aware" if first == "top_individual" else "top_individual"
        for repeat, policies in ((1, (first, second)), (2, (second, first)), (3, (first, second))):
            for policy in policies:
                yield state, repeat, policy


def run_grid(root, data, states, exact_hints):
    rows = []
    plans = list(grid_order(states))
    for index, (state, repeat, policy) in enumerate(plans, start=1):
        attempt = int(state["attempt_index"])
        execution = 1
        event = supervise_cell(
            root, state_attempt=attempt, policy=policy, repeat=repeat,
            execution=execution, output_root=root,
            search_seconds=SEARCH_SECONDS, validation_seconds=VALIDATION_SECONDS,
            exact_hints=exact_hints, hard_wall_seconds=PARENT_SECONDS,
            parent_time_limit_seconds=PARENT_SECONDS,
        )
        if event["sleep_gap_detected"] or not event["completed"]:
            execution = 2
            event["rerun_reason"] = "host_sleep_contaminated" if event["sleep_gap_detected"] else "worker_or_protocol_failure"
            rerun = supervise_cell(
                root, state_attempt=attempt, policy=policy, repeat=repeat,
                execution=execution, output_root=root,
                search_seconds=SEARCH_SECONDS, validation_seconds=VALIDATION_SECONDS,
                exact_hints=exact_hints, hard_wall_seconds=PARENT_SECONDS,
                parent_time_limit_seconds=PARENT_SECONDS,
            )
            event["rerun"] = rerun
            if rerun["completed"] and rerun.get("payload"):
                event = rerun
                event["rerun_completed"] = True
        payload = event.get("payload") or {}
        row = dict(payload.get("row") or {})
        row.update({
            "grid_index": index,
            "repeat": repeat,
            "policy": policy,
            "state_attempt": attempt,
            "supervisor_event": event,
            "execution_used": event.get("execution"),
            "operationally_valid": bool(event.get("completed") and not event.get("sleep_gap_detected")),
        })
        rows.append(row)
        write_atomic(Path(root) / "progress.json", {
            "schema": "r16_qualification_progress_v1",
            "status": "running",
            "completed_cells": index,
            "expected_cells": 48,
            "last_cell": row,
            "updated_at_utc": utc_now(),
        })
        with (Path(root) / "cell_results.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, sort_keys=True, default=str) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    return rows


def percentile(values, fraction):
    if not values:
        return None
    values = sorted(float(value) for value in values)
    position = (len(values) - 1) * fraction
    low, high = int(position), min(len(values) - 1, int(position) + 1)
    weight = position - low
    return values[low] * (1 - weight) + values[high] * weight


def distribution(values):
    values = [float(value) for value in values]
    return {
        "count": len(values),
        "mean": statistics.mean(values) if values else None,
        "median": statistics.median(values) if values else None,
        "p25": percentile(values, 0.25),
        "p75": percentile(values, 0.75),
        "min": min(values) if values else None,
        "max": max(values) if values else None,
    }


def policy_stats(rows):
    gains = [float(row.get("gain", 0) or 0) for row in rows]
    valid = [row for row in rows if row.get("operationally_valid")]
    stats = {
        "cells": len(rows),
        "operationally_valid_cells": len(valid),
        "gain": distribution(gains),
        "probability_gain_at_least_30": sum(value >= 30 for value in gains) / len(gains) if gains else None,
        "probability_gain_at_least_60": sum(value >= 60 for value in gains) / len(gains) if gains else None,
        "probability_gain_at_least_90": sum(value >= 90 for value in gains) / len(gains) if gains else None,
        "changed_student_distribution": {str(key): sum(int(row.get("changed_student_count", 0) or 0) == key for row in rows) for key in range(1, 5)},
        "changed_decision_distribution": distribution([row.get("changed_source_decision_count", 0) or 0 for row in rows]),
        "probability_changed_decisions_at_least_15": sum(int(row.get("changed_source_decision_count", 0) or 0) >= 15 for row in rows) / len(rows) if rows else None,
        "solver_wall": distribution([row.get("solver_wall_seconds", 0) or 0 for row in rows]),
        "validation_wall": distribution([
            ((row.get("inner_probe_summaries") or [{}])[0].get("validation_elapsed_seconds", 0) or 0)
            for row in rows
        ]),
        "gain_per_search_minute": distribution([
            float(row.get("gain", 0) or 0) / (float(row.get("solver_wall_seconds", 0) or 0) / 60)
            if float(row.get("solver_wall_seconds", 0) or 0) > 0 else 0
            for row in rows
        ]),
        "gain_per_full_wall_minute": distribution([
            float(row.get("gain", 0) or 0) / (float(row.get("attempt_wall_seconds", 0) or 0) / 60)
            if float(row.get("attempt_wall_seconds", 0) or 0) > 0 else 0
            for row in rows
        ]),
        "solver_statuses": {},
        "validation_statuses": {},
    }
    for field, target in (("status", stats["solver_statuses"]), ("validation_classification", stats["validation_statuses"])):
        for row in rows:
            value = row.get(field) or "missing"
            target[value] = target.get(value, 0) + 1
    component = {name: [] for name in COMPONENTS}
    for row in rows:
        for name in COMPONENTS:
            component[name].append(float((row.get("objective_after") or {}).get("components", {}).get(name, {}).get("weighted_normalized_contribution", 0) or 0))
    stats["component_after_weighted_values"] = {name: distribution(values) for name, values in component.items()}
    return stats


def analyze(root, rows, trajectory, smoke):
    root = Path(root)
    by_policy = {policy: policy_stats([row for row in rows if row["policy"] == policy]) for policy in POLICIES}
    by_state = {}
    for attempt in sorted({row["state_attempt"] for row in rows}):
        state_rows = {policy: [row for row in rows if row["state_attempt"] == attempt and row["policy"] == policy] for policy in POLICIES}
        by_state[str(attempt)] = {
            "known_top_jackpot_positive_control": attempt == 60,
            "policies": {policy: policy_stats(state_rows[policy]) for policy in POLICIES},
            "ia_minus_top_mean_gain": statistics.mean(float(row.get("gain", 0) or 0) for row in state_rows["interaction_aware"]) - statistics.mean(float(row.get("gain", 0) or 0) for row in state_rows["top_individual"]),
            "ia_minus_top_median_gain": statistics.median(float(row.get("gain", 0) or 0) for row in state_rows["interaction_aware"]) - statistics.median(float(row.get("gain", 0) or 0) for row in state_rows["top_individual"]),
        }
    no60 = [row for row in rows if row["state_attempt"] != 60]
    all_diffs = [value["ia_minus_top_median_gain"] for value in by_state.values()]
    no60_diffs = [value["ia_minus_top_median_gain"] for key, value in by_state.items() if key != "60"]
    ia_wins = sum(value > 0 for value in all_diffs)
    top_wins = sum(value < 0 for value in all_diffs)
    no60_ia_wins = sum(value > 0 for value in no60_diffs)
    no60_top_wins = sum(value < 0 for value in no60_diffs)
    operational_invalid = [row for row in rows if not row.get("operationally_valid") or not row.get("candidate_validated") or not row.get("adopted")]
    if operational_invalid:
        classification = "E_EXPERIMENT_OPERATIONALLY_INVALID"
    elif ia_wins >= 6 and no60_ia_wins >= 4 and statistics.mean([row["ia_minus_top_mean_gain"] for row in by_state.values()]) > 0:
        classification = "A_INTERACTION_AWARE_ADVANCES_TO_BOUNDED_DYNAMIC_CONTINUATION"
    elif top_wins >= 6 and no60_top_wins >= 4 and statistics.mean([row["ia_minus_top_mean_gain"] for row in by_state.values()]) < 0:
        classification = "B_TOP_INDIVIDUAL_REMAINS_THE_STRONGER_R16_S4_TARGETING_BASELINE"
    elif ia_wins >= 2 and top_wins >= 2:
        classification = "C_TOP_AND_IA_SHOW_STATE_DEPENDENT_COMPLEMENTARY_VALUE"
    else:
        classification = "D_PRODUCTION_LIKE_TOP_VS_IA_RESULT_REMAINS_INCONCLUSIVE"
    summary = {
        "schema": "r16_top_vs_ia_qualification_analysis_v1",
        "primary_classification": classification,
        "cells_completed": sum(bool(row.get("operationally_valid")) for row in rows),
        "cells_invalid": sum(not bool(row.get("operationally_valid")) for row in rows),
        "cells_rerun": sum(bool((row.get("supervisor_event") or {}).get("rerun")) for row in rows),
        "all_eight_states": {"by_policy": by_policy, "by_state": by_state},
        "excluding_state_60": {
            "by_policy": {policy: policy_stats([row for row in no60 if row["policy"] == policy]) for policy in POLICIES},
            "ia_wins": no60_ia_wins,
            "top_wins": no60_top_wins,
        },
        "state_60_positive_control": by_state.get("60"),
        "trajectory_hypothesis": trajectory,
        "hint_smoke": smoke,
        "no_alternative_schedule_outcome_inferred": True,
    }
    write_atomic(root / "analysis" / "paired_results.json", {"schema": "r16_top_vs_ia_paired_results_v1", "rows": rows})
    write_atomic(root / "analysis" / "qualification_summary.json", summary)
    write_atomic(root / "analysis" / "hint_telemetry_audit.json", {
        "schema": "r16_hint_telemetry_audit_v1",
        "smoke_passed": smoke.get("passed", False),
        "identity_enabled": smoke.get("exact_identity_enabled_for_grid", False),
        "cells_with_exact_identity": sum(bool(((row.get("inner_probe_summaries") or [{}])[0].get("hint_telemetry") or {}).get("exact_identity")) for row in rows),
        "cells": len(rows),
        "actual_hint_values_changed": False,
        "no_hint_or_target_release_treatment": True,
    })
    with (root / "analysis" / "cell_results.csv").open("w", newline="", encoding="utf-8") as stream:
        fields = sorted({key for row in rows for key in row if key != "supervisor_event"})
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: json.dumps(value, sort_keys=True, default=str) if isinstance(value, (dict, list, tuple)) else value for key, value in row.items()})
    report_lines = [
        "# R16/S4 diverse-prestate eight-worker TOP versus IA qualification",
        "",
        f"Primary classification: **{classification}**.",
        "",
        "This is a production-like research qualification, not a fully independent state-aware generalization test: the eight states were selected from the already analyzed 70-state TOP trajectory. State 60 is a known TOP jackpot positive control, not an unseen state.",
        "",
        "## Contract and operational audit",
        "",
        "The grid used 8 states × 2 target policies × 3 clean repeats = 48 one-transition cells. Every cell used R16/S4, seed 101, eight CP-SAT workers, current incumbent-derived hints, a 300-second CP-SAT ceiling, one validation worker, a 180-second validation allowance, a 720-second parent wall, fresh source reset, unchanged Objective Semantics v2, strict full-model validation, and strict adoption.",
        f"Completed operationally valid cells: {summary['cells_completed']}; invalid cells: {summary['cells_invalid']}; rerun cells: {summary['cells_rerun']}.",
        "",
        "## All-state and state-60-excluded results",
        "",
        f"TOP distribution: {by_policy['top_individual']['gain']}; IA distribution: {by_policy['interaction_aware']['gain']}.",
        f"TOP probabilities ≥30/60/90: {by_policy['top_individual']['probability_gain_at_least_30']:.3f}/{by_policy['top_individual']['probability_gain_at_least_60']:.3f}/{by_policy['top_individual']['probability_gain_at_least_90']:.3f}.",
        f"IA probabilities ≥30/60/90: {by_policy['interaction_aware']['probability_gain_at_least_30']:.3f}/{by_policy['interaction_aware']['probability_gain_at_least_60']:.3f}/{by_policy['interaction_aware']['probability_gain_at_least_90']:.3f}.",
        f"State-level median wins: IA {ia_wins}, TOP {top_wins}; excluding state 60: IA {no60_ia_wins}, TOP {no60_top_wins}.",
        "",
        "## Interpretation boundary",
        "",
        "All quality statements use returned candidates that passed the unchanged full-model authority. Scope differences, hint telemetry, and any shadow facts do not imply an alternative schedule outcome. No additional heavy experiment was launched after the grid.",
    ]
    (root / "report" / "study_report.md").write_text("\n".join(report_lines) + "\n", encoding="utf-8")
    return summary


def seal(root):
    root = Path(root)
    lines = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name in {"artifact_hashes.sha256", "SEALED"}:
            continue
        lines.append(f"{sha256_file(path)}  {path.relative_to(root).as_posix()}")
    manifest = root / "artifact_hashes.sha256"
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8")
    write_atomic(root / "SEALED", {
        "schema": "r16_top_vs_ia_qualification_seal_v1",
        "status": "complete",
        "artifact_hashes_sha256": sha256_file(manifest),
        "sealed_at_utc": utc_now(),
    })


def child_main(args):
    root = Path(args.study_root)
    output_root = Path(args.output_root)
    states = read_payload(root / "states" / "state_catalog.json")["states"]
    state = next(item for item in states if int(item["attempt_index"]) == args.state_attempt)
    data = read_durable_stage2_benchmark(REFERENCE_TARGET)["data"]
    try:
        row = run_cell(
            data, state, seed=SEED, policy=args.policy, output_root=output_root,
            search_seconds=args.search_seconds, validation_seconds=args.validation_seconds,
            run_label=f"repeat_{args.repeat:02d}_execution_{args.execution}",
            worker_count=WORKERS if args.repeat else 1,
            fixed_scope=None,
            parent_time_limit_seconds=args.parent_time_limit_seconds,
            collect_hint_vector_telemetry=True,
            collect_hint_identity_telemetry=args.exact_hints,
        )
        output = {
            "schema": CELL_SCHEMA,
            "output_protocol_complete": True,
            "row": row,
            "cell_contract": {
                "worker_count": WORKERS if args.repeat else 1,
                "validation_worker_count": VALIDATION_WORKERS,
                "seed": SEED,
                "search_seconds": args.search_seconds,
                "validation_seconds": args.validation_seconds,
                "parent_time_limit_seconds": args.parent_time_limit_seconds,
                "exact_hint_identity_telemetry": args.exact_hints,
            },
        }
        write_atomic(Path(root) / "worker_results" / f"attempt_{args.state_attempt:03d}_{args.policy}_repeat_{args.repeat:02d}__execution_{args.execution}.json", output)
        return 0
    except Exception as error:
        write_atomic(Path(root) / "worker_results" / f"attempt_{args.state_attempt:03d}_{args.policy}_repeat_{args.repeat:02d}__execution_{args.execution}.error.json", {
            "schema": CELL_SCHEMA,
            "output_protocol_complete": False,
            "error_type": type(error).__name__,
            "error": str(error),
            "traceback": traceback.format_exc(),
        })
        return 1


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--lineage-id")
    parser.add_argument("--confirm-lineage-id")
    parser.add_argument("--parent", type=Path, default=OUTER_PARENT)
    parser.add_argument("--run-grid", action="store_true")
    parser.add_argument("--child-cell", action="store_true")
    parser.add_argument("--study-root", type=Path)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--state-attempt", type=int)
    parser.add_argument("--policy", choices=POLICIES)
    parser.add_argument("--repeat", type=int)
    parser.add_argument("--execution", type=int, default=1)
    parser.add_argument("--search-seconds", type=float, default=SEARCH_SECONDS)
    parser.add_argument("--validation-seconds", type=float, default=VALIDATION_SECONDS)
    parser.add_argument("--parent-time-limit-seconds", type=float, default=PARENT_SECONDS)
    parser.add_argument("--exact-hints", action="store_true")
    parser.add_argument("--no-exact-hints", action="store_true")
    args = parser.parse_args(argv)
    if args.child_cell:
        return child_main(args)
    if not args.run_grid:
        parser.error("use --run-grid")
    if not args.lineage_id or args.confirm_lineage_id != args.lineage_id:
        parser.error("--lineage-id and matching --confirm-lineage-id are required")
    root = create_root(args.parent, args.lineage_id)
    data, states = prepare(root)
    smoke = run_smoke(root, data, states)
    exact_hints = bool(smoke.get("exact_identity_enabled_for_grid"))
    write_atomic(root / "smoke" / "grid_hint_telemetry_decision.json", {
        "schema": "r16_hint_telemetry_grid_decision_v1",
        "smoke_passed": smoke.get("passed", False),
        "collect_hint_identity_telemetry": exact_hints,
        "reason": "smoke_gate_passed" if exact_hints else "smoke_gate_failed_or_incomplete; ordinary compact telemetry retained",
    })
    rows = run_grid(root, data, states, exact_hints)
    trajectory = read_payload(root / "trajectory_hypothesis_v1.json")
    summary = analyze(root, rows, trajectory, smoke)
    write_atomic(root / "progress.json", {"schema": "r16_qualification_progress_v1", "status": "complete", "completed_cells": len(rows), "expected_cells": 48, "primary_classification": summary["primary_classification"], "updated_at_utc": utc_now()})
    seal(root)
    print(json.dumps({"root": str(root), "status": "complete", "cells": len(rows), "classification": summary["primary_classification"], "smoke_passed": smoke.get("passed", False)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
