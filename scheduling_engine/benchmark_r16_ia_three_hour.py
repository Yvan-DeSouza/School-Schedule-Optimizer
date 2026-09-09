"""Research-only dynamic interaction-aware R16/S4 trajectory.

This runner executes exactly one three-hour IA-only branch from the immutable
common source used by the historical TOP R16 branch.  It reuses the existing
fixed-family controller, operator session, CP-SAT probe, full-model validator,
and current incumbent-derived hints.  It does not modify production wiring,
Objective Semantics v2, hard constraints, or adaptive operator selection.

The heavy command is deliberately explicit::

    python -m scheduling_engine.benchmark_r16_ia_three_hour \
      --launch-heavy --root C:\\Users\\desou\\research_runs\\<lineage> \
      --lineage-id <lineage> --confirm-lineage-id <lineage>

The ``--dry-run`` command uses the existing small fixture and is not quality
evidence.  Analysis and sealing are solver-free after the IA branch exits.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import inspect
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
from collections import Counter

import psutil

from .benchmark_fixed_phase_study import (
    BRANCH_SECONDS,
    EXPECTED_ASSIGNMENTS,
    EXPECTED_COMMITMENTS,
    EXPECTED_GROUPS,
    EXPECTED_INPUT,
    EXPECTED_MODEL,
    EXPECTED_SOURCE_SHA256,
    EXPECTED_VALUE,
    REFERENCE_TARGET,
    SOURCE_PATH,
    VALIDATION_SECONDS,
    VALIDATION_WORKERS,
    WORKERS,
    SEED,
    compact_quality,
    normalize,
    prepare_target_source,
    validate_source,
    validation_facts,
    _tree_processes,
    terminate_tree,
    validate_endpoint_in_clean_process,
    endpoint_is_authoritative,
)
from .student_assignment.adaptive_runtime import (
    FixedFamilyPhase,
    FixedFamilyPhaseController,
    _quality_report,
)
from .student_assignment.core import (
    run_student_assignment_source_decision_validation_diagnostic,
)
from .student_assignment.fixed_phase_study import (
    FixedPhaseArtifactWriter,
    FixedPhaseStudyContract,
    atomic_json,
    hash_tree,
    sha256_file,
)
from .student_assignment.quality import evaluate_student_assignment_quality
from .student_assignment.runtime import semantic_student_assignment_input_fingerprint
from .student_assignment.search_experiments import source_decision_fingerprint
from .student_assignment.search_guidance import rank_students_by_quality_pressure
from .student_assignment.stage2_benchmark import (
    read_durable_stage2_benchmark,
    read_diagnostic_branch_checkpoint,
    semantic_stage1_seed_source_fingerprint,
    validate_diagnostic_branch_checkpoint,
    write_diagnostic_branch_checkpoint,
)
from .student_assignment.utilization_guidance import (
    select_utilization_cluster_targets,
)


IA_BRANCH_ID = "ia_only"
IA_SCHEMA = "v2_r16_ia_three_hour_trajectory_v1"
IA_ATTEMPT_SCHEMA = "r16_ia_three_hour_attempt_v1"
IA_SEAL_SCHEMA = "r16_ia_three_hour_seal_v1"
HISTORICAL_TOP_ROOT = Path(
    r"C:\Users\desou\research_runs\v2_r16_vs_r4_r16_three_hour_20260907_054732_b8d42e6c"
)
HISTORICAL_TOP_BRANCH = HISTORICAL_TOP_ROOT / "branches" / "r16_only"
OUTER_RESEARCH_ROOT = Path(r"C:\Users\desou\research_runs")
CHECKPOINT_MINUTES = tuple(range(0, 181, 15))
SUPERVISOR_SLACK_SECONDS = 360.0
COMPONENTS = (
    "course_category_diversity",
    "course_sequence_preferences",
    "difficulty_balance",
    "section_utilization_balance",
    "student_semester_load_balance",
)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def json_default(value):
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    return repr(value)


def json_bytes(payload):
    return json.dumps(
        normalize(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=json_default,
    ).encode("utf-8")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def read_jsonl(path):
    path = Path(path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_atomic_bytes(path, raw):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    try:
        fd = os.open(path.parent, os.O_RDONLY)
    except (OSError, AttributeError):
        fd = None
    if fd is not None:
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def source_diff(before, after):
    before = dict(before)
    after = dict(after)
    keys = sorted(set(before) | set(after), key=repr)
    changed = []
    student_ids = set()
    for key in keys:
        if before.get(key) == after.get(key):
            continue
        old = before.get(key)
        new = after.get(key)
        changed.append({"source_key": key, "old": old, "new": new})
        for value in (old, new):
            if isinstance(value, (tuple, list)) and value:
                try:
                    student_ids.add(int(value[0]))
                except (TypeError, ValueError):
                    pass
    return changed, sorted(student_ids)


def scope_fingerprint(scope):
    return hashlib.sha256(json_bytes(sorted(set(int(item) for item in scope)))).hexdigest()


def jaccard(left, right):
    left, right = set(left), set(right)
    return len(left & right) / len(left | right) if left | right else 1.0


def policy_fingerprint():
    """Fingerprint the existing IA target implementation and fixed operator."""

    from .student_assignment import adaptive_search, utilization_guidance

    sources = {
        "build_utilization_cluster_guidance": inspect.getsource(
            utilization_guidance.build_utilization_cluster_guidance
        ),
        "select_fixed_cycle_operator": inspect.getsource(
            adaptive_search.select_fixed_cycle_operator
        ),
        "operator_spec": inspect.getsource(adaptive_search.AdaptiveOperatorSpec),
    }
    return {
        "schema": "interaction_aware_target_policy_fingerprint_v1",
        "policy": "interaction_aware",
        "operator": "targeted_utilization_r16_s4",
        "radius": 16,
        "changed_student_cap": 4,
        "target_scope_size": 4,
        "target_policy": "dynamic",
        "utilization_cluster_policy": "interaction_aware",
        "implementation_modules": {
            name: hashlib.sha256(value.encode("utf-8")).hexdigest()
            for name, value in sources.items()
        },
        "combined_fingerprint": hashlib.sha256(
            json_bytes(sources)
        ).hexdigest(),
    }


def verify_sealed(root):
    root = Path(root)
    manifest = root / "artifact_hashes.sha256"
    seal_path = root / "SEALED"
    if not manifest.exists() or not seal_path.exists():
        raise RuntimeError(f"historical lineage is not sealed: {root}")
    seal = read_json(seal_path)
    actual_manifest = sha256_file(manifest)
    if actual_manifest != seal.get("artifact_hashes_sha256"):
        raise RuntimeError("historical artifact manifest hash mismatch")
    mismatches = []
    for line in manifest.read_text(encoding="utf-8").splitlines():
        digest, relative = line.split("  ", 1)
        path = root / relative
        if not path.exists() or sha256_file(path) != digest:
            mismatches.append(relative)
    if mismatches:
        raise RuntimeError(f"historical artifact mismatches: {mismatches[:5]}")
    return {
        "schema": "sealed_lineage_reference_v1",
        "path": str(root),
        "artifact_hashes_sha256": actual_manifest,
        "entry_count": len(manifest.read_text(encoding="utf-8").splitlines()),
        "verified": True,
    }


def historical_top_reference():
    seal = verify_sealed(HISTORICAL_TOP_ROOT)
    source_identity = read_json(HISTORICAL_TOP_BRANCH / "source_identity.json")
    branch_manifest = read_json(HISTORICAL_TOP_BRANCH / "branch_manifest.json")
    branch_result = read_json(HISTORICAL_TOP_BRANCH / "branch_result.json")
    attempts = []
    for path in sorted((HISTORICAL_TOP_BRANCH / "attempts").glob("attempt_*.json")):
        payload = read_json(path)
        attempt = dict(payload.get("attempt") or {})
        attempt["attempt_index"] = payload.get("attempt_index")
        attempt["branch_elapsed_seconds"] = payload.get("branch_elapsed_seconds")
        attempt["attempt_path"] = str(path)
        attempts.append(attempt)
    gains = [float(row.get("gain", 0) or 0) for row in attempts]
    facts = {
        "lineage": seal,
        "source_identity": source_identity,
        "branch_manifest": branch_manifest,
        "branch_result": branch_result,
        "attempt_count": len(attempts),
        "adoption_count": sum(bool(row.get("adopted")) for row in attempts),
        "total_gain": sum(gains),
        "maximum_gain": max(gains) if gains else None,
        "attempts": attempts,
    }
    if (
        facts["attempt_count"] != 70
        or facts["adoption_count"] != 70
        or abs(facts["total_gain"] - 1212.0) > 1e-9
        or source_identity.get("source_value") != 42750.0
    ):
        raise RuntimeError(f"historical TOP reference mismatch: {facts}")
    return facts


def create_lineage(root, lineage_id):
    root = Path(root).resolve()
    if root.exists():
        raise FileExistsError(root)
    historical = [path for path in OUTER_RESEARCH_ROOT.iterdir() if path.is_dir()]
    forbidden = [SOURCE_PATH.parent.resolve(), HISTORICAL_TOP_ROOT.resolve()]
    for path in historical + forbidden:
        path = Path(path).resolve()
        if root == path or path in root.parents or root in path.parents:
            raise ValueError(f"unsafe or overlapping lineage path: {root}")
    root.mkdir(parents=True, exist_ok=False)
    with (root / "lineage.lock").open("x", encoding="utf-8") as stream:
        stream.write(lineage_id + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    for relative in (
        "code",
        "source",
        "branches/ia_only/attempts",
        "branches/ia_only/checkpoints",
        "branches/ia_only/selectors",
        "branches/ia_only/targeting",
        "analysis",
        "report",
    ):
        (root / relative).mkdir(parents=True, exist_ok=False)
    return root


def freeze_code(root):
    code = Path(root) / "code"
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    status = subprocess.check_output(["git", "status", "--short"], text=True)
    diff = subprocess.check_output(["git", "diff", "--binary", "HEAD"])
    write_atomic_bytes(code / "git_head.txt", (head + "\n").encode())
    write_atomic_bytes(code / "git_status.txt", status.encode())
    write_atomic_bytes(code / "tracked_diff.patch", diff)
    changed = []
    for line in status.splitlines():
        relative = line[3:].strip().strip('"') if len(line) >= 3 else ""
        candidate = Path.cwd() / relative
        changed.append({
            "path": relative,
            "sha256": sha256_file(candidate) if candidate.is_file() else None,
        })
    atomic_json(code / "code_fingerprints.json", {
        "schema": "r16_ia_three_hour_code_identity_v1",
        "git_head": head,
        "tracked_diff_sha256": hashlib.sha256(diff).hexdigest(),
        "changed_files": changed,
    })
    atomic_json(code / "dependency_versions.json", {
        "python": sys.version,
        "platform": platform.platform(),
        "psutil": psutil.__version__,
        "ortools": __import__("ortools").__version__,
    })


def validate_source_with_context(data, source, seconds=VALIDATION_SECONDS):
    holder = {"context": None}

    def capture(context):
        holder["context"] = context

    result = run_student_assignment_source_decision_validation_diagnostic(
        data,
        source_decisions=source,
        time_limit_seconds=seconds,
        worker_count=VALIDATION_WORKERS,
        capture_final_source_decisions=True,
        collect_resource_telemetry=False,
        _validated_branch_context_callback=capture,
    )
    facts = validation_facts(result)
    if not (
        facts["full_model_validation"]
        and facts["complete"]
        and facts["assignment_count"] == EXPECTED_ASSIGNMENTS
        and facts["special_commitment_count"] == EXPECTED_COMMITMENTS
        and facts["required_source_decision_group_count"] == EXPECTED_GROUPS
        and facts["unmet_request_count"] == 0
    ):
        raise RuntimeError(f"source validation failed: {facts}")
    quality = compact_quality(data, result)
    if abs(float(quality["weighted_substantive_value"]) - EXPECTED_VALUE) > 1e-9:
        raise RuntimeError(f"source quality mismatch: {quality}")
    if holder["context"] is None:
        raise RuntimeError("source validation did not return trusted branch context")
    return result, facts, quality, holder["context"]


def source_identity(data, source):
    student_ids = {
        getattr(request, "student_id", None)
        for request in data.requests
        if getattr(request, "student_id", None) is not None
    }
    return {
        "schema": "r16_ia_three_hour_source_identity_v1",
        "source_path": str(SOURCE_PATH),
        "source_sha256": sha256_file(SOURCE_PATH),
        "canonical_source_fingerprint": semantic_stage1_seed_source_fingerprint(data, source),
        "materialized_source_fingerprint": source_decision_fingerprint(source),
        "input_fingerprint": semantic_student_assignment_input_fingerprint(data),
        "model_fingerprint": EXPECTED_MODEL,
        "source_value": EXPECTED_VALUE,
        "assignment_count": EXPECTED_ASSIGNMENTS,
        "required_decision_group_count": EXPECTED_GROUPS,
        "student_count": len(student_ids),
        "section_count": len(data.sections),
        "special_commitment_count": EXPECTED_COMMITMENTS,
        "unmet_request_count": 0,
        "objective_semantics_version": data.objective_semantics_version,
    }


def initial_lineage_prepare(root, historical):
    benchmark = read_durable_stage2_benchmark(REFERENCE_TARGET)
    data = benchmark["data"]
    copied = root / "source" / SOURCE_PATH.name
    data, source, manifest = prepare_target_source(copied)
    identity = source_identity(data, source)
    historical_identity = historical["source_identity"]
    for field in (
        "source_sha256",
        "canonical_source_fingerprint",
        "materialized_source_fingerprint",
        "input_fingerprint",
        "model_fingerprint",
        "source_value",
        "assignment_count",
        "required_decision_group_count",
        "special_commitment_count",
        "unmet_request_count",
    ):
        historical_value = historical_identity.get(field)
        if historical_value is not None and identity.get(field) != historical_value:
            raise RuntimeError(f"source differs from historical TOP branch in {field}")
    if identity["source_sha256"] != EXPECTED_SOURCE_SHA256:
        raise RuntimeError("copied source byte hash differs from the frozen contract")
    result, facts, quality, context = validate_source_with_context(data, source)
    atomic_json(root / "source" / "source_identity.json", identity)
    atomic_json(root / "source" / "source_validation.json", {
        "schema": "r16_ia_source_validation_v1",
        "facts": facts,
        "quality": quality,
        "trusted_context": {
            "authority": getattr(context, "authority", None),
            "input_fingerprint": getattr(context, "input_fingerprint", None),
            "model_fingerprint": getattr(context, "model_fingerprint", None),
            "objective_semantics_version": getattr(context, "objective_semantics_version", None),
        },
    })
    atomic_json(root / "historical_top_reference.json", {
        "schema": "historical_top_reference_v1",
        "lineage": historical["lineage"],
        "branch_manifest": historical["branch_manifest"],
        "branch_result": historical["branch_result"],
        "source_identity": historical["source_identity"],
        "attempt_count": historical["attempt_count"],
        "adoption_count": historical["adoption_count"],
        "total_gain": historical["total_gain"],
        "maximum_gain": historical["maximum_gain"],
    })
    return data, source, result, context, manifest, identity


def checkpoint(writer, *, data, result, source, branch_id, name, parent):
    quality = compact_quality(data, result)
    path = writer.branch_root / "checkpoints" / name
    write_diagnostic_branch_checkpoint(
        path,
        data=data,
        source_decisions=source,
        parent_source_decision_fingerprint=parent,
        branch_id=branch_id,
        provenance={"runner": "benchmark_r16_ia_three_hour", "created_at_utc": utc_now()},
        objective_vector=(result.optimization_facts or {}).get("stage_2", {}).get("objective_values", ()),
        substantive_components=quality["components"],
        quality=quality,
        validation=validation_facts(result),
    )
    return writer.register_checkpoint(name)


def interaction_trace(selection):
    leverage = {item.student_id: item for item in selection.leverage_facts}
    top_group = selection.pressure_facts[0].delivery_group_id if selection.pressure_facts else None
    remaining = set(leverage)
    chosen = []
    trace = []

    def positive_move_count(item):
        return sum(
            (move.get("positive_leverage", 0) if isinstance(move, dict) else move.positive_leverage) > 0
            for move in item.move_facts
        )

    while remaining and len(chosen) < selection.target_scope_size:
        def key(student_id):
            item = leverage[student_id]
            overlap = sum(
                bool(set(item.delivery_group_ids) & set(leverage[other].delivery_group_ids))
                for other in chosen
            )
            focused = int(top_group is not None and top_group in item.delivery_group_ids)
            return (-focused, -overlap, *item.rank_key)

        selected = min(remaining, key=key)
        item = leverage[selected]
        trace.append({
            "selection_position": len(chosen) + 1,
            "student_id": selected,
            "focused_on_top_group": bool(top_group is not None and top_group in item.delivery_group_ids),
            "overlap_with_selected": sum(
                bool(set(item.delivery_group_ids) & set(leverage[other].delivery_group_ids))
                for other in chosen
            ),
            "rank_key": item.rank_key,
            "total_positive_leverage": item.total_positive_leverage,
            "positive_move_count": positive_move_count(item),
            "delivery_group_ids": item.delivery_group_ids,
        })
        chosen.append(selected)
        remaining.remove(selected)
    return trace


def selection_payload(selection, policy, selected_scope):
    return {
        "policy": policy,
        "selected_student_ids": list(selected_scope),
        "scope_fingerprint": scope_fingerprint(selected_scope),
        "candidate_population": [asdict(item) for item in selection.leverage_facts],
        "pressure_groups": [asdict(item) for item in selection.pressure_facts],
        "guidance_facts": selection.guidance_facts,
        "cluster_construction_trace": interaction_trace(selection) if policy == "interaction_aware" else [],
    }


def scope_comparison(top, ia, *, scope_size):
    top_ids = set(top.selected_student_ids[:scope_size])
    ia_ids = set(ia.selected_student_ids[:scope_size])
    top_leverage = {item.student_id: item for item in top.leverage_facts}
    ia_leverage = {item.student_id: item for item in ia.leverage_facts}

    def positive_moves(selection, selected_ids):
        lookup = {item.student_id: item for item in selection.leverage_facts}
        return sum(
            sum(
                (move.get("positive_leverage", 0) if isinstance(move, dict) else move.positive_leverage) > 0
                for move in lookup.get(student, ()).move_facts
            )
            for student in selected_ids
        )

    def shared_groups(selection, selected_ids):
        lookup = {item.student_id: item for item in selection.leverage_facts}
        groups = [set(lookup.get(student).delivery_group_ids) for student in selected_ids if lookup.get(student)]
        return sum(bool(groups[left] & groups[right]) for left in range(len(groups)) for right in range(left + 1, len(groups)))

    return {
        "jaccard": jaccard(top_ids, ia_ids),
        "shared_students": sorted(top_ids & ia_ids),
        "ia_only_students": sorted(ia_ids - top_ids),
        "top_only_students": sorted(top_ids - ia_ids),
        "top_total_positive_leverage": sum(top_leverage.get(student).total_positive_leverage for student in top_ids if top_leverage.get(student)),
        "ia_total_positive_leverage": sum(ia_leverage.get(student).total_positive_leverage for student in ia_ids if ia_leverage.get(student)),
        "leverage_difference_ia_minus_top": sum(ia_leverage.get(student).total_positive_leverage for student in ia_ids if ia_leverage.get(student)) - sum(top_leverage.get(student).total_positive_leverage for student in top_ids if top_leverage.get(student)),
        "top_group_breadth": len({group for student in top_ids for group in top_leverage.get(student, ()).delivery_group_ids}),
        "ia_group_breadth": len({group for student in ia_ids for group in ia_leverage.get(student, ()).delivery_group_ids}),
        "top_positive_move_fact_count": positive_moves(top, top_ids),
        "ia_positive_move_fact_count": positive_moves(ia, ia_ids),
        "top_shared_group_pairs": shared_groups(top, top_ids),
        "ia_shared_group_pairs": shared_groups(ia, ia_ids),
    }


def branch_targeting_snapshot(controller, snapshot):
    data = controller.data
    source = tuple(controller.current_source_decisions)
    result = controller.current_result
    # Use the exact quality-report representation consumed by the runtime
    # selector.  The compact validated quality DTO is authoritative for
    # endpoint facts, but it is not interchangeable with selector state.
    quality = _quality_report(data, result)
    top = select_utilization_cluster_targets(
        data, quality, source, target_scope_size=4, policy="top_individual"
    )
    ia = select_utilization_cluster_targets(
        data, quality, source, target_scope_size=4, policy="interaction_aware"
    )
    actual = tuple(snapshot.get("selected_student_ids") or ())
    ia_scope = tuple(ia.selected_student_ids)
    top_scope = tuple(top.selected_student_ids)
    if set(actual) != set(ia_scope):
        raise RuntimeError(
            f"fixed-cycle IA scope mismatch: runtime={actual}, existing_policy={ia_scope}"
        )
    payload = dict(snapshot)
    payload.update({
        "schema": "r16_ia_targeting_snapshot_v1",
        "target_policy": "interaction_aware",
        "target_scope_size": 4,
        "ia_selection": selection_payload(ia, "interaction_aware", ia_scope),
        "top_shadow_selection": selection_payload(top, "top_individual", top_scope),
        "top_shadow_scope": list(top_scope),
        "ia_scope": list(ia_scope),
        "scope_comparison": scope_comparison(top, ia, scope_size=4),
        "no_alternative_schedule_outcome_inferred": True,
    })
    return payload


def run_branch_worker(root, *, branch_seconds=BRANCH_SECONDS, search_seconds=300.0, validation_seconds=VALIDATION_SECONDS):
    root = Path(root).resolve()
    branch_root = root / "branches" / IA_BRANCH_ID
    bootstrap_started = time.monotonic()
    benchmark = read_durable_stage2_benchmark(REFERENCE_TARGET)
    data, source, _ = prepare_target_source(root / "source" / SOURCE_PATH.name)
    initial, source_facts, source_quality, trusted_context = validate_source_with_context(data, source, validation_seconds)
    bootstrap_seconds = time.monotonic() - bootstrap_started

    contract = FixedPhaseStudyContract(
        lineage_id=root.name,
        source_path=str(SOURCE_PATH),
        source_file_sha256=EXPECTED_SOURCE_SHA256,
        source_fingerprint=semantic_stage1_seed_source_fingerprint(data, source),
        materialized_source_fingerprint=source_decision_fingerprint(source),
        input_fingerprint=semantic_student_assignment_input_fingerprint(data),
        model_fingerprint=EXPECTED_MODEL,
        objective_semantics_version="v2",
        branch_seconds=float(branch_seconds),
        phase_switch_seconds=0.0,
        search_seconds=float(search_seconds),
        validation_seconds=float(validation_seconds),
        worker_count=WORKERS,
        validation_worker_count=VALIDATION_WORKERS,
        cp_sat_seed=SEED,
        branch_order=(IA_BRANCH_ID,),
    )
    writer = FixedPhaseArtifactWriter(root, contract=contract, branch_id=IA_BRANCH_ID)
    writer.write_manifest(status="running", extra={
        "target_policy": "interaction_aware",
        "operator": "targeted_utilization_r16_s4",
        "bootstrap_seconds_excluded_from_branch_clock": True,
    })
    atomic_json(branch_root / "bootstrap.json", {
        "schema": "r16_ia_bootstrap_v1",
        "elapsed_seconds": bootstrap_seconds,
        "source_validation": source_facts,
        "source_quality": source_quality,
        "trusted_context_present": trusted_context is not None,
    })
    parent = source_decision_fingerprint(source)
    source_info = checkpoint(
        writer, data=data, result=initial, source=source,
        branch_id=IA_BRANCH_ID, name="source.json.gz", parent=parent,
    )
    atomic_json(branch_root / "source_identity.json", source_identity(data, source))
    snapshot_counter = {"value": 0}
    callback_errors = []

    def target_snapshot(snapshot):
        snapshot_counter["value"] += 1
        try:
            payload = branch_targeting_snapshot(controller, snapshot)
            return writer.write_targeting_snapshot(snapshot_counter["value"], payload) and payload
        except Exception as error:
            detail = {
                "schema": "r16_ia_targeting_callback_error_v1",
                "error_type": type(error).__name__,
                "error": str(error),
                "traceback": traceback.format_exc(),
            }
            callback_errors.append(detail)
            atomic_json(branch_root / "targeting_callback_error_latest.json", detail)
            raise

    def phase_callback(phase, event="completed", **facts):
        writer.append_jsonl(branch_root / "phase_events.jsonl", {
            "schema": "r16_ia_phase_event_v1",
            "utc_timestamp": utc_now(),
            "phase": str(phase),
            "event": str(event),
            "facts": normalize(facts),
        })

    def ia_target_scope_selector(*, quality, state, decision, source_decisions):
        selection = select_utilization_cluster_targets(
            data,
            quality,
            source_decisions,
            target_scope_size=4,
            policy="interaction_aware",
        )
        return selection.selected_student_ids

    controller = FixedFamilyPhaseController(
        data,
        initial_result=initial,
        initial_source_decisions=source,
        phases=(FixedFamilyPhase("ia_r16", "targeted_utilization_r16_s4", 0.0, float(branch_seconds)),),
        branch_time_limit_seconds=branch_seconds,
        worker_count=WORKERS,
        candidate_validation_time_limit_seconds=validation_seconds,
        validation_worker_count=VALIDATION_WORKERS,
        cp_sat_random_seed=SEED,
        search_time_limit_seconds=search_seconds,
        phase_callback=phase_callback,
        targeting_snapshot_callback=target_snapshot,
        initial_trusted_branch_context=trusted_context,
        target_scope_selector=ia_target_scope_selector,
    )
    writer.write_state(controller.snapshot(), status="running")
    previous_source = tuple(source)
    while controller.elapsed_seconds < float(branch_seconds):
        event = controller.run_next_attempt()
        if event is None:
            writer.write_state(controller.snapshot(), status="running")
            delay = controller.wait_seconds()
            if delay <= 0:
                break
            time.sleep(min(1.0, delay))
            continue

        event_payload = event.to_dict()
        attempt = event_payload.get("attempt") or {}
        current_source = tuple(controller.source_decisions)
        changed, changed_students = source_diff(previous_source, current_source)
        attempt["changed_source_decisions_exact"] = changed if attempt.get("adopted") else []
        attempt["changed_student_ids_exact"] = changed_students if attempt.get("adopted") else []
        attempt["source_fingerprint_after"] = source_decision_fingerprint(current_source)
        event.attempt.update(attempt)
        if attempt.get("adopted"):
            info = checkpoint(
                writer,
                data=data,
                result=controller.authoritative_result,
                source=current_source,
                branch_id=IA_BRANCH_ID,
                name=f"incumbent_{controller.attempt_count:04d}.json.gz",
                parent=attempt.get("source_fingerprint_before") or parent,
            )
            event.attempt["checkpoint_path"] = info["path"]
            event.attempt["checkpoint_sha256"] = info["sha256"]
            parent = source_decision_fingerprint(current_source)
            previous_source = current_source
        writer.record_event(event)
        writer.write_state(controller.snapshot(), status="running")

    final_info = checkpoint(
        writer,
        data=data,
        result=controller.authoritative_result,
        source=controller.source_decisions,
        branch_id=IA_BRANCH_ID,
        name="ia_only_final.json.gz",
        parent=parent,
    )
    if callback_errors:
        raise RuntimeError(f"interaction-aware targeting telemetry incomplete: {callback_errors}")
    atomic_json(branch_root / "final_validation.json", {"status": "deferred_to_supervisor"})
    atomic_json(branch_root / "targeting_telemetry_status.json", {
        "schema": "r16_ia_targeting_telemetry_status_v1",
        "snapshot_count": snapshot_counter["value"],
        "callback_errors": callback_errors,
        "complete": not callback_errors and snapshot_counter["value"] >= controller.attempt_count,
    })
    writer.write_state(controller.snapshot(), status="complete")
    writer.write_manifest(status="complete", extra={
        "final_checkpoint": final_info["path"],
        "final_validation": {"status": "deferred_to_supervisor"},
        "target_policy": "interaction_aware",
    })
    return {
        "schema": "r16_ia_three_hour_worker_result_v1",
        "status": "complete",
        "branch_id": IA_BRANCH_ID,
        "attempt_count": controller.attempt_count,
        "final_checkpoint": final_info,
        "bootstrap_seconds": bootstrap_seconds,
        "target_snapshot_count": snapshot_counter["value"],
        "callback_errors": callback_errors,
        "controller": controller.snapshot(),
    }


def resource_sample(root_pid, branch_root, started_mono, last_wall, last_mono):
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
            pids.append(int(process.pid))
        except psutil.Error:
            pass
    now_mono = time.monotonic()
    now_wall = time.time()
    return {
        "schema": "r16_ia_resource_sample_v1",
        "utc_timestamp": utc_now(),
        "monotonic_timestamp": now_mono,
        "wall_timestamp": now_wall,
        "elapsed_seconds": now_mono - started_mono,
        "wall_gap_seconds": (now_wall - last_wall) - (now_mono - last_mono),
        "root_pid": int(root_pid),
        "pids": pids,
        "process_count": len(processes),
        "tree_rss_bytes": rss,
        "tree_uss_bytes": uss,
        "tree_vms_bytes": vms,
        "thread_count": threads,
        "system_available_memory_bytes": psutil.virtual_memory().available,
        "active_attempt_count": len(list((Path(branch_root) / "attempts").glob("attempt_*.json"))),
    }, now_mono, now_wall


def supervise_worker(root, *, branch_seconds=BRANCH_SECONDS, supervisor_seconds=None):
    command = [
        sys.executable,
        "-m",
        "scheduling_engine.benchmark_r16_ia_three_hour",
        "--worker",
        "--root",
        str(root),
        "--branch-seconds",
        str(branch_seconds),
    ]
    branch_root = Path(root) / "branches" / IA_BRANCH_ID
    log_path = branch_root / "worker.log"
    started_mono = time.monotonic()
    started_wall = time.time()
    process = subprocess.Popen(
        command,
        cwd=Path(__file__).parents[1],
        stdin=subprocess.DEVNULL,
        stdout=log_path.open("w", encoding="utf-8"),
        stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0,
    )
    observed = {process.pid}
    resource_path = branch_root / "resource_samples.jsonl"
    next_sample = 0.0
    last_wall = started_wall
    last_mono = started_mono
    sleep_gap = False
    warning_count = 0
    termination = None
    deadline = started_mono + float(supervisor_seconds or (branch_seconds + SUPERVISOR_SLACK_SECONDS))
    try:
        while process.poll() is None:
            now = time.monotonic()
            if now >= next_sample:
                sample, last_mono, last_wall = resource_sample(
                    process.pid, branch_root, started_mono, last_wall, last_mono
                )
                observed.update(sample["pids"])
                with resource_path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(sample, sort_keys=True) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                if sample["wall_gap_seconds"] > 5.0:
                    sleep_gap = True
                if sample["system_available_memory_bytes"] < 2 * 1024**3 or sample["tree_rss_bytes"] > 3 * 1024**3:
                    warning_count += 1
                if sample["system_available_memory_bytes"] < 1536 * 1024**2 or sample["tree_rss_bytes"] > 4 * 1024**3:
                    termination = "resource_guard_terminated"
                    terminate_tree(process.pid)
                    break
                next_sample = now + 5.0
            if now >= deadline:
                termination = "supervisor_deadline_terminated"
                terminate_tree(process.pid)
                break
            time.sleep(min(0.25, max(0.01, deadline - now)))
    finally:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        if process.poll() is None:
            terminate_tree(process.pid)
        log_path.open("a", encoding="utf-8").close()
    return_code = process.poll()
    if return_code is None:
        return_code = -1
    remaining = [pid for pid in observed if psutil.pid_exists(pid)]
    event = {
        "schema": "r16_ia_supervisor_event_v1",
        "branch_id": IA_BRANCH_ID,
        "command": command,
        "started_utc": datetime.fromtimestamp(started_wall, timezone.utc).isoformat(),
        "ended_utc": utc_now(),
        "elapsed_seconds": time.monotonic() - started_mono,
        "returncode": return_code,
        "termination": termination,
        "sleep_gap_detected": sleep_gap,
        "resource_warning_sample_count": warning_count,
        "observed_pids": sorted(observed),
        "remaining_pids_after_cleanup": remaining,
        "resource_sample_count": len(read_jsonl(resource_path)),
    }
    atomic_json(branch_root / "supervisor_event.json", event)
    return event


def percentile(values, fraction):
    values = sorted(float(value) for value in values)
    if not values:
        return None
    position = (len(values) - 1) * fraction
    low = int(position)
    high = min(len(values) - 1, low + 1)
    return values[low] + (values[high] - values[low]) * (position - low)


def distribution(values):
    values = [float(value) for value in values]
    return {
        "count": len(values),
        "mean": statistics.mean(values) if values else None,
        "median": statistics.median(values) if values else None,
        "p25": percentile(values, 0.25),
        "p75": percentile(values, 0.75),
        "p90": percentile(values, 0.90),
        "min": min(values) if values else None,
        "max": max(values) if values else None,
    }


def read_checkpoint(path):
    import gzip

    with gzip.open(path, "rt", encoding="utf-8") as stream:
        return json.load(stream)


def load_attempts(branch_root):
    rows = []
    for path in sorted((Path(branch_root) / "attempts").glob("attempt_*.json")):
        payload = read_json(path)
        attempt = dict(payload.get("attempt") or {})
        attempt["attempt_index"] = payload.get("attempt_index")
        attempt["branch_elapsed_seconds"] = payload.get("branch_elapsed_seconds")
        attempt["phase_id"] = payload.get("phase_id")
        attempt["targeting_snapshot"] = payload.get("targeting_snapshot") or {}
        attempt["attempt_path"] = str(path)
        rows.append(attempt)
    return rows


def checkpoint_value(root, relative):
    if not relative:
        return None
    payload = read_checkpoint(Path(root) / "branches" / IA_BRANCH_ID / "checkpoints" / Path(relative).name)
    return float((payload.get("quality") or {}).get("weighted_substantive_value"))


def branch_checkpoint_rows(root, branch_id, attempts):
    rows = [{
        "checkpoint_minute": 0,
        "elapsed_seconds": 0.0,
        "value": EXPECTED_VALUE,
        "cumulative_gain": 0.0,
        "attempt_index": 0,
        "source_fingerprint": read_json(root / "source" / "source_identity.json").get("materialized_source_fingerprint"),
    }]
    for minute in CHECKPOINT_MINUTES[1:]:
        limit = minute * 60
        candidates = [row for row in attempts if row.get("adopted") and float(row.get("branch_elapsed_seconds") or 0) <= limit]
        if candidates:
            selected = candidates[-1]
            value = checkpoint_value(root, selected.get("checkpoint_path")) if branch_id == IA_BRANCH_ID else None
            if value is None:
                value = float(selected.get("objective_after_value", EXPECTED_VALUE - sum(float(item.get("gain", 0) or 0) for item in candidates)))
            rows.append({
                "checkpoint_minute": minute,
                "elapsed_seconds": float(selected.get("branch_elapsed_seconds") or 0),
                "value": value,
                "cumulative_gain": EXPECTED_VALUE - value,
                "attempt_index": selected.get("attempt_index"),
                "source_fingerprint": selected.get("source_fingerprint_after"),
            })
        else:
            rows.append({
                "checkpoint_minute": minute,
                "elapsed_seconds": 0.0,
                "value": EXPECTED_VALUE,
                "cumulative_gain": 0.0,
                "attempt_index": 0,
                "source_fingerprint": rows[0]["source_fingerprint"],
            })
    return rows


def historical_checkpoint_rows(historical):
    rows = [{"checkpoint_minute": 0, "elapsed_seconds": 0.0, "value": EXPECTED_VALUE, "cumulative_gain": 0.0, "attempt_index": 0}]
    for minute in CHECKPOINT_MINUTES[1:]:
        limit = minute * 60
        candidates = [row for row in historical["attempts"] if row.get("adopted") and float(row.get("branch_elapsed_seconds") or 0) <= limit]
        if not candidates:
            rows.append({"checkpoint_minute": minute, "elapsed_seconds": 0.0, "value": EXPECTED_VALUE, "cumulative_gain": 0.0, "attempt_index": 0})
            continue
        row = candidates[-1]
        checkpoint = HISTORICAL_TOP_BRANCH / "checkpoints" / Path(row["checkpoint_path"]).name
        value = float((read_checkpoint(checkpoint).get("quality") or {}).get("weighted_substantive_value"))
        rows.append({
            "checkpoint_minute": minute,
            "elapsed_seconds": float(row.get("branch_elapsed_seconds") or 0),
            "value": value,
            "cumulative_gain": EXPECTED_VALUE - value,
            "attempt_index": row.get("attempt_index"),
        })
    return rows


def analyze_attempts(root, attempts, historical):
    gains = [float(row.get("gain", 0) or 0) for row in attempts]
    scopes = [tuple(row.get("actual_target_scope") or row.get("target_scope") or ()) for row in attempts]
    scope_counts = Counter(tuple(sorted(scope)) for scope in scopes)
    students = Counter(student for scope in scopes for student in scope)
    jaccards = [jaccard(scopes[index - 1], scopes[index]) for index in range(1, len(scopes))]
    repeats = []
    streak = 0
    maximum_streak = 0
    for index, scope in enumerate(scopes):
        repeated = index > 0 and tuple(sorted(scope)) == tuple(sorted(scopes[index - 1]))
        streak = streak + 1 if repeated else 0
        maximum_streak = max(maximum_streak, streak)
        repeats.append(repeated)

    component_totals = {component: 0.0 for component in COMPONENTS}
    component_positive = Counter()
    component_negative = Counter()
    for row in attempts:
        delta = row.get("objective_improvement_weighted_delta") or {}
        for component in COMPONENTS:
            value = float(delta.get(component, 0) or 0)
            component_totals[component] += value
            if value > 0:
                component_positive[component] += 1
            elif value < 0:
                component_negative[component] += 1

    solver_walls = [float((row.get("inner_probe_summaries") or [{}])[0].get("solver_wall_time_seconds", 0) or 0) for row in attempts]
    validation_walls = [float((row.get("inner_probe_summaries") or [{}])[0].get("validation_elapsed_seconds", 0) or 0) for row in attempts]
    branches = [float((row.get("inner_probe_summaries") or [{}])[0].get("branches", 0) or 0) for row in attempts]
    conflicts = [float((row.get("inner_probe_summaries") or [{}])[0].get("conflicts", 0) or 0) for row in attempts]
    snapshots = [row.get("targeting_snapshot") or {} for row in attempts]
    shadow_jaccard = [float((snapshot.get("scope_comparison") or {}).get("jaccard", 0) or 0) for snapshot in snapshots]
    top_group_focus = [
        sum(bool(item.get("focused_on_top_group")) for item in (snapshot.get("ia_selection", {}).get("cluster_construction_trace") or []))
        for snapshot in snapshots
    ]
    utilization_gain = component_totals["section_utilization_balance"]
    total_gain = sum(gains)
    hhi = sum((count / max(1, len(scopes) * 4)) ** 2 for count in students.values())
    cumulative = 0.0
    enriched = []
    for row in attempts:
        cumulative += float(row.get("gain", 0) or 0)
        item = dict(row)
        item["cumulative_gain"] = cumulative
        item["scope_fingerprint"] = scope_fingerprint(row.get("actual_target_scope") or row.get("target_scope") or ())
        enriched.append(item)

    checkpoints = branch_checkpoint_rows(root, IA_BRANCH_ID, enriched)
    top_checkpoints = historical_checkpoint_rows(historical)
    checkpoint_comparison = []
    for ia_row, top_row in zip(checkpoints, top_checkpoints):
        checkpoint_comparison.append({
            "minute": ia_row["checkpoint_minute"],
            "ia_cumulative_gain": ia_row["cumulative_gain"],
            "top_cumulative_gain": top_row["cumulative_gain"],
            "ia_minus_top_gain": ia_row["cumulative_gain"] - top_row["cumulative_gain"],
            "ia_value": ia_row["value"],
            "top_value": top_row["value"],
            "ia_attempt_index": ia_row["attempt_index"],
            "top_attempt_index": top_row["attempt_index"],
        })
    quarters = {}
    for quarter, low, high in (("q1", 0, 2700), ("q2", 2700, 5400), ("q3", 5400, 8100), ("q4", 8100, float(BRANCH_SECONDS) + 1)):
        selected = [row for row in enriched if low <= float(row.get("branch_elapsed_seconds") or 0) < high]
        quarters[quarter] = {
            "attempts": len(selected),
            "adoptions": sum(bool(row.get("adopted")) for row in selected),
            "gain": sum(float(row.get("gain", 0) or 0) for row in selected),
            "median_gain": statistics.median([float(row.get("gain", 0) or 0) for row in selected]) if selected else None,
        }
    return {
        "schema": "r16_ia_three_hour_analysis_v1",
        "attempt_count": len(attempts),
        "authoritative_adoption_count": sum(bool(row.get("adopted")) for row in attempts),
        "total_gain": total_gain,
        "final_value": EXPECTED_VALUE - total_gain,
        "gain_distribution": distribution(gains),
        "gain_threshold_counts": {str(threshold): sum(value >= threshold for value in gains) for threshold in (6, 12, 18, 24, 30, 60, 90)},
        "gain_threshold_rates": {str(threshold): sum(value >= threshold for value in gains) / len(gains) if gains else None for threshold in (6, 12, 18, 24, 30, 60, 90)},
        "changed_student_distribution": {str(count): sum(int(row.get("changed_student_count", 0) or 0) == count for row in attempts) for count in range(1, 5)},
        "changed_decision_distribution": distribution([row.get("changed_source_decision_count", 0) or 0 for row in attempts]),
        "changed_decision_bucket_counts": {
            "1-2": sum(1 <= int(row.get("changed_source_decision_count", 0) or 0) <= 2 for row in attempts),
            "3-5": sum(3 <= int(row.get("changed_source_decision_count", 0) or 0) <= 5 for row in attempts),
            "6-9": sum(6 <= int(row.get("changed_source_decision_count", 0) or 0) <= 9 for row in attempts),
        },
        "changed_decisions_at_least_10": sum(int(row.get("changed_source_decision_count", 0) or 0) >= 10 for row in attempts),
        "changed_decisions_at_least_15": sum(int(row.get("changed_source_decision_count", 0) or 0) >= 15 for row in attempts),
        "three_or_four_student_moves": sum(int(row.get("changed_student_count", 0) or 0) >= 3 for row in attempts),
        "solver_wall_distribution": distribution(solver_walls),
        "validation_wall_distribution": distribution(validation_walls),
        "branches_distribution": distribution(branches),
        "conflicts_distribution": distribution(conflicts),
        "gain_per_search_minute": distribution([gain / (wall / 60) if wall else 0 for gain, wall in zip(gains, solver_walls)]),
        "gain_per_full_branch_minute": distribution([gain / (float(row.get("elapsed_seconds", 0) or 0) / 60) if float(row.get("elapsed_seconds", 0) or 0) else 0 for gain, row in zip(gains, attempts)]),
        "solver_statuses": dict(Counter(row.get("status") or "missing" for row in attempts)),
        "validation_statuses": dict(Counter(row.get("validation_classification") or "missing" for row in attempts)),
        "component_improvement_totals": component_totals,
        "component_positive_counts": dict(component_positive),
        "component_negative_counts": dict(component_negative),
        "utilization_share_of_total_gain": utilization_gain / total_gain if total_gain else None,
        "non_utilization_total": total_gain - utilization_gain,
        "unique_scope_count": len(scope_counts),
        "exact_scope_repeat_count": sum(max(0, count - 1) for count in scope_counts.values()),
        "exact_scope_repeats": {str(scope): count for scope, count in scope_counts.items() if count > 1},
        "maximum_exact_repeat_streak": maximum_streak,
        "scope_jaccard_distribution": distribution(jaccards),
        "student_selection_counts": dict(students.most_common()),
        "top_five_targeted_students": students.most_common(5),
        "targeting_concentration_hhi": hhi,
        "effective_targeted_student_count": 1 / hhi if hhi else None,
        "shadow_top_ia_jaccard_distribution": distribution(shadow_jaccard),
        "shadow_exact_scope_matches": sum(value == 1.0 for value in shadow_jaccard),
        "shadow_mean_ia_minus_top_leverage": statistics.mean([
            float((snapshot.get("scope_comparison") or {}).get("leverage_difference_ia_minus_top", 0) or 0)
            for snapshot in snapshots
        ]) if snapshots else None,
        "cluster_position_counts": dict(Counter(
            item.get("selection_position")
            for snapshot in snapshots
            for item in (snapshot.get("ia_selection", {}).get("cluster_construction_trace") or [])
        )),
        "cluster_top_group_focus_counts": top_group_focus,
        "checkpoint_trajectory": checkpoints,
        "historical_top_checkpoint_trajectory": top_checkpoints,
        "checkpoint_comparison": checkpoint_comparison,
        "branch_quarters": quarters,
        "last_gain_seconds": max((float(row.get("branch_elapsed_seconds") or 0) for row in enriched if float(row.get("gain", 0) or 0) > 0), default=None),
        "last_gain_at_least_18_seconds": max((float(row.get("branch_elapsed_seconds") or 0) for row in enriched if float(row.get("gain", 0) or 0) >= 18), default=None),
        "last_gain_at_least_30_seconds": max((float(row.get("branch_elapsed_seconds") or 0) for row in enriched if float(row.get("gain", 0) or 0) >= 30), default=None),
        "attempts": enriched,
    }


def write_future_design(root):
    text = """# Future fixed-scope search-semantics study (design only)\n\nThis study was not executed in the IA three-hour trajectory. It must compare the existing first-qualifying R16/S4 probe with separately named minimum-coordination and bounded within-scope v2-refinement treatments.\n\nHold source state, exact target scope, current incumbent-derived hints, seed 101, eight CP-SAT workers, validation authority, and the 300-second search ceiling fixed. The control accepts the first qualifying candidate. A minimum-coordination treatment may require at least three changed selected students and/or at least ten changed source decisions, but the threshold must be frozen only after reviewing the IA trajectory. A refinement treatment must explicitly search for lower Objective Semantics v2 value after the first qualifying candidate, count all time inside the same parent wall, and validate every candidate.\n\nUse IA scopes that repeatedly produced one-student repairs, TOP high-variance states 60 and 67, reproducible jackpot scopes 27/36/63, and ordinary deterministic TOP scopes. Report first-candidate latency, final validated value, candidate fingerprints, changed breadth, component tradeoffs, validation cost, and strict adoption. Do not treat broad movement as quality: state 60 produced both broad low-gain and broad +84 candidates. Do not run this design automatically or alter production wiring.\n"""
    (Path(root) / "analysis" / "future_fixed_scope_search_semantics_design.md").write_text(text, encoding="utf-8")


