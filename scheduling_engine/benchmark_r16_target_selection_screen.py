"""Research-only R16/S4 target-selection screen.

This module compares the existing top-individual utilization scope with the
existing interaction-aware utilization scope.  It is deliberately separate
from production and from the adaptive selector: each cell reloads one
authoritative pre-state, executes exactly one R16/S4 probe, and records the
scope and observational targeting facts before the unchanged solver and
validation boundary are called.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
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
import uuid

from .student_assignment.adaptive_runtime import (
    run_adaptive_local_search_diagnostic,
)
from .student_assignment.adaptive_search import (
    AdaptiveOperatorSpec,
    build_adaptive_search_state,
)
from .student_assignment.core import (
    run_student_assignment_source_decision_validation_diagnostic,
)
from .student_assignment.quality import evaluate_student_assignment_quality
from .student_assignment.runtime import semantic_student_assignment_input_fingerprint
from .student_assignment.search_experiments import source_decision_fingerprint
from .student_assignment.stage2_benchmark import (
    read_durable_stage2_benchmark,
    read_diagnostic_branch_checkpoint,
    semantic_stage1_seed_source_fingerprint,
)
from .student_assignment.utilization_guidance import (
    select_utilization_cluster_targets,
)


STUDY_SCHEMA = "v2_r16_target_selection_screen_v1"
CELL_SCHEMA = "r16_target_selection_cell_v1"
POLICIES = ("top_individual", "interaction_aware")
SEEDS = (101, 202)
WORKER_COUNT = 1
VALIDATION_WORKERS = 1
SEARCH_SECONDS = 300.0
VALIDATION_SECONDS = 180.0
TARGET_SCOPE_SIZE = 4
OPERATOR = AdaptiveOperatorSpec(
    "targeted_utilization_r16_s4", 16, 4, True, 4, "utilization_repair",
    session_time_limit_seconds=SEARCH_SECONDS,
    session_max_attempts=1,
    per_attempt_cp_sat_limit_seconds=SEARCH_SECONDS,
)
FORENSIC_ROOT = Path(
    r"C:\Users\desou\research_runs\v2_r16_r4_targeting_hint_forensics_20260907_20260907_155839_a0bc36dd"
)
PRIOR_STUDY_ROOT = Path(
    r"C:\Users\desou\research_runs\v2_r16_vs_r4_r16_three_hour_20260907_054732_b8d42e6c"
)
REFERENCE_TARGET = Path(
    r"scheduling_engine\benchmarks\student_assignment\v2_policy_generalization_suite_20260829\reference_target"
)
SOURCE_PATH = Path(
    r"C:\Users\desou\research_runs\v2_r64_s8_two_hour_long_horizon_20260906\source\reference_target_common.json.gz"
)
EXPECTED_SOURCE_SHA256 = "31076e65806a17b133c01c06513f15f7c6d42d7c2991be4dc814a075023f7857"
EXPECTED_INPUT = "f56b5c0d5b745d919a57281a2f1e49959b4b23d8feb9486eda3c81afd8bb7906"
EXPECTED_MODEL = "ab929407fe3daed51c48e9280bc2f05f5e1610eeda2b316c8bb375ba30999cec"
EXPECTED_VALUE = 42750.0
EXPECTED_ASSIGNMENTS = 9030
EXPECTED_COMMITMENTS = 140
EXPECTED_GROUPS = 9170


def now_utc():
    return datetime.now(timezone.utc).isoformat()


def sha256_bytes(raw):
    return hashlib.sha256(raw).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def normalize(value):
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): normalize(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [normalize(v) for v in value]
    if isinstance(value, set):
        return [normalize(v) for v in sorted(value, key=repr)]
    if hasattr(value, "to_dict"):
        return normalize(value.to_dict())
    if hasattr(value, "__dict__"):
        return normalize(vars(value))
    return value


def json_bytes(payload):
    return json.dumps(
        normalize(payload), sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, default=repr,
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


def read_json(path):
    path = Path(path)
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            return json.load(stream)
    return json.loads(path.read_text(encoding="utf-8"))


def source_identity(data, source):
    return {
        "schema": "r16_target_selection_source_identity_v1",
        "input_fingerprint": semantic_student_assignment_input_fingerprint(data),
        "canonical_source_fingerprint": semantic_stage1_seed_source_fingerprint(data, source),
        "materialized_source_fingerprint": source_decision_fingerprint(source),
        "model_fingerprint": EXPECTED_MODEL,
        "source_value": EXPECTED_VALUE,
        "assignment_count": EXPECTED_ASSIGNMENTS,
        "special_commitment_count": EXPECTED_COMMITMENTS,
        "required_decision_group_count": EXPECTED_GROUPS,
        "unmet_request_count": 0,
    }


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
                for key in ("raw_penalty", "normalized_penalty", "denominator",
                            "importance_score", "weighted_normalized_contribution")
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


def validate_initial(data, source, *, validation_seconds=VALIDATION_SECONDS):
    context_holder = {"context": None}

    def capture_context(context):
        context_holder["context"] = context

    result = run_student_assignment_source_decision_validation_diagnostic(
        data,
        source_decisions=source,
        time_limit_seconds=validation_seconds,
        worker_count=VALIDATION_WORKERS,
        capture_final_source_decisions=True,
        collect_resource_telemetry=False,
        _validated_branch_context_callback=capture_context,
    )
    stage1 = dict((result.optimization_facts or {}).get("stage_1") or {})
    facts = {
        "full_model_validation": bool(
            (result.optimization_facts or {}).get("stage_2", {}).get("alternate_seed_validated")
        ),
        "complete": result.status == "complete",
        "assignment_count": len(result.assignments),
        "special_commitment_count": len(result.commitment_assignments),
        "required_decision_group_count": stage1.get("required_decision_group_count"),
        "unmet_request_count": len(result.unmet_requests),
        "solver_outcome": result.solver_outcome,
    }
    if not (
        facts["full_model_validation"] and facts["complete"]
        and facts["assignment_count"] == EXPECTED_ASSIGNMENTS
        and facts["special_commitment_count"] == EXPECTED_COMMITMENTS
        and facts["required_decision_group_count"] == EXPECTED_GROUPS
        and facts["unmet_request_count"] == 0
    ):
        raise RuntimeError(f"initial state validation failed: {facts}")
    quality = compact_quality(data, result)
    if abs(quality["weighted_substantive_value"] - EXPECTED_VALUE) > 1e-9:
        # The pre-state is allowed to be below the source value; only the
        # source identity and authoritative counts are frozen.  Preserve the
        # observed quality for the cell instead of rejecting a real fork.
        pass
    return result, facts, quality, context_holder["context"]


def _guidance_rows(selection):
    return {
        "pressure_facts": [asdict(row) for row in selection.pressure_facts],
        "leverage_facts": [asdict(row) for row in selection.leverage_facts],
        "guidance_facts": selection.guidance_facts,
    }


def _cluster_trace(selection):
    leverage = {row.student_id: row for row in selection.leverage_facts}
    top_group = selection.pressure_facts[0].delivery_group_id if selection.pressure_facts else None
    remaining = set(leverage)
    chosen = []
    trace = []
    while remaining and len(chosen) < selection.target_scope_size:
        def key(student_id):
            row = leverage[student_id]
            overlap = sum(bool(set(row.delivery_group_ids) & set(leverage[other].delivery_group_ids)) for other in chosen)
            focused = int(top_group is not None and top_group in row.delivery_group_ids)
            return (-focused, -overlap, *row.rank_key)
        student_id = min(remaining, key=key)
        row = leverage[student_id]
        trace.append({
            "selection_step": len(chosen) + 1,
            "remaining_population_count": len(remaining),
            "student_id": student_id,
            "focused_on_top_group": bool(top_group is not None and top_group in row.delivery_group_ids),
            "overlap_with_selected": sum(bool(set(row.delivery_group_ids) & set(leverage[other].delivery_group_ids)) for other in chosen),
            "rank_key": row.rank_key,
            "delivery_group_ids": row.delivery_group_ids,
        })
        chosen.append(student_id)
        remaining.remove(student_id)
    return trace


def _target_decisions(data, source, student_ids):
    selected = set(int(student_id) for student_id in student_ids)
    rows = []
    for key, value in sorted(dict(source).items(), key=repr):
        if isinstance(key, tuple) and key and key[0] in {"course", "commitment_course"}:
            if isinstance(value, tuple) and value and int(value[0]) in selected:
                rows.append({"source_key": key, "source_value": value})
    return rows


def targeting_snapshot(data, source, quality, state, policy, selection):
    scope = tuple(selection.selected_student_ids)
    leverage = {row.student_id: row for row in selection.leverage_facts}
    target_decisions = _target_decisions(data, source, scope)
    actionability = {
        str(student_id): {
            "optimistic_positive_move_count": sum(
                int(move.get("positive_leverage", 0) > 0)
                for move in leverage.get(student_id, ()).move_facts
            ) if student_id in leverage else 0,
            "destination_count": len(leverage[student_id].move_facts) if student_id in leverage else 0,
            "delivery_group_count": len(leverage[student_id].delivery_group_ids) if student_id in leverage else 0,
            "total_positive_leverage": leverage[student_id].total_positive_leverage if student_id in leverage else 0,
        }
        for student_id in scope
    }
    return {
        "schema": "r16_targeting_snapshot_v1",
        "policy": policy,
        "operator": OPERATOR.name,
        "operator_family": OPERATOR.portfolio_role,
        "source_fingerprint": source_decision_fingerprint(source),
        "canonical_scope": scope,
        "scope_count": len(scope),
        "substantive_value": state.substantive_aggregate,
        "objective_vector": state.current_objective_vector,
        "utilization_population_count": len(selection.leverage_facts),
        "utilization_population": [asdict(row) for row in selection.leverage_facts],
        "pressure_population": [asdict(row) for row in selection.pressure_facts],
        "selection": selection.guidance_facts,
        "cluster_construction_trace": _cluster_trace(selection) if policy == "interaction_aware" else [],
        "actionability_proxy": actionability,
        "target_local_source_decisions": target_decisions,
        "target_local_source_decision_count": len(target_decisions),
        "guidance_only": True,
        "objective_attribution": False,
    }


def target_hint_snapshot(data, source, scope, *, exact_identity_requested=False):
    target_rows = _target_decisions(data, source, scope)
    destinations = {}
    for row in target_rows:
        value = row["source_value"]
        destinations.setdefault(str(value[0]), []).append({
            "source_key": row["source_key"],
            "from_section_id": value[1] if len(value) > 1 else None,
        })
    return {
        "schema": "r16_target_hint_snapshot_v1",
        "hint_contract": "complete_authoritative_incumbent_derived",
        "scope_student_ids": tuple(scope),
        "target_source_decision_count": len(target_rows),
        "target_source_decision_fingerprint": sha256_bytes(json_bytes(target_rows)),
        "target_source_decision_rows": target_rows,
        "target_local_destination_observability": destinations,
        "exact_variable_identity": (
            "captured_in_inner_probe_hint_telemetry"
            if exact_identity_requested
            else "deferred_to_probe_hint_telemetry"
        ),
        "target_dependent_hint_facts_are_observational": True,
    }


def cell_identity(state_record, seed, policy, run_label=None):
    branch = state_record["branch"]
    attempt = int(state_record["attempt_index"])
    suffix = f"_{run_label}" if run_label else ""
    return f"{branch}_attempt_{attempt:03d}_{policy}_seed_{seed}{suffix}"


def run_cell(data, state_record, *, seed, policy, output_root,
             search_seconds=SEARCH_SECONDS,
             validation_seconds=VALIDATION_SECONDS,
             run_label=None,
             collect_search_start_telemetry=False,
             worker_count=WORKER_COUNT,
             fixed_scope=None,
             parent_time_limit_seconds=None,
             collect_hint_vector_telemetry=False,
             collect_hint_identity_telemetry=False):
    checkpoint = PRIOR_STUDY_ROOT / state_record["checkpoint_path"]
    branch = read_diagnostic_branch_checkpoint(checkpoint, data=data)
    source = tuple(branch["source_decisions"])
    identity = source_identity(data, source)
    if identity["input_fingerprint"] != EXPECTED_INPUT:
        raise RuntimeError("cell input fingerprint mismatch")
    initial, initial_validation, initial_quality, trusted_context = validate_initial(
        data, source, validation_seconds=validation_seconds
    )
    quality_report = evaluate_student_assignment_quality(
        data,
        assignments=initial.assignments,
        commitment_assignments=initial.commitment_assignments,
        solver_objective_components=initial.objective_components,
        include_entity_metrics=True,
    )
    state = build_adaptive_search_state(
        data, quality_report, elapsed_seconds=0.0, remaining_seconds=search_seconds,
        source_decisions=source, current_source_fingerprint=source_decision_fingerprint(source),
        candidate_validation_time_limit_seconds=VALIDATION_SECONDS,
        current_objective_vector=tuple((initial.optimization_facts or {}).get("stage_2", {}).get("objective_values", ()) or ()),
    )
    top = select_utilization_cluster_targets(
        data, quality_report, source, target_scope_size=TARGET_SCOPE_SIZE, policy="top_individual"
    )
    interaction = select_utilization_cluster_targets(
        data, quality_report, source, target_scope_size=TARGET_SCOPE_SIZE, policy="interaction_aware"
    )
    selection = top if policy == "top_individual" else interaction
    scope = tuple(fixed_scope) if fixed_scope is not None else tuple(selection.selected_student_ids)
    cell_dir = Path(output_root) / "cells" / cell_identity(
        state_record, seed, policy, run_label=run_label
    )
    cell_dir.mkdir(parents=True, exist_ok=False)
    atomic_write(cell_dir / "cell_manifest.json", {
        "schema": CELL_SCHEMA,
        "cell_id": cell_dir.name,
        "state": state_record,
        "policy": policy,
        "seed": seed,
        "operator": asdict(OPERATOR),
        "worker_count": worker_count,
        "validation_worker_count": VALIDATION_WORKERS,
        "search_seconds": search_seconds,
        "validation_seconds": VALIDATION_SECONDS,
        "parent_time_limit_seconds": (
            float(parent_time_limit_seconds)
            if parent_time_limit_seconds is not None
            else search_seconds
        ),
        "fixed_scope": list(scope),
        "source_identity": identity,
        "initial_validation": initial_validation,
    })
    snapshot = targeting_snapshot(data, source, initial_quality, state, policy, selection)
    atomic_write(cell_dir / "targeting_snapshot.json.gz", snapshot, compressed=True)
    atomic_write(cell_dir / "targeting_comparison_snapshot.json.gz", {
        "schema": "r16_targeting_comparison_snapshot_v1",
        "source_fingerprint": source_decision_fingerprint(source),
        "operator": OPERATOR.name,
        "target_scope_size": TARGET_SCOPE_SIZE,
        "top_individual": targeting_snapshot(
            data, source, initial_quality, state, "top_individual", top
        ),
        "interaction_aware": targeting_snapshot(
            data, source, initial_quality, state, "interaction_aware", interaction
        ),
        "guidance_only": True,
        "objective_attribution": False,
    }, compressed=True)
    atomic_write(cell_dir / "cluster_construction_trace.json.gz", {
        "schema": "r16_cluster_construction_trace_v1",
        "policy": policy,
        "trace": snapshot["cluster_construction_trace"],
        "selected_student_ids": scope,
        "selection_is_from_existing_guidance": True,
    }, compressed=True)
    atomic_write(cell_dir / "actionability_snapshot.json.gz", {
        "schema": "r16_actionability_snapshot_v1",
        "policy": policy,
        "scope_student_ids": scope,
        "proxy": snapshot["actionability_proxy"],
        "proxy_used_for_selection": False,
    }, compressed=True)
    atomic_write(
        cell_dir / "target_hint_snapshot.json.gz",
        target_hint_snapshot(
            data,
            source,
            scope,
            exact_identity_requested=collect_hint_identity_telemetry,
        ),
        compressed=True,
    )

    before_fp = source_decision_fingerprint(source)
    session = run_adaptive_local_search_diagnostic(
        data,
        initial_result=initial,
        initial_source_decisions=source,
        total_time_limit_seconds=(
            float(parent_time_limit_seconds)
            if parent_time_limit_seconds is not None
            else search_seconds
        ),
        per_operator_time_limit_seconds=search_seconds,
        worker_count=worker_count,
        portfolio=(OPERATOR,),
        max_iterations=1,
        collect_resource_telemetry=False,
        candidate_validation_time_limit_seconds=validation_seconds,
        hard_feasibility_validation_time_limit_seconds=validation_seconds,
        hard_feasibility_validation_worker_count=VALIDATION_WORKERS,
        cp_sat_random_seed=seed,
        session_id=cell_dir.name,
        selection_policy="fixed_cycle",
        fixed_cycle=(OPERATOR,),
        fixed_target_scope=scope,
        # Native search logging is intentionally opt-in.  It is observational
        # only and can perturb CP-SAT setup/wall behavior; semantic
        # target/destination telemetry above remains available without it.
        collect_search_start_telemetry=collect_search_start_telemetry,
        collect_hint_vector_telemetry=collect_hint_vector_telemetry,
        collect_hint_identity_telemetry=collect_hint_identity_telemetry,
        use_trusted_branch_context=True,
        initial_trusted_branch_context=trusted_context,
    )
    attempt = session.history[-1] if session.history else None
    attempt_payload = asdict(attempt) if attempt is not None else {}
    after_source = tuple(session.source_decisions)
    after_fp = source_decision_fingerprint(after_source)
    result_payload = {
        "schema": "r16_target_selection_solver_result_v1",
        "cell_id": cell_dir.name,
        "policy": policy,
        "seed": seed,
        "worker_count": worker_count,
        "parent_time_limit_seconds": (
            float(parent_time_limit_seconds)
            if parent_time_limit_seconds is not None
            else search_seconds
        ),
        "source_fingerprint_before": before_fp,
        "source_fingerprint_after": after_fp,
        "attempt": attempt_payload,
        "record": asdict(session.record),
        "candidate_authority": {
            "adopted": bool(attempt and attempt.adopted),
            "strict_improvement": bool(attempt and attempt.gain > 0),
            "full_model_validated": bool(attempt and attempt.candidate_validated),
            "candidate_source_fingerprint": attempt.candidate_source_decision_fingerprint if attempt else None,
        },
        "no_alternative_schedule_inferred": True,
    }
    atomic_write(cell_dir / "solver_result.json", result_payload)
    if attempt and attempt.adopted:
        atomic_write(cell_dir / "candidate.json.gz", {
            "schema": "r16_target_selection_authoritative_candidate_v1",
            "source_decisions": after_source,
            "quality": compact_quality(data, session.result),
            "validation": {
                "full_model_validated": bool(attempt.candidate_validated),
                "complete": session.result.status == "complete",
                "unmet_request_count": len(session.result.unmet_requests),
            },
        }, compressed=True)
        atomic_write(cell_dir / "authoritative_result.json", {
            "schema": "r16_target_selection_authoritative_result_v1",
            "source_fingerprint": after_fp,
            "quality": compact_quality(data, session.result),
            "adopted": True,
        })
    else:
        atomic_write(cell_dir / "authoritative_result.json", {
            "schema": "r16_target_selection_authoritative_result_v1",
            "source_fingerprint": before_fp,
            "quality": initial_quality,
            "adopted": False,
        })
    return {
        "schema": CELL_SCHEMA,
        "cell_id": cell_dir.name,
        "state_id": f"{state_record['branch']}_attempt_{int(state_record['attempt_index']):03d}",
        "branch": state_record["branch"],
        "attempt_index": int(state_record["attempt_index"]),
        "policy": policy,
        "seed": seed,
        "scope": scope,
        "source_fingerprint_before": before_fp,
        "source_fingerprint_after": after_fp,
        "adopted": bool(attempt and attempt.adopted),
        "candidate_validated": bool(attempt and attempt.candidate_validated),
        "candidate_found": bool(attempt and attempt.candidate_found),
        "gain": float(attempt.gain if attempt else 0.0),
        "candidate_discovery_gain": float(attempt.candidate_discovery_gain if attempt else 0.0),
        "status": attempt.status if attempt else "no_attempt",
        "validation_classification": attempt.validation_classification if attempt else "not_attempted",
        "solver_wall_seconds": attempt.solver_wall_time_seconds if attempt else None,
        "attempt_wall_seconds": attempt.elapsed_seconds if attempt else None,
        "changed_student_count": attempt.changed_student_count if attempt else 0,
        "changed_source_decision_count": attempt.changed_source_decision_count if attempt else 0,
        "inner_probe_summaries": attempt.inner_probe_summaries if attempt else (),
        "targeting_policy": policy,
        "top_scope": tuple(top.selected_student_ids),
        "interaction_scope": tuple(interaction.selected_student_ids),
        "scope_jaccard_to_other": (
            len(set(scope) & set(top.selected_student_ids if policy == "interaction_aware" else interaction.selected_student_ids))
            / len(set(scope) | set(top.selected_student_ids if policy == "interaction_aware" else interaction.selected_student_ids))
            if set(scope) | set(top.selected_student_ids if policy == "interaction_aware" else interaction.selected_student_ids) else 1.0
        ),
        "objective_before": initial_quality,
        "objective_after": compact_quality(data, session.result),
        "artifacts": {"cell_dir": str(cell_dir)},
    }


def load_states():
    payload = read_json(FORENSIC_ROOT / "representative_fork_states.json")
    states = []
    for record in payload.get("r16", []):
        if not record.get("authoritative_pre_state"):
            continue
        if record.get("operator") != "targeted_utilization_r16_s4":
            continue
        states.append(dict(record))
    if len(states) != 12:
        raise RuntimeError(f"expected 12 authoritative R16 states, found {len(states)}")
    return states


def ordered_cells(states):
    # Matched within-state order: seed 101 TOP then IA; seed 202 IA then TOP.
    for state in states:
        for seed, policies in ((101, ("top_individual", "interaction_aware")),
                               (202, ("interaction_aware", "top_individual"))):
            for policy in policies:
                yield state, seed, policy


def create_root(parent, lineage_id):
    parent = Path(parent).resolve()
    root = parent / lineage_id
    if root.exists():
        raise FileExistsError(root)
    if root == FORENSIC_ROOT.resolve() or root in FORENSIC_ROOT.resolve().parents:
        raise ValueError("unsafe lineage path")
    root.mkdir(parents=True, exist_ok=False)
    with (root / "lineage.lock").open("x", encoding="utf-8") as stream:
        stream.write(lineage_id)
        stream.flush()
        os.fsync(stream.fileno())
    for name in ("states", "cells", "analysis", "report", "integrity"):
        (root / name).mkdir(exist_ok=False)
    return root


def write_identity(root):
    status = subprocess.run(["git", "status", "--short"], capture_output=True, text=True, check=False)
    head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False)
    diff = subprocess.run(["git", "diff", "--binary", "HEAD"], capture_output=True, check=False)
    atomic_write(root / "code_identity.json", {
        "schema": "r16_target_selection_code_identity_v1",
        "git_head": head.stdout.strip(),
        "git_status": status.stdout,
        "tracked_diff_sha256": sha256_bytes(diff.stdout),
        "python": sys.version,
        "platform": platform.platform(),
        "created_at_utc": now_utc(),
    })


def dry_run(root, states, data):
    selected = states[0]
    rows = []
    for policy in POLICIES:
        rows.append(run_cell(data, selected, seed=101, policy=policy, output_root=root,
                             search_seconds=15.0, validation_seconds=VALIDATION_SECONDS))
    atomic_write(root / "dry_run_report.json", {
        "schema": "r16_target_selection_dry_run_v1",
        "classification": "DRY_RUN_NOT_QUALITY_EVIDENCE",
        "cells": rows,
        "requirements": {
            "one_worker": True,
            "source_reset_per_cell": True,
            "no_candidate_carryover": True,
            "explicit_policies": POLICIES,
            "hints_observational": True,
        },
    })
    return rows


def qualify(root, states, data):
    state = states[0]
    rows = []
    for repeat in (1, 2):
        rows.append(run_cell(data, state, seed=101, policy="top_individual", output_root=root,
                             search_seconds=SEARCH_SECONDS, run_label=f"repeat_{repeat}"))
    comparable = [
        (row["scope"], row["status"], row["candidate_found"], row["candidate_validated"], row["adopted"], row["gain"], row["source_fingerprint_after"])
        for row in rows
    ]
    passed = comparable[0] == comparable[1]
    atomic_write(root / "qualification_report.json", {
        "schema": "r16_target_selection_determinism_qualification_v1",
        "passed": passed,
        "state": state,
        "seed": 101,
        "worker_count": 1,
        "repeats": rows,
        "comparison_tuple": comparable,
        "stop_before_main_grid_if_disagreement": True,
    })
    if not passed:
        raise RuntimeError("determinism qualification disagreed; main grid not launched")
    return rows


def analyze(root, rows):
    analysis = Path(root) / "analysis"
    analysis.mkdir(exist_ok=True)
    paired = {}
    for row in rows:
        key = row["state_id"]
        paired.setdefault(key, {}).setdefault(row["seed"], {})[row["policy"]] = row
    state_rows = []
    for state_id, by_seed in paired.items():
        ia = [seed_rows["interaction_aware"] for seed_rows in by_seed.values() if "interaction_aware" in seed_rows]
        top = [seed_rows["top_individual"] for seed_rows in by_seed.values() if "top_individual" in seed_rows]
        ia_gain = sum(row["gain"] for row in ia) / max(1, len(ia))
        top_gain = sum(row["gain"] for row in top) / max(1, len(top))
        state_rows.append({
            "state_id": state_id,
            "interaction_aware_mean_gain": ia_gain,
            "top_individual_mean_gain": top_gain,
            "interaction_aware_minus_top": ia_gain - top_gain,
            "interaction_aware_adoptions": sum(bool(row["adopted"]) for row in ia),
            "top_individual_adoptions": sum(bool(row["adopted"]) for row in top),
            "resolved_seed_count": len(ia),
        })
    differences = [row["interaction_aware_minus_top"] for row in state_rows]
    positive = sum(value > 0 for value in differences)
    negative = sum(value < 0 for value in differences)
    ordered = sorted(differences)
    median = ordered[len(ordered) // 2] if ordered else None
    trimmed = sorted(differences)[1:-1] if len(differences) > 2 else ordered
    trimmed_median = trimmed[len(trimmed) // 2] if trimmed else None
    classification = "C"
    if len(state_rows) == 12 and median is not None and median >= 6 and positive > negative and (trimmed_median or 0) > 0:
        classification = "A"
    elif len(state_rows) == 12 and median is not None and median <= -6 and negative > positive and (trimmed_median or 0) < 0:
        classification = "B"
    atomic_write(analysis / "paired_cell_results.json", {"schema": "r16_paired_cell_results_v1", "rows": rows})
    atomic_write(analysis / "state_level_results.json", {"schema": "r16_state_level_results_v1", "rows": state_rows})
    atomic_write(analysis / "stage1_classification.json", {
        "schema": "r16_target_selection_stage1_classification_v1",
        "classification": classification,
        "interaction_aware_minus_top_mean_by_state_median": median,
        "trimmed_median": trimmed_median,
        "states_interaction_aware_wins": positive,
        "states_top_individual_wins": negative,
        "no_alternative_schedule_outcome_inferred": True,
        "hint_readiness": "B_OBSERVABILITY_STILL_INSUFFICIENT",
    })
    return classification


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--parent", type=Path, default=Path(r"C:\Users\desou\research_runs"))
    parser.add_argument("--lineage-id")
    parser.add_argument("--confirm-lineage-id")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--qualify", action="store_true")
    parser.add_argument("--run-grid", action="store_true")
    args = parser.parse_args(argv)
    if not (args.dry_run or args.qualify or args.run_grid):
        parser.error("choose --dry-run, --qualify, or --run-grid")
    states = load_states()
    benchmark = read_durable_stage2_benchmark(REFERENCE_TARGET)
    data = benchmark["data"]
    if semantic_student_assignment_input_fingerprint(data) != EXPECTED_INPUT:
        raise RuntimeError("reference target input fingerprint mismatch")
    source_hash = sha256_file(SOURCE_PATH)
    if source_hash != EXPECTED_SOURCE_SHA256:
        raise RuntimeError("authoritative source byte hash mismatch")
    lineage_id = args.lineage_id or f"v2_r16_target_selection_screen_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:8]}"
    if args.confirm_lineage_id != lineage_id:
        raise RuntimeError("--confirm-lineage-id must exactly match lineage id")
    root = Path(args.output_root) if args.output_root else args.parent / lineage_id
    root = create_root(root.parent, root.name)
    write_identity(root)
    atomic_write(root / "experiment_contract.json", {
        "schema": STUDY_SCHEMA,
        "lineage_id": root.name,
        "policies": POLICIES,
        "states": len(states),
        "seeds": SEEDS,
        "cells": 48,
        "operator": asdict(OPERATOR),
        "worker_count": WORKER_COUNT,
        "validation_worker_count": VALIDATION_WORKERS,
        "search_seconds": SEARCH_SECONDS,
        "validation_seconds": VALIDATION_SECONDS,
        "objective_semantics_version": "v2",
        "objective_profile": "balanced",
        "production_default_unchanged": True,
        "adaptive_selector_unchanged": True,
        "source_path": str(SOURCE_PATH),
        "source_sha256": source_hash,
        "created_at_utc": now_utc(),
    })
    atomic_write(root / "preflight.json", {
        "schema": "r16_target_selection_preflight_v1",
        "source_sha256": source_hash,
        "input_fingerprint": semantic_student_assignment_input_fingerprint(data),
        "model_fingerprint": EXPECTED_MODEL,
        "state_count": len(states),
        "state_ids": [f"{state['branch']}_attempt_{int(state['attempt_index']):03d}" for state in states],
        "python_pid": os.getpid(),
        "platform": platform.platform(),
        "created_at_utc": now_utc(),
    })
    rows = []
    if args.dry_run:
        dry_run(root, states, data)
    if args.qualify:
        qualify(root, states, data)
    if args.run_grid:
        qualification = root / "qualification_report.json"
        if not qualification.exists() or not read_json(qualification).get("passed"):
            raise RuntimeError("qualification_report.json must pass before main grid")
        for state, seed, policy in ordered_cells(states):
            row = run_cell(data, state, seed=seed, policy=policy, output_root=root)
            rows.append(row)
            atomic_write(root / "analysis" / "progress.json", {
                "schema": "r16_target_selection_progress_v1",
                "completed_cells": len(rows),
                "expected_cells": 48,
                "last_cell": row,
                "updated_at_utc": now_utc(),
            })
        analyze(root, rows)
    print(json.dumps({"root": str(root), "mode": "dry_run" if args.dry_run else "qualify" if args.qualify else "run_grid", "completed_cells": len(rows)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
