"""Research-only runner for the frozen R16/S4 within-scope semantics screen.

Importing this module never launches a solver.  Target-scale execution requires
the explicit ``--run-grid --confirm-experiment-id ... --confirm-cell-count 72``
command.  Every cell runs in a fresh child process and resets to its immutable
source checkpoint.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import ctypes
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
import traceback
import uuid

try:
    import psutil
except ImportError:  # pragma: no cover - preflight reports this explicitly
    psutil = None

from .student_assignment.calibration_supervisor import (
    _cleanup_observed_descendants,
    process_tree_snapshot,
    terminate_process_tree,
)
from .student_assignment.core import (
    run_student_assignment_source_decision_validation_diagnostic,
    run_substantive_soft_tier_probe,
)
from .student_assignment.quality import evaluate_student_assignment_quality
from .student_assignment.runtime import semantic_student_assignment_input_fingerprint
from .student_assignment.search_experiments import source_decision_fingerprint
from .student_assignment.stage2_benchmark import (
    read_diagnostic_branch_checkpoint,
    read_durable_stage2_benchmark,
)


RUNNER_SCHEMA = "r16_fixed_scope_search_semantics_runner_v1"
CELL_SCHEMA = "r16_fixed_scope_search_semantics_cell_v1"
RESULT_SCHEMA = "r16_fixed_scope_search_semantics_result_v1"
RESEARCH_PARENT = Path(r"C:\Users\desou\research_runs")
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def awake_time_seconds():
    """Return a clock that excludes host suspension where Windows exposes it."""

    if os.name == "nt":
        ticks = ctypes.c_ulonglong()
        query = ctypes.windll.kernel32.QueryUnbiasedInterruptTime
        if query(ctypes.byref(ticks)):
            return ticks.value / 10_000_000.0
    return time.monotonic()


def json_safe(value):
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [json_safe(item) for item in value]
    if hasattr(value, "to_dict"):
        return json_safe(value.to_dict())
    if hasattr(value, "__dict__"):
        return json_safe(asdict(value) if hasattr(value, "__dataclass_fields__") else vars(value))
    return value


def json_bytes(payload):
    return json.dumps(
        json_safe(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")


def atomic_write(path, payload, *, compressed=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = gzip.compress(json_bytes(payload), mtime=0) if compressed else json_bytes(payload)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    return sha256_bytes(raw)


def append_jsonl(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as stream:
        stream.write(json_bytes(payload) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())


def read_json(path):
    path = Path(path)
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            return json.load(stream)
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_bytes(raw):
    return hashlib.sha256(raw).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while block := stream.read(8 * 1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def canonical_scope(scope):
    return tuple(sorted({int(student_id) for student_id in scope}))


def scope_fingerprint(scope):
    return sha256_bytes(json_bytes(canonical_scope(scope)))


def load_contract(path):
    contract = read_json(path)
    validate_contract(contract)
    return contract


def contract_payload_fingerprint(contract):
    """Hash the frozen contract payload, excluding only its hash field.

    ``code_identity.prepared_from_git_head`` and the implementation file
    hashes are frozen provenance/identity inputs and therefore participate in
    this payload.  Runtime ``execution_git_head`` is deliberately never a
    contract field; it is written to a created lineage instead.
    """

    payload = dict(contract)
    payload.pop("contract_payload_fingerprint", None)
    return sha256_bytes(json_bytes(payload))


def expected_cell_order(contract):
    treatments = tuple(contract["treatments"])
    scopes = tuple(contract["source_cells"])
    repeats = int(contract["repeat_count"])
    rows = []
    ordinal = 0
    # Balanced deterministic rotation: every treatment occupies early and late
    # positions across scopes/repeats without changing any scientific factor.
    for repeat in range(1, repeats + 1):
        for scope_index, scope in enumerate(scopes):
            offset = (scope_index + repeat - 1) % len(treatments)
            order = treatments[offset:] + treatments[:offset]
            if repeat % 2 == 0:
                order = tuple(reversed(order))
            for treatment in order:
                ordinal += 1
                rows.append({
                    "ordinal": ordinal,
                    "cell_id": f"cell_{ordinal:03d}_{scope['id']}_{treatment}_repeat_{repeat:02d}",
                    "source_cell_id": scope["id"],
                    "treatment": treatment,
                    "repeat": repeat,
                })
    return rows


def validate_contract(contract):
    if contract.get("schema") != "r16_fixed_scope_search_semantics_contract_v1":
        raise ValueError("unexpected fixed-scope contract schema")
    if tuple(contract.get("treatments", ())) != (
        "control_first_qualifying",
        "minimum_coordination",
        "iterative_strict_bound_refinement",
        "direct_exact_v2_optimization",
    ):
        raise ValueError("the frozen four-treatment contract changed")
    if len(contract.get("source_cells", ())) != 6:
        raise ValueError("the frozen contract requires six source/scope cells")
    if int(contract.get("repeat_count", 0)) != 3:
        raise ValueError("the frozen contract requires three repeats")
    if int(contract.get("total_cell_count", 0)) != 72:
        raise ValueError("the frozen contract requires 72 cells")
    if contract_payload_fingerprint(contract) != contract.get(
        "contract_payload_fingerprint"
    ):
        raise ValueError("frozen contract payload fingerprint mismatch")
    code_identity = contract.get("code_identity") or {}
    if not code_identity.get("prepared_from_git_head"):
        raise ValueError("frozen preparation-base Git provenance is missing")
    if "git_head" in code_identity:
        raise ValueError("self-referential frozen git_head is not supported")
    if not code_identity.get("fingerprinted_files"):
        raise ValueError("experiment implementation fingerprint file set is empty")
    if not code_identity.get("implementation_fingerprint"):
        raise ValueError("experiment implementation fingerprint is missing")
    if contract.get("objective_semantics_version") != "v2":
        raise ValueError("the frozen contract requires Objective Semantics v2")
    if set(contract.get("objective_importance_scores", {}).values()) != {6}:
        raise ValueError("the frozen contract requires the balanced importance profile")
    expected_execution = {
        "seed": 101,
        "search_workers": 8,
        "validation_workers": 1,
        "search_budget_seconds": 300,
        "validation_allowance_seconds": 180,
        "neighborhood_radius": 16,
        "max_changed_students": 4,
        "max_changed_source_decisions": 16,
    }
    for key, expected in expected_execution.items():
        if contract.get(key) != expected:
            raise ValueError(f"frozen execution field changed: {key}")
    for source in contract["source_cells"]:
        scope = canonical_scope(source["scope"])
        if len(scope) != 4 or list(scope) != list(source["scope"]):
            raise ValueError(f"source scope is not canonical R16/S4: {source['id']}")
        if scope_fingerprint(scope) != source["scope_fingerprint"]:
            raise ValueError(f"scope fingerprint mismatch: {source['id']}")
        if not source.get("materialized_source_decision_fingerprint"):
            raise ValueError(f"materialized source fingerprint missing: {source['id']}")
    if float(contract.get("decision_criteria", {}).get(
        "practical_gain_points", -1
    )) != 12:
        raise ValueError("the preregistered practical threshold changed")
    if expected_cell_order(contract) != contract.get("cell_order"):
        raise ValueError("persisted cell order does not match deterministic contract")
    return True


def verify_sealed_lineage(root):
    """Verify a historical lineage without importing a study-specific reader."""

    root = Path(root)
    manifest = root / "artifact_hashes.sha256"
    seal = root / "SEALED"
    if not manifest.exists() or not seal.exists():
        return {"verified": False, "reason": "missing_manifest_or_seal"}
    try:
        seal_payload = json.loads(seal.read_text(encoding="utf-8"))
        expected_manifest_hash = seal_payload.get("artifact_hashes_sha256")
    except (json.JSONDecodeError, OSError):
        expected_manifest_hash = seal.read_text(encoding="utf-8").strip()
    missing = []
    mismatched = []
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        expected, relative = line.split("  ", 1)
        target = root / relative
        if not target.exists():
            missing.append(relative)
        elif sha256_file(target) != expected:
            mismatched.append(relative)
    actual_manifest_hash = sha256_file(manifest)
    return {
        "verified": bool(
            expected_manifest_hash == actual_manifest_hash
            and not missing
            and not mismatched
        ),
        "manifest_sha256": actual_manifest_hash,
        "expected_manifest_sha256": expected_manifest_hash,
        "missing_files": missing,
        "mismatched_files": mismatched,
    }


def implementation_fingerprint(contract):
    rows = []
    for relative in contract["code_identity"]["fingerprinted_files"]:
        path = REPOSITORY_ROOT / relative
        rows.append((relative, sha256_file(path)))
    return sha256_bytes(json_bytes(rows)), rows


def code_identity_is_authorized(
    *,
    ancestry_ok,
    worktree_clean,
    implementation_fingerprint_matches,
):
    """Return the stable code-identity launch authority.

    A committed contract cannot require the repository's current HEAD to be
    equal to a value recorded inside that same contract.  Ancestry establishes
    preparation provenance; the exact implementation fingerprint and clean
    worktree establish the executable code identity.
    """

    return bool(
        ancestry_ok
        and worktree_clean
        and implementation_fingerprint_matches
    )


def code_identity_facts(contract):
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPOSITORY_ROOT,
        capture_output=True, text=True, check=False,
    ).stdout.strip()
    status = subprocess.run(
        ["git", "status", "--short"], cwd=REPOSITORY_ROOT,
        capture_output=True, text=True, check=False,
    ).stdout
    prepared_from_git_head = contract["code_identity"]["prepared_from_git_head"]
    ancestry = subprocess.run(
        ["git", "merge-base", "--is-ancestor", prepared_from_git_head, head],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    fingerprint, rows = implementation_fingerprint(contract)
    worktree_clean = not status.strip()
    implementation_matches = (
        fingerprint == contract["code_identity"]["implementation_fingerprint"]
    )
    return {
        "execution_git_head": head,
        "prepared_from_git_head": prepared_from_git_head,
        "preparation_base_is_ancestor": ancestry.returncode == 0,
        "git_status": status,
        "worktree_clean": worktree_clean,
        "implementation_fingerprint": fingerprint,
        "file_hashes": dict(rows),
        "expected_prepared_from_git_head": prepared_from_git_head,
        "expected_implementation_fingerprint": contract["code_identity"][
            "implementation_fingerprint"
        ],
        "matches": code_identity_is_authorized(
            ancestry_ok=ancestry.returncode == 0,
            worktree_clean=worktree_clean,
            implementation_fingerprint_matches=implementation_matches,
        ),
    }


def execution_provenance(contract, identity):
    """Build lineage-only execution identity without mutating the contract."""

    return {
        "schema": "r16_fixed_scope_execution_provenance_v1",
        "contract_payload_fingerprint": contract["contract_payload_fingerprint"],
        "prepared_from_git_head": contract["code_identity"][
            "prepared_from_git_head"
        ],
        "execution_git_head": identity["execution_git_head"],
        "implementation_fingerprint": identity["implementation_fingerprint"],
        "worktree_clean": identity["worktree_clean"],
        "captured_at_utc": utc_now(),
    }


def competing_processes():
    if psutil is None:
        return [{"reason": "psutil_unavailable"}]
    current = os.getpid()
    matches = []
    needles = (
        "benchmark_r16_fixed_scope_search_semantics",
        "benchmark_fixed_phase_study",
        "benchmark_r16_ia_three_hour",
        "celery worker",
    )
    for process in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            pid = int(process.info["pid"])
            name = str(process.info.get("name") or "").lower()
            command = " ".join(process.info.get("cmdline") or [])
        except (psutil.Error, OSError, ValueError, TypeError):
            continue
        executable_is_relevant = "python" in name or "celery" in name
        if (
            pid != current
            and executable_is_relevant
            and any(needle in command for needle in needles)
        ):
            matches.append({"pid": pid, "command": command})
    return matches


def verify_source_cells(contract, data=None):
    rows = []
    for source in contract["source_cells"]:
        path = Path(source["source_path"])
        row = {
            "id": source["id"],
            "path": str(path),
            "exists": path.exists(),
            "sha256": sha256_file(path) if path.exists() else None,
            "expected_sha256": source["source_sha256"],
            "scope_fingerprint": scope_fingerprint(source["scope"]),
        }
        if path.exists() and data is not None:
            stored = read_json(path)
            checkpoint = read_diagnostic_branch_checkpoint(path, data=data)
            decisions = tuple(checkpoint["source_decisions"])
            row["stored_source_decision_fingerprint"] = stored.get(
                "source_decision_fingerprint"
            )
            row["expected_stored_source_decision_fingerprint"] = source[
                "source_decision_fingerprint"
            ]
            row["materialized_source_decision_fingerprint"] = (
                source_decision_fingerprint(decisions)
            )
            row["expected_materialized_source_decision_fingerprint"] = source[
                "materialized_source_decision_fingerprint"
            ]
            row["substantive_value"] = checkpoint.get("quality", {}).get(
                "weighted_substantive_value"
            )
            row["objective_semantics_version"] = checkpoint.get(
                "objective_semantics_version"
            )
            row["objective_importance_scores"] = checkpoint.get(
                "objective_importance_scores"
            )
            row["counts"] = checkpoint.get("counts")
            row["source_validation"] = checkpoint.get("validation")
        row["matches"] = (
            row["exists"]
            and row["sha256"] == row["expected_sha256"]
            and row["scope_fingerprint"] == source["scope_fingerprint"]
            and (
                data is None
                or row.get("stored_source_decision_fingerprint")
                == source["source_decision_fingerprint"]
            )
            and (
                data is None
                or row.get("materialized_source_decision_fingerprint")
                == source["materialized_source_decision_fingerprint"]
            )
            and (
                data is None
                or float(row.get("substantive_value"))
                == float(source["substantive_value"])
            )
            and (
                data is None
                or row.get("objective_semantics_version")
                == contract["objective_semantics_version"]
            )
            and (
                data is None
                or row.get("objective_importance_scores")
                == contract["objective_importance_scores"]
            )
            and (
                data is None
                or row.get("counts") == contract["expected_source_counts"]
            )
            and (
                data is None
                or bool((row.get("source_validation") or {}).get("full_model_validation"))
            )
        )
        rows.append(row)
    return rows


def preflight(contract_path, *, run_parity_tests=True):
    contract = load_contract(contract_path)
    benchmark = read_durable_stage2_benchmark(
        REPOSITORY_ROOT / contract["input_fixture_path"]
    )
    data = benchmark["data"]
    sources = verify_source_cells(contract, data=data)
    seals = {
        root: verify_sealed_lineage(root)
        for root in contract["historical_source_lineages"]
    }
    available = (
        int(psutil.virtual_memory().available) if psutil is not None else None
    )
    conflicts = competing_processes()
    parity = {"run": False, "returncode": None}
    if run_parity_tests:
        command = [
            sys.executable,
            "-m",
            "pytest",
            "scheduling_engine/tests/test_within_scope_search.py",
            "scheduling_engine/tests/test_fixed_scope_search_runner.py",
            "scheduling_engine/tests/test_fixed_scope_search_analysis.py",
            "-q",
        ]
        completed = subprocess.run(
            command,
            cwd=REPOSITORY_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        parity = {
            "run": True,
            "command": command,
            "returncode": completed.returncode,
            "stdout": completed.stdout[-4000:],
            "stderr": completed.stderr[-4000:],
        }
    identity = code_identity_facts(contract)
    facts = {
        "schema": "r16_fixed_scope_preflight_v1",
        "created_at_utc": utc_now(),
        "contract_path": str(Path(contract_path).resolve()),
        "experiment_id": contract["experiment_id"],
        "contract_payload_fingerprint": contract["contract_payload_fingerprint"],
        "input_fingerprint": semantic_student_assignment_input_fingerprint(data),
        "expected_input_fingerprint": contract["input_fingerprint"],
        "source_cells": sources,
        "historical_source_seals": seals,
        "model_fingerprint": contract["model_fingerprint"],
        "code_identity": identity,
        "available_memory_bytes": available,
        "minimum_available_memory_bytes": contract["resource_contract"][
            "preflight_minimum_available_memory_bytes"
        ],
        "competing_processes": conflicts,
        "parity_tests": parity,
        "target_scale_cells_executed": 0,
    }
    facts["passed"] = bool(
        facts["input_fingerprint"] == facts["expected_input_fingerprint"]
        and all(row["matches"] for row in sources)
        and all(row["verified"] for row in seals.values())
        and identity["matches"]
        and available is not None
        and available >= facts["minimum_available_memory_bytes"]
        and not conflicts
        and (not run_parity_tests or parity["returncode"] == 0)
    )
    return facts


def create_lineage(contract, parent=RESEARCH_PARENT, lineage_id=None):
    lineage_id = lineage_id or (
        f"v2_r16_fixed_scope_search_semantics_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:8]}"
    )
    parent = Path(parent).resolve()
    if not lineage_id.startswith("v2_r16_fixed_scope_search_semantics_"):
        raise ValueError("lineage id must use the fixed-scope experiment prefix")
    root = (parent / lineage_id).resolve()
    if root.parent != parent:
        raise ValueError("lineage must be a direct child of the research parent")
    identity = code_identity_facts(contract)
    if not identity["matches"]:
        raise RuntimeError("code identity changed after preflight")
    root.mkdir(parents=True, exist_ok=False)
    with (root / "lineage.lock").open("x", encoding="utf-8") as stream:
        stream.write(contract["experiment_id"] + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    for name in ("source", "cells", "analysis", "report", "logs"):
        (root / name).mkdir(exist_ok=False)
    atomic_write(root / "frozen_contract.json", contract)
    atomic_write(root / "execution_provenance.json", execution_provenance(contract, identity))
    for source in contract["source_cells"]:
        target = root / "source" / f"{source['id']}.json.gz"
        shutil.copyfile(source["source_path"], target)
        if sha256_file(target) != source["source_sha256"]:
            raise RuntimeError(f"copied source hash mismatch: {source['id']}")
    atomic_write(root / "cell_order.json", {
        "schema": "r16_fixed_scope_cell_order_v1",
        "rows": contract["cell_order"],
    })
    atomic_write(root / "runner_state.json", {
        "schema": "r16_fixed_scope_runner_state_v1",
        "experiment_id": contract["experiment_id"],
        "status": "created",
        "completed_valid_cells": [],
        "invalid_executions": [],
        "created_at_utc": utc_now(),
    })
    return root


def compact_quality(data, result):
    report = evaluate_student_assignment_quality(
        data,
        assignments=result.assignments,
        commitment_assignments=result.commitment_assignments,
        solver_objective_components=result.objective_components,
        include_entity_metrics=False,
    )
    objective = report.get("objective_semantics", {})
    components = objective.get("components", {})
    return {
        "objective_semantics_version": objective.get("version"),
        "components": components,
        "weighted_substantive_value": float(sum(
            float(row.get("weighted_normalized_contribution", 0) or 0)
            for row in components.values()
        )),
        "assignment_count": len(result.assignments),
        "special_commitment_count": len(result.commitment_assignments),
        "unmet_request_count": len(result.unmet_requests),
    }


def component_improvements(before, after):
    return {
        name: (
            float(before["components"][name]["weighted_normalized_contribution"])
            - float(after["components"][name]["weighted_normalized_contribution"])
        )
        for name in before["components"]
    }


def candidate_is_authoritative(
    *, validation_status, unmet_request_count, stage2_facts,
    expected_candidate_fingerprint, validated_candidate_fingerprint,
    before_value, after_value,
):
    """Apply the unchanged full-model-validation and strict-gain boundary."""

    return bool(
        stage2_facts.get("alternate_seed_validated")
        and validation_status == "complete"
        and int(unmet_request_count) == 0
        and validated_candidate_fingerprint == expected_candidate_fingerprint
        and after_value is not None
        and float(after_value) < float(before_value)
    )


def treatment_probe_semantics(treatment):
    return {
        "control_first_qualifying": "first_qualifying",
        "minimum_coordination": "minimum_coordination",
        "iterative_strict_bound_refinement": "iterative_strict_bound_refinement",
        "direct_exact_v2_optimization": "direct_exact_v2_optimization",
    }[treatment]


def _phase_writer(path):
    def write(phase, event="completed", **facts):
        atomic_write(path, {
            "schema": "r16_fixed_scope_cell_state_v1",
            "phase": str(phase),
            "event": str(event),
            "facts": facts,
            "updated_at_utc": utc_now(),
        })
    return write


def run_cell_worker(contract_path, lineage_root, cell_id, output_path, state_path):
    contract = load_contract(contract_path)
    cell = next(row for row in contract["cell_order"] if row["cell_id"] == cell_id)
    source_spec = next(
        row for row in contract["source_cells"]
        if row["id"] == cell["source_cell_id"]
    )
    output_path = Path(output_path)
    state_path = Path(state_path)
    update_phase = _phase_writer(state_path)
    started = time.monotonic()
    try:
        update_phase("input_and_source", "started")
        benchmark = read_durable_stage2_benchmark(
            REPOSITORY_ROOT / contract["input_fixture_path"]
        )
        data = benchmark["data"]
        source_path = Path(lineage_root) / "source" / f"{source_spec['id']}.json.gz"
        if sha256_file(source_path) != source_spec["source_sha256"]:
            raise RuntimeError("copied source hash mismatch")
        checkpoint = read_diagnostic_branch_checkpoint(source_path, data=data)
        source = tuple(checkpoint["source_decisions"])
        source_fp = source_decision_fingerprint(source)
        stored_source_fp = read_json(source_path).get("source_decision_fingerprint")
        if source_fp != source_spec["materialized_source_decision_fingerprint"]:
            raise RuntimeError("materialized source decision fingerprint mismatch")
        if stored_source_fp != source_spec["source_decision_fingerprint"]:
            raise RuntimeError("stored canonical source decision fingerprint mismatch")
        before_quality = checkpoint["quality"]
        update_phase("fixed_scope_search", "started")
        probe = run_substantive_soft_tier_probe(
            data,
            threshold=None,
            strict_improvement=True,
            time_limit_seconds=float(contract["search_budget_seconds"]),
            worker_count=int(contract["search_workers"]),
            neighborhood_radius=16,
            max_changed_students=4,
            selected_student_ids=tuple(source_spec["scope"]),
            search_semantics=treatment_probe_semantics(cell["treatment"]),
            alternate_source_decisions=source,
            mature_checkpoint_only=True,
            hard_feasibility_validation_time_limit_seconds=float(
                contract["source_validation_allowance_seconds"]
            ),
            hard_feasibility_validation_worker_count=1,
            cp_sat_random_seed=int(contract["seed"]),
            phase_callback=update_phase,
            collect_resource_telemetry=False,
            collect_hint_identity_telemetry=bool(
                contract["telemetry_contract"]["record_hint_fingerprint"]
            ),
        )
        if probe.probe_base_model_fingerprint != contract["model_fingerprint"]:
            raise RuntimeError("probe base-model fingerprint mismatch")
        candidate_quality = None
        validation = {
            "classification": "not_attempted_no_complete_candidate",
            "complete": False,
            "full_model_validated": False,
            "authoritative": False,
            "wall_seconds": 0.0,
        }
        validation_result = None
        if probe.complete_candidate_found:
            update_phase("candidate_validation", "started")
            validation_started = time.monotonic()
            validation_result = run_student_assignment_source_decision_validation_diagnostic(
                data,
                source_decisions=probe.candidate_source_decisions,
                time_limit_seconds=float(contract["validation_allowance_seconds"]),
                worker_count=int(contract["validation_workers"]),
                capture_final_source_decisions=True,
                collect_resource_telemetry=False,
            )
            validation_wall = time.monotonic() - validation_started
            stage2 = dict((validation_result.optimization_facts or {}).get("stage_2") or {})
            candidate_quality = compact_quality(data, validation_result)
            candidate_fp = source_decision_fingerprint(
                tuple(stage2.get("final_source_decisions") or ())
            )
            authoritative = candidate_is_authoritative(
                validation_status=validation_result.status,
                unmet_request_count=len(validation_result.unmet_requests),
                stage2_facts=stage2,
                expected_candidate_fingerprint=source_decision_fingerprint(
                    probe.candidate_source_decisions
                ),
                validated_candidate_fingerprint=candidate_fp,
                before_value=before_quality["weighted_substantive_value"],
                after_value=candidate_quality["weighted_substantive_value"],
            )
            full_model_validated = bool(
                stage2.get("alternate_seed_validated")
                and validation_result.status == "complete"
                and not validation_result.unmet_requests
                and candidate_fp == source_decision_fingerprint(
                    probe.candidate_source_decisions
                )
            )
            validation = {
                "classification": (
                    "validated" if authoritative
                    else "validated_not_authoritative" if full_model_validated
                    else stage2.get("alternate_seed_validation_classification")
                    or "validation_error"
                ),
                "complete": validation_result.status == "complete",
                "full_model_validated": full_model_validated,
                "authoritative": authoritative,
                "wall_seconds": validation_wall,
                "solver_outcome": validation_result.solver_outcome,
                "candidate_source_fingerprint": candidate_fp,
                "unmet_request_count": len(validation_result.unmet_requests),
            }
            update_phase("candidate_validation", "completed", **validation)

        before_value = float(before_quality["weighted_substantive_value"])
        after_value = (
            float(candidate_quality["weighted_substantive_value"])
            if candidate_quality is not None else None
        )
        authoritative_gain = (
            before_value - after_value
            if validation["authoritative"] and after_value is not None else 0.0
        )
        deltas = (
            component_improvements(before_quality, candidate_quality)
            if validation["authoritative"] else {}
        )
        hint_identity = dict(probe.hint_telemetry.get("exact_identity") or {})
        result = {
            "schema": RESULT_SCHEMA,
            "cell": cell,
            "source": {
                "path": str(source_path),
                "sha256": source_spec["source_sha256"],
                "source_decision_fingerprint": stored_source_fp,
                "materialized_source_decision_fingerprint": source_fp,
                "substantive_value": before_value,
            },
            "scope": list(canonical_scope(source_spec["scope"])),
            "scope_fingerprint": scope_fingerprint(source_spec["scope"]),
            "treatment": cell["treatment"],
            "repeat": cell["repeat"],
            "seed": contract["seed"],
            "search_workers": contract["search_workers"],
            "validation_workers": contract["validation_workers"],
            "model_fingerprint": probe.probe_base_model_fingerprint,
            "hint_contract": "complete_authoritative_incumbent_derived_unchanged",
            "hint_fingerprint": hint_identity.get("mapping_fingerprint"),
            "hint_fingerprint_unavailable_reason": (
                None if hint_identity.get("mapping_fingerprint")
                else "disabled_by_compact_telemetry_contract"
            ),
            "scope_aggregate": {
                "selected_student_count": len(source_spec["scope"]),
                "eligible_targeted_source_decision_count": (
                    probe.eligible_targeted_source_decision_count
                ),
                "neighborhood_radius": probe.neighborhood_radius,
                "effective_neighborhood_radius": probe.effective_neighborhood_radius,
                "min_changed_students": probe.min_changed_students,
                "max_changed_students": probe.max_changed_students,
                "min_changed_source_decisions": probe.min_changed_source_decisions,
                "max_changed_source_decisions": contract[
                    "max_changed_source_decisions"
                ],
            },
            "search": {
                "requested_budget_seconds": contract["search_budget_seconds"],
                "cumulative_native_solve_wall_seconds": probe.cumulative_native_solve_wall_seconds,
                "cumulative_external_solve_wall_seconds": probe.cumulative_external_solve_wall_seconds,
                "first_qualifying_latency_seconds": probe.first_qualifying_latency_seconds,
                "time_spent_after_first_candidate_seconds": (
                    max(
                        0.0,
                        probe.cumulative_external_solve_wall_seconds
                        - probe.first_qualifying_latency_seconds,
                    )
                    if probe.first_qualifying_latency_seconds is not None
                    else None
                ),
                "status": probe.status,
                "termination": probe.search_termination_classification,
                "branches": probe.branches,
                "conflicts": probe.conflicts,
                "solve_rounds": probe.solve_rounds,
            },
            "candidate": {
                "found": probe.complete_candidate_found,
                "first_substantive_value": probe.first_candidate_substantive_value,
                "final_substantive_value": probe.candidate_substantive_value,
                "first_gain": (
                    before_value - probe.first_candidate_substantive_value
                    if probe.first_candidate_substantive_value is not None else None
                ),
                "final_discovery_gain": (
                    before_value - probe.candidate_substantive_value
                    if probe.candidate_substantive_value is not None else None
                ),
                "fingerprint": (
                    source_decision_fingerprint(probe.candidate_source_decisions)
                    if probe.complete_candidate_found else None
                ),
                "changed_student_count": probe.changed_student_count,
                "changed_source_decision_count": probe.changed_source_decision_count,
                "changed_source_decisions": probe.source_decision_deltas,
                "best_objective": probe.candidate_substantive_value,
                "best_bound": probe.best_bound,
                "absolute_gap": probe.objective_absolute_gap,
                "relative_gap": probe.objective_relative_gap,
                "first_feasible_time_instrumented": False,
                "final_best_update_time_instrumented": False,
            },
            "quality": {
                "before": before_quality,
                "probe_seed_objective_semantics": probe.seed_quality_objective_semantics,
                "probe_candidate_objective_semantics": (
                    probe.candidate_quality_objective_semantics
                ),
                "after": candidate_quality,
                "component_improvements": deltas,
                "authoritative_gain": authoritative_gain,
            },
            "validation": validation,
            "total_wall_seconds": time.monotonic() - started,
            "production_behavior_changed": False,
            "target_policy_changed": False,
            "hint_behavior_changed": False,
            "objective_semantics_changed": False,
            "validation_authority_changed": False,
            "full_target_population_embedded": False,
        }
        raw = json_bytes(result)
        if len(raw) > int(contract["telemetry_contract"]["max_result_json_bytes"]):
            raise RuntimeError("compact cell result exceeds artifact-size contract")
        atomic_write(output_path, result)
        if probe.complete_candidate_found:
            atomic_write(
                output_path.with_name("candidate_source_decisions.json.gz"),
                {
                    "schema": "r16_fixed_scope_candidate_source_v1",
                    "cell_id": cell_id,
                    "source_decisions": probe.candidate_source_decisions,
                    "fingerprint": source_decision_fingerprint(
                        probe.candidate_source_decisions
                    ),
                },
                compressed=True,
            )
        update_phase("complete", "completed", authoritative=validation["authoritative"])
        return 0
    except Exception as error:
        atomic_write(output_path.with_name("worker_error.json"), {
            "schema": "r16_fixed_scope_worker_error_v1",
            "cell_id": cell_id,
            "error": f"{type(error).__name__}: {error}",
            "traceback": traceback.format_exc(),
            "created_at_utc": utc_now(),
        })
        update_phase("failed", "completed", error=f"{type(error).__name__}: {error}")
        return 1


def completed_valid_result(path, cell):
    path = Path(path)
    if not path.exists():
        return False
    try:
        payload = read_json(path)
    except (OSError, ValueError, TypeError):
        return False
    return bool(
        payload.get("schema") == RESULT_SCHEMA
        and payload.get("cell") == cell
        and payload.get("scope_fingerprint")
        and payload.get("source", {}).get("sha256")
        and payload.get("source", {}).get("source_decision_fingerprint")
        and payload.get("model_fingerprint")
        and payload.get("treatment") == cell["treatment"]
        and payload.get("repeat") == cell["repeat"]
        and payload.get("validation", {}).get("classification") != "not_recorded"
        and payload.get("full_target_population_embedded") is False
    )


def supervise_cell(command, execution_dir, contract):
    execution_dir = Path(execution_dir)
    result_path = execution_dir / "result.json"
    state_path = execution_dir / "cell_state.json"
    resource_path = execution_dir / "resource_samples.jsonl"
    stdout_path = execution_dir / "worker.stdout.log"
    stderr_path = execution_dir / "worker.stderr.log"
    started_mono = time.monotonic()
    started_wall = time.time()
    started_awake = awake_time_seconds()
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        kwargs = {
            "cwd": REPOSITORY_ROOT,
            "stdin": subprocess.DEVNULL,
            "stdout": stdout,
            "stderr": stderr,
        }
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kwargs["start_new_session"] = True
        process = subprocess.Popen(command, **kwargs)
        observed_pids = {process.pid}
        sleep_contaminated = False
        resource_terminated = False
        deadline_terminated = False
        peak_rss = 0
        peak_uss = 0
        min_available = None
        while process.poll() is None:
            snapshot = process_tree_snapshot(process.pid)
            observed_pids.update(snapshot.get("pids") or ())
            now_mono, now_wall = time.monotonic(), time.time()
            awake_elapsed = awake_time_seconds() - started_awake
            wall_gap = (now_wall - started_wall) - awake_elapsed
            phase = {}
            if state_path.exists():
                try:
                    phase = read_json(state_path)
                except (OSError, ValueError):
                    phase = {}
            row = {
                "schema": "r16_fixed_scope_resource_sample_v1",
                "utc_timestamp": utc_now(),
                "elapsed_seconds": now_mono - started_mono,
                "wall_gap_seconds": wall_gap,
                "awake_elapsed_seconds": awake_elapsed,
                "phase": phase.get("phase"),
                **snapshot,
            }
            append_jsonl(resource_path, row)
            rss = int(snapshot.get("tree_rss_bytes") or 0)
            uss = int(snapshot.get("tree_uss_bytes") or 0)
            available = snapshot.get("system_available_memory_bytes")
            peak_rss = max(peak_rss, rss)
            peak_uss = max(peak_uss, uss)
            min_available = (
                int(available)
                if min_available is None and available is not None
                else min(min_available, int(available))
                if available is not None else min_available
            )
            if abs(wall_gap) > float(contract["resource_contract"]["sleep_gap_seconds"]):
                sleep_contaminated = True
                terminate_process_tree(process.pid, grace_seconds=5)
                break
            if (
                rss > int(contract["resource_contract"]["max_tree_rss_bytes"])
                or (
                    available is not None
                    and int(available) < int(contract["resource_contract"]["min_available_memory_bytes"])
                )
            ):
                resource_terminated = True
                terminate_process_tree(process.pid, grace_seconds=5)
                break
            if now_mono - started_mono > float(contract["cell_hard_wall_seconds"]):
                deadline_terminated = True
                terminate_process_tree(process.pid, grace_seconds=5)
                break
            time.sleep(float(contract["resource_contract"]["sample_interval_seconds"]))
        try:
            returncode = process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            terminate_process_tree(process.pid, grace_seconds=5)
            returncode = process.poll()
    cleanup = _cleanup_observed_descendants(
        process.pid,
        observed_pids,
        grace_seconds=2,
    )
    classification = (
        "host_sleep_contaminated" if sleep_contaminated
        else "resource_guard_terminated" if resource_terminated
        else "hard_deadline_terminated" if deadline_terminated
        else "completed" if returncode == 0 and result_path.exists()
        else "worker_crashed_or_protocol_error"
    )
    return {
        "schema": "r16_fixed_scope_supervisor_event_v1",
        "classification": classification,
        "returncode": returncode,
        "elapsed_seconds": time.monotonic() - started_mono,
        "sleep_contaminated": sleep_contaminated,
        "peak_tree_rss_bytes": peak_rss,
        "peak_tree_uss_bytes": peak_uss,
        "minimum_available_memory_bytes": min_available,
        "observed_pids": sorted(observed_pids),
        "cleanup": cleanup,
        "result_path": str(result_path),
    }


def next_execution_dir(root, cell_id):
    cell_root = Path(root) / "cells" / cell_id
    cell_root.mkdir(parents=True, exist_ok=True)
    indexes = [
        int(path.name.split("_")[-1])
        for path in cell_root.glob("execution_*")
        if path.is_dir() and path.name.split("_")[-1].isdigit()
    ]
    execution = cell_root / f"execution_{max(indexes, default=0) + 1:02d}"
    execution.mkdir(exist_ok=False)
    return execution


def run_grid(contract_path, root):
    contract_path = Path(contract_path).resolve()
    contract = load_contract(contract_path)
    root = Path(root).resolve()
    state_path = root / "runner_state.json"
    state = read_json(state_path)
    completed = set(state.get("completed_valid_cells", ()))
    invalid = list(state.get("invalid_executions", ()))
    for cell in contract["cell_order"]:
        valid_path = root / "cells" / cell["cell_id"] / "valid_result.json"
        if cell["cell_id"] in completed and completed_valid_result(valid_path, cell):
            append_jsonl(root / "logs" / "runner_events.jsonl", {
                "event": "valid_cell_skipped_on_resume",
                "cell_id": cell["cell_id"],
                "utc_timestamp": utc_now(),
            })
            continue
        execution_dir = next_execution_dir(root, cell["cell_id"])
        command = [
            sys.executable,
            "-m",
            "scheduling_engine.benchmark_r16_fixed_scope_search_semantics",
            "--contract",
            str(contract_path),
            "--cell-worker",
            "--lineage-root",
            str(root),
            "--cell-id",
            cell["cell_id"],
            "--worker-output",
            str(execution_dir / "result.json"),
            "--worker-state",
            str(execution_dir / "cell_state.json"),
        ]
        event = supervise_cell(command, execution_dir, contract)
        atomic_write(execution_dir / "supervisor_event.json", event)
        candidate = execution_dir / "result.json"
        if event["classification"] == "completed" and completed_valid_result(candidate, cell):
            payload = read_json(candidate)
            payload["resource_summary"] = {
                "peak_tree_rss_bytes": event["peak_tree_rss_bytes"],
                "peak_tree_uss_bytes": event["peak_tree_uss_bytes"],
                "minimum_available_memory_bytes": event[
                    "minimum_available_memory_bytes"
                ],
                "sample_path": str(
                    (execution_dir / "resource_samples.jsonl").relative_to(root)
                ),
            }
            payload["execution_provenance"] = {
                "execution": execution_dir.name,
                "supervisor_classification": event["classification"],
            }
            payload["artifact_sizes"] = {
                path.name: path.stat().st_size
                for path in execution_dir.iterdir()
                if path.is_file()
            }
            if len(json_bytes(payload)) > int(
                contract["telemetry_contract"]["max_result_json_bytes"]
            ):
                event["classification"] = "artifact_size_contract_failed"
                invalid.append({
                    "cell_id": cell["cell_id"],
                    "execution": execution_dir.name,
                    "classification": event["classification"],
                })
                atomic_write(execution_dir / "supervisor_event.json", event)
                atomic_write(state_path, {
                    **state,
                    "status": "running",
                    "completed_valid_cells": sorted(completed),
                    "invalid_executions": invalid,
                    "updated_at_utc": utc_now(),
                })
                continue
            atomic_write(valid_path, payload)
            atomic_write(valid_path.with_suffix(".sha256.json"), {
                "sha256": sha256_file(valid_path),
                "execution": execution_dir.name,
            })
            completed.add(cell["cell_id"])
        else:
            invalid.append({
                "cell_id": cell["cell_id"],
                "execution": execution_dir.name,
                "classification": event["classification"],
            })
        atomic_write(state_path, {
            **state,
            "status": "running" if len(completed) < contract["total_cell_count"] else "grid_complete",
            "completed_valid_cells": sorted(completed),
            "invalid_executions": invalid,
            "updated_at_utc": utc_now(),
        })
    return len(completed)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--contract", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preflight", action="store_true")
    mode.add_argument("--run-grid", action="store_true")
    mode.add_argument("--resume", type=Path)
    mode.add_argument("--cell-worker", action="store_true")
    parser.add_argument("--skip-parity-tests", action="store_true")
    parser.add_argument("--parent", type=Path, default=RESEARCH_PARENT)
    parser.add_argument("--lineage-id")
    parser.add_argument("--lineage-root", type=Path)
    parser.add_argument("--cell-id")
    parser.add_argument("--worker-output", type=Path)
    parser.add_argument("--worker-state", type=Path)
    parser.add_argument("--confirm-experiment-id")
    parser.add_argument("--confirm-cell-count", type=int)
    args = parser.parse_args(argv)
    contract = load_contract(args.contract)
    if args.preflight:
        facts = preflight(
            args.contract,
            run_parity_tests=not args.skip_parity_tests,
        )
        print(json.dumps(facts, indent=2, sort_keys=True))
        return 0 if facts["passed"] else 1
    if args.cell_worker:
        if not all((args.lineage_root, args.cell_id, args.worker_output, args.worker_state)):
            parser.error("cell worker requires lineage, cell id, output, and state")
        return run_cell_worker(
            args.contract,
            args.lineage_root,
            args.cell_id,
            args.worker_output,
            args.worker_state,
        )
    if (
        args.confirm_experiment_id != contract["experiment_id"]
        or args.confirm_cell_count != contract["total_cell_count"]
    ):
        raise RuntimeError(
            "target grid requires exact experiment-id and 72-cell confirmation"
        )
    if args.run_grid:
        facts = preflight(args.contract, run_parity_tests=True)
        if not facts["passed"]:
            raise RuntimeError(f"preflight failed: {facts}")
        root = create_lineage(contract, parent=args.parent, lineage_id=args.lineage_id)
        atomic_write(root / "preflight.json", facts)
        completed = run_grid(args.contract, root)
        print(json.dumps({"lineage": str(root), "completed_valid_cells": completed}))
        return 0 if completed == contract["total_cell_count"] else 2
    root = args.resume.resolve()
    expected_parent = RESEARCH_PARENT.resolve()
    if (
        root.parent != expected_parent
        or not root.name.startswith("v2_r16_fixed_scope_search_semantics_")
    ):
        raise RuntimeError("resume lineage is outside the fixed research namespace")
    if not (root / "frozen_contract.json").exists():
        raise RuntimeError("resume lineage is missing its frozen contract")
    if read_json(root / "frozen_contract.json") != contract:
        raise RuntimeError("resume contract differs from frozen lineage contract")
    completed = run_grid(args.contract, root)
    print(json.dumps({"lineage": str(root), "completed_valid_cells": completed}))
    return 0 if completed == contract["total_cell_count"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