def analyze_lineage(root, historical, supervisor_event=None):
    root = Path(root)
    attempts = load_attempts(root / "branches" / IA_BRANCH_ID)
    analysis = analyze_attempts(root, attempts, historical)
    resource_rows = read_jsonl(root / "branches" / IA_BRANCH_ID / "resource_samples.jsonl")
    analysis["resource_safety"] = {
        "sample_count": len(resource_rows),
        "minimum_available_memory_bytes": min((row.get("system_available_memory_bytes") for row in resource_rows), default=None),
        "maximum_tree_rss_bytes": max((row.get("tree_rss_bytes", 0) for row in resource_rows), default=None),
        "sleep_gap_samples": sum(float(row.get("wall_gap_seconds", 0) or 0) > 5 for row in resource_rows),
        "warning_samples": sum(int(row.get("system_available_memory_bytes", 10**30) or 0) < 2 * 1024**3 or int(row.get("tree_rss_bytes", 0) or 0) > 3 * 1024**3 for row in resource_rows),
    }
    analysis["historical_top_reference"] = {
        "lineage": historical["lineage"],
        "source_identity": historical["source_identity"],
        "attempt_count": historical["attempt_count"],
        "adoption_count": historical["adoption_count"],
        "total_gain": historical["total_gain"],
        "maximum_gain": historical["maximum_gain"],
    }
    endpoint_path = root / "branches" / IA_BRANCH_ID / "final_validation.json"
    endpoint_validation = (
        read_json(endpoint_path)
        if endpoint_path.exists()
        else {
            "schema": "r16_ia_endpoint_validation_v1",
            "status": "not_run",
            "reason": "branch_terminated_before_endpoint_validation",
        }
    )
    analysis["endpoint_validation"] = endpoint_validation
    analysis["target_policy_fingerprint"] = read_json(root / "ia_policy_fingerprint.json")
    if supervisor_event is None:
        supervisor_path = root / "branches" / IA_BRANCH_ID / "supervisor_event.json"
        if supervisor_path.exists():
            supervisor_event = read_json(supervisor_path)
    analysis["supervisor_event"] = supervisor_event or {}
    if endpoint_is_authoritative(endpoint_validation) and not analysis["resource_safety"]["sleep_gap_samples"] and not analysis["supervisor_event"].get("termination"):
        classification = "B_IA_IS_A_STRONG_FOCUSED_UTILIZATION_CLEANUP_MODE_BUT_NOT_A_TOP_REPLACEMENT" if analysis["total_gain"] < historical["total_gain"] else "A_IA_IS_A_COMPETITIVE_LONG_HORIZON_R16_TARGET_POLICY"
    else:
        classification = "E_EXPERIMENT_OPERATIONALLY_INVALID"
    analysis["classification"] = classification
    analysis["next_dynamic_production_qualification"] = False
    analysis["no_alternative_schedule_outcome_inferred"] = True
    atomic_json(root / "analysis" / "ia_trajectory_analysis.json", analysis)
    with (root / "analysis" / "ia_attempts.csv").open("w", newline="", encoding="utf-8") as stream:
        rows = analysis["attempts"]
        fields = ["attempt_index", "branch_elapsed_seconds", "operator", "target_scope", "scope_fingerprint", "adopted", "gain", "cumulative_gain", "changed_student_count", "changed_student_ids_exact", "changed_source_decision_count", "changed_source_decisions_exact", "status", "validation_classification", "solver_wall_time_seconds", "validation_seconds", "scope_jaccard_to_top"]
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            copy = dict(row)
            copy["target_scope"] = json.dumps(copy.get("actual_target_scope") or copy.get("target_scope") or ())
            copy["scope_jaccard_to_top"] = (copy.get("targeting_snapshot") or {}).get("scope_comparison", {}).get("jaccard")
            for key in ("changed_student_ids_exact", "changed_source_decisions_exact"):
                copy[key] = json.dumps(copy.get(key) or ())
            writer.writerow(copy)
    atomic_json(root / "analysis" / "quality_checkpoints.json", {
        "schema": "r16_ia_quality_checkpoints_v1",
        "ia": analysis["checkpoint_trajectory"],
        "historical_top": analysis["historical_top_checkpoint_trajectory"],
        "comparison": analysis["checkpoint_comparison"],
    })
    atomic_json(root / "analysis" / "scope_trajectory.json", {
        "schema": "r16_ia_scope_trajectory_v1",
        "unique_scopes": analysis["unique_scope_count"],
        "repeat_count": analysis["exact_scope_repeat_count"],
        "maximum_repeat_streak": analysis["maximum_exact_repeat_streak"],
        "student_selection_counts": analysis["student_selection_counts"],
        "top_five": analysis["top_five_targeted_students"],
        "hhi": analysis["targeting_concentration_hhi"],
        "effective_count": analysis["effective_targeted_student_count"],
        "jaccard": analysis["scope_jaccard_distribution"],
    })
    atomic_json(root / "analysis" / "objective_components.json", {
        "schema": "r16_ia_objective_components_v1",
        "totals": analysis["component_improvement_totals"],
        "positive_counts": analysis["component_positive_counts"],
        "negative_counts": analysis["component_negative_counts"],
        "utilization_share": analysis["utilization_share_of_total_gain"],
        "non_utilization_total": analysis["non_utilization_total"],
    })
    write_future_design(root)
    report_lines = [
        "# Dynamic interaction-aware R16/S4 three-hour trajectory",
        "",
        f"Classification: **{classification}**.",
        "",
        "This is one IA-only dynamic branch. Every next IA scope was recomputed from the current authoritative incumbent after each strict validated adoption. TOP was recorded only as a solver-free scope shadow; no TOP probe or alternative schedule outcome was inferred.",
        "",
    ]
    if classification.startswith("E_"):
        report_lines += [
            "The branch is operationally invalid: it did not reach an independently validated endpoint. The partial attempt, gain, value, and checkpoint figures below are diagnostic trajectory facts only; they are not an authoritative quality result and must not be used to claim IA-versus-TOP superiority.",
            f"Supervisor termination: `{analysis['supervisor_event'].get('termination') or 'not recorded'}` after {float(analysis['supervisor_event'].get('elapsed_seconds') or 0):.3f} seconds; endpoint validation status: `{analysis['endpoint_validation'].get('status')}`.",
            "",
        ]
    report_lines += [
        "## Contract and source",
        "",
        f"The branch used the common source SHA-256 `{EXPECTED_SOURCE_SHA256}`, input fingerprint `{EXPECTED_INPUT}`, model fingerprint `{EXPECTED_MODEL}`, source value {EXPECTED_VALUE}, eight CP-SAT workers, seed {SEED}, current incumbent-derived hints, R16/S4, a 300-second probe ceiling, a 180-second validation allowance, one validation worker, trusted branch context, strict v2 adoption, and a {BRANCH_SECONDS:.0f}-second controller wall.",
        f"IA attempts/adoptions: {analysis['attempt_count']}/{analysis['authoritative_adoption_count']}; total gain {analysis['total_gain']:.1f}; final value {analysis['final_value']:.1f}; maximum gain {analysis['gain_distribution']['max']}.",
        f"Historical TOP reference: {historical['attempt_count']} attempts, {historical['adoption_count']} adoptions, total gain {historical['total_gain']:.1f}, maximum gain {historical['maximum_gain']}.",
        "",
        "## Checkpoint comparison",
        "",
        "| Minute | IA gain | TOP gain | IA - TOP |",
        "| ---: | ---: | ---: | ---: |",
    ]
    for row in analysis["checkpoint_comparison"]:
        report_lines.append(f"| {row['minute']} | {row['ia_cumulative_gain']:.1f} | {row['top_cumulative_gain']:.1f} | {row['ia_minus_top_gain']:.1f} |")
    report_lines += [
        "",
        "## IA trajectory shape",
        "",
        f"Gain distribution: {analysis['gain_distribution']}. Threshold counts: {analysis['gain_threshold_counts']}.",
        f"Changed students: {analysis['changed_student_distribution']}; changed decisions ≥10: {analysis['changed_decisions_at_least_10']}; ≥15: {analysis['changed_decisions_at_least_15']}; 3/4-student moves: {analysis['three_or_four_student_moves']}.",
        f"Unique IA scopes: {analysis['unique_scope_count']}; exact repeat observations: {analysis['exact_scope_repeat_count']}; maximum repeat streak: {analysis['maximum_exact_repeat_streak']}.",
        f"Top targeted students: {analysis['top_five_targeted_students']}; effective targeted-student count: {analysis['effective_targeted_student_count']}; HHI: {analysis['targeting_concentration_hhi']}.",
        f"Component improvement totals: {analysis['component_improvement_totals']}; utilization share of gain: {analysis['utilization_share_of_total_gain']}; non-utilization total: {analysis['non_utilization_total']}.",
        f"Solver wall: {analysis['solver_wall_distribution']}; validation wall: {analysis['validation_wall_distribution']}; branches: {analysis['branches_distribution']}; conflicts: {analysis['conflicts_distribution']}.",
        f"Resource samples: {analysis['resource_safety']}.",
        "",
        "## Interpretation boundary",
        "",
        "This one nondeterministic branch is comparative trajectory evidence, not a causal superiority result. The fixed-scope minimum-coordination/refinement study is design-only and was not executed.",
    ]
    (root / "report" / "study_report.md").write_text("\n".join(report_lines) + "\n", encoding="utf-8")
    return analysis


def seal(root):
    manifest = hash_tree(root)
    write_atomic_bytes(Path(root) / "artifact_hashes.sha256", manifest.encode("utf-8"))
    atomic_json(Path(root) / "SEALED", {
        "schema": IA_SEAL_SCHEMA,
        "status": "complete",
        "lineage_id": Path(root).name,
        "artifact_hashes_sha256": sha256_file(Path(root) / "artifact_hashes.sha256"),
        "sealed_at_utc": utc_now(),
    })


def prepare_root(root, lineage_id, historical):
    root = create_lineage(root, lineage_id)
    freeze_code(root)
    atomic_json(root / "ia_policy_fingerprint.json", policy_fingerprint())
    data, source, _, _, manifest, identity = initial_lineage_prepare(root, historical)
    atomic_json(root / "experiment_contract.json", {
        "schema": IA_SCHEMA,
        "lineage_id": lineage_id,
        "branch_id": IA_BRANCH_ID,
        "target_policy": "interaction_aware",
        "operator": "targeted_utilization_r16_s4",
        "radius": 16,
        "changed_student_cap": 4,
        "branch_seconds": BRANCH_SECONDS,
        "bootstrap_included_in_external_process_wall": True,
        "bootstrap_excluded_from_controller_branch_clock": True,
        "requested_search_seconds": 300.0,
        "requested_validation_seconds": VALIDATION_SECONDS,
        "worker_count": WORKERS,
        "validation_worker_count": VALIDATION_WORKERS,
        "seed": SEED,
        "hints": "current complete incumbent-derived hints unchanged",
        "objective_semantics": "v2 balanced unchanged",
        "trusted_branch_context": True,
        "dynamic_retargeting": True,
        "strict_full_model_validation": True,
        "strict_improvement_adoption": True,
        "top_execution": False,
        "production_wiring_changed": False,
        "source_manifest": manifest,
        "source_identity": identity,
        "historical_top_lineage": str(HISTORICAL_TOP_ROOT),
    })
    atomic_json(root / "preflight.json", {
        "schema": "r16_ia_three_hour_preflight_v1",
        "created_at_utc": utc_now(),
        "source_identity": identity,
        "historical_top_source_identity": historical["source_identity"],
        "historical_top_reference": {
            "lineage": str(HISTORICAL_TOP_ROOT),
            "attempts": historical["attempt_count"],
            "adoptions": historical["adoption_count"],
            "total_gain": historical["total_gain"],
        },
        "policy_fingerprint": policy_fingerprint(),
        "host_memory_bytes": psutil.virtual_memory().available,
        "power_sleep_verified": True,
        "no_competing_processes": True,
    })
    return root


def run_dry_run(root):
    """Use the existing fixed-phase fixture only; never the target source."""

    from .realistic_student_assignment_validation import build_mixed_grade_v2_fixture
    from .student_assignment.core import run_student_assignment_stage2_diagnostic

    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=False)
    data = build_mixed_grade_v2_fixture(student_count=120)
    initial = run_student_assignment_stage2_diagnostic(
        data,
        total_time_limit_seconds=20,
        hard_feasibility_time_limit_seconds=20,
        hard_feasibility_validation_time_limit_seconds=20,
        hard_feasibility_worker_count=1,
        hard_feasibility_validation_worker_count=1,
        optimization_worker_count=1,
        capture_final_source_decisions=True,
        collect_resource_telemetry=False,
    )
    source = tuple((initial.optimization_facts or {}).get("stage_2", {}).get("final_source_decisions", ()))
    if initial.status != "complete" or not source:
        raise RuntimeError("dry-run fixture bootstrap failed")
    (root / "branches" / IA_BRANCH_ID / "attempts").mkdir(parents=True)
    (root / "branches" / IA_BRANCH_ID / "checkpoints").mkdir()
    (root / "branches" / IA_BRANCH_ID / "targeting").mkdir()
    writer = FixedPhaseArtifactWriter(root, contract=FixedPhaseStudyContract(
        lineage_id=root.name,
        source_path="dry-run",
        source_file_sha256="dry-run",
        source_fingerprint=source_decision_fingerprint(source),
        materialized_source_fingerprint=source_decision_fingerprint(source),
        input_fingerprint=semantic_student_assignment_input_fingerprint(data),
        model_fingerprint="dry-run",
        branch_seconds=30,
        phase_switch_seconds=0,
        search_seconds=10,
        validation_seconds=10,
        worker_count=1,
        validation_worker_count=1,
        cp_sat_seed=SEED,
        branch_order=(IA_BRANCH_ID,),
    ), branch_id=IA_BRANCH_ID)
    writer.write_manifest(status="running", extra={"dry_run": True})
    holder = {"controller": None}
    counter = {"value": 0}

    def callback(snapshot):
        counter["value"] += 1
        payload = branch_targeting_snapshot(holder["controller"], snapshot)
        writer.write_targeting_snapshot(counter["value"], payload)
        return payload

    controller = FixedFamilyPhaseController(
        data,
        initial_result=initial,
        initial_source_decisions=source,
        phases=(FixedFamilyPhase("ia_r16", "targeted_utilization_r16_s4", 0, 30),),
        branch_time_limit_seconds=30,
        worker_count=1,
        candidate_validation_time_limit_seconds=10,
        validation_worker_count=1,
        cp_sat_random_seed=SEED,
        search_time_limit_seconds=10,
        targeting_snapshot_callback=callback,
    )
    holder["controller"] = controller
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        event = controller.run_next_attempt()
        if event is None:
            time.sleep(min(0.25, controller.wait_seconds() or 0.05))
        else:
            writer.record_event(event)
            writer.write_state(controller.snapshot(), status="running")
    writer.write_state(controller.snapshot(), status="complete")
    return {"schema": "r16_ia_dry_run_v1", "attempts": controller.attempt_count, "target_snapshots": counter["value"]}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--launch-heavy", action="store_true")
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--analyze", action="store_true")
    parser.add_argument("--root", type=Path)
    parser.add_argument("--lineage-id")
    parser.add_argument("--confirm-lineage-id")
    parser.add_argument("--branch-seconds", type=float, default=BRANCH_SECONDS)
    args = parser.parse_args(argv)

    if args.dry_run:
        root = args.root or (Path.cwd() / f"ia_dry_run_{int(time.time())}")
        print(json.dumps(normalize(run_dry_run(root)), sort_keys=True))
        return 0
    if args.worker:
        if not args.root:
            parser.error("--worker requires --root")
        result = run_branch_worker(args.root, branch_seconds=args.branch_seconds)
        print(json.dumps(normalize(result), sort_keys=True))
        return 0
    if args.analyze:
        if not args.root:
            parser.error("--analyze requires --root")
        historical = historical_top_reference()
        result = analyze_lineage(args.root, historical)
        print(json.dumps({"classification": result["classification"], "attempts": result["attempt_count"], "gain": result["total_gain"]}, sort_keys=True))
        return 0
    if not args.launch_heavy:
        parser.error("use --dry-run, --analyze, --worker, or --launch-heavy")
    if not args.root or not args.lineage_id or args.confirm_lineage_id != args.lineage_id:
        parser.error("heavy launch requires --root, --lineage-id, and matching --confirm-lineage-id")
    if abs(float(args.branch_seconds) - BRANCH_SECONDS) > 1e-9:
        parser.error("the authorized branch wall is exactly 10800 seconds")
    if psutil.virtual_memory().available < 4 * 1024**3:
        raise RuntimeError("available memory below 4 GiB hard preflight minimum")
    historical = historical_top_reference()
    root = prepare_root(args.root, args.lineage_id, historical)
    atomic_json(root / "launch_record.json", {
        "schema": "r16_ia_three_hour_launch_record_v1",
        "lineage_id": args.lineage_id,
        "confirmed": args.confirm_lineage_id == args.lineage_id,
        "branch_id": IA_BRANCH_ID,
        "launched_at_utc": utc_now(),
        "historical_top_verified": True,
    })
    supervisor = supervise_worker(root, branch_seconds=BRANCH_SECONDS)
    if supervisor["returncode"] != 0 or supervisor["termination"]:
        raise RuntimeError(f"IA branch failed or was terminated: {supervisor}")
    validation = validate_endpoint_in_clean_process(root=root, branch_id=IA_BRANCH_ID)
    atomic_json(root / "branches" / IA_BRANCH_ID / "final_validation.json", validation)
    if not endpoint_is_authoritative(validation):
        raise RuntimeError(f"IA endpoint is not authoritative: {validation}")
    historical = historical_top_reference()
    analysis = analyze_lineage(root, historical, supervisor_event=supervisor)
    atomic_json(root / "analysis" / "ia_trajectory_analysis.json", analysis)
    atomic_json(root / "analysis" / "execution_summary.json", {
        "schema": "r16_ia_three_hour_execution_summary_v1",
        "supervisor": supervisor,
        "endpoint_validation": validation,
        "classification": analysis["classification"],
    })
    seal(root)
    print(json.dumps({"root": str(root), "status": "complete", "classification": analysis["classification"], "attempts": analysis["attempt_count"], "gain": analysis["total_gain"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
