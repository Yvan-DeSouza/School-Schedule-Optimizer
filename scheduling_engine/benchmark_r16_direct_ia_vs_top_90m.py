"""Research-only dynamic IA versus TOP R16/S4 trajectory study.

This module is deliberately separate from production scheduling.  It runs two
clean, sequential, independently reset branches from the same validated
source.  Each branch dynamically recomputes either the existing
``interaction_aware`` or ``top_individual`` four-student target policy after
every authoritative adoption, then runs the existing R16/S4 operator with the
research-only direct exact-v2 probe semantics.

The heavy command is intentionally explicit::

    python -m scheduling_engine.benchmark_r16_direct_ia_vs_top_90m \
      --launch-heavy --root <new-research-root> \
      --lineage-id <id> --confirm-lineage-id <id>

No production policy, hint behavior, objective semantics, hard constraint, or
validation authority is changed by this runner.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time

import psutil

from .benchmark_fixed_phase_study import (
    BRANCH_SECONDS as _OLD_BRANCH_SECONDS,
    EXPECTED_ASSIGNMENTS,
    EXPECTED_COMMITMENTS,
    EXPECTED_GROUPS,
    EXPECTED_INPUT,
    EXPECTED_MODEL,
    EXPECTED_SOURCE_SHA256,
    EXPECTED_VALUE,
    REFERENCE_TARGET,
    SEED,
    VALIDATION_SECONDS,
    VALIDATION_WORKERS,
    WORKERS,
    compact_quality,
    normalize,
    prepare_target_source,
    validation_facts,
    validate_endpoint_in_clean_process,
    endpoint_is_authoritative,
    _tree_processes,
    terminate_tree,
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
    create_lineage,
    hash_tree,
    sha256_file,
)
from .student_assignment.runtime import semantic_student_assignment_input_fingerprint
from .student_assignment.search_experiments import source_decision_fingerprint
from .student_assignment.search_guidance import rank_students_by_quality_pressure
from .student_assignment.stage2_benchmark import (
    semantic_stage1_seed_source_fingerprint,
    read_durable_stage2_benchmark,
    write_diagnostic_branch_checkpoint,
)
from .student_assignment.utilization_guidance import (
    select_utilization_cluster_targets,
)


BRANCH_SECONDS = 5_400.0
SEARCH_SECONDS = 300.0
IA_BRANCH = "interaction_aware"
TOP_BRANCH = "top_individual"
BRANCHES = (IA_BRANCH, TOP_BRANCH)
OPERATOR = "targeted_utilization_r16_s4"
SCHEMA = "v2_r16_direct_ia_vs_top_90m_study_v1"
ATTEMPT_SCHEMA = "r16_direct_target_selection_attempt_v1"
CHECKPOINT_MINUTES = tuple(range(0, 91, 5))
OUTER_RESEARCH_ROOT = Path(r"C:\Users\desou\research_runs")


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def json_bytes(payload):
    return json.dumps(
        normalize(payload), sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, default=repr,
    ).encode("utf-8")


def write_atomic(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = payload if isinstance(payload, bytes) else json_bytes(payload)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    return raw


def source_identity(data, source):
    return {
        "schema": "r16_direct_target_selection_source_identity_v1",
        "source_sha256": sha256_file(Path(data._source_path))
        if hasattr(data, "_source_path") else None,
        "canonical_source_fingerprint": semantic_stage1_seed_source_fingerprint(data, source),
        "materialized_source_fingerprint": source_decision_fingerprint(source),
        "input_fingerprint": semantic_student_assignment_input_fingerprint(data),
        "model_fingerprint": EXPECTED_MODEL,
        "source_value": EXPECTED_VALUE,
        "assignment_count": EXPECTED_ASSIGNMENTS,
        "required_decision_group_count": EXPECTED_GROUPS,
        "special_commitment_count": EXPECTED_COMMITMENTS,
        "unmet_request_count": 0,
        "objective_semantics_version": data.objective_semantics_version,
    }


def validate_source(data, source):
    holder = {"context": None}

    def capture(context):
        holder["context"] = context

    result = run_student_assignment_source_decision_validation_diagnostic(
        data,
        source_decisions=source,
        time_limit_seconds=VALIDATION_SECONDS,
        worker_count=VALIDATION_WORKERS,
        capture_final_source_decisions=True,
        collect_resource_telemetry=False,
        _validated_branch_context_callback=capture,
    )
    facts = validation_facts(result)
    quality = compact_quality(data, result)
    if not (
        facts["full_model_validation"] and facts["complete"]
        and facts["assignment_count"] == EXPECTED_ASSIGNMENTS
        and facts["special_commitment_count"] == EXPECTED_COMMITMENTS
        and facts["required_source_decision_group_count"] == EXPECTED_GROUPS
        and facts["unmet_request_count"] == 0
        and abs(float(quality["weighted_substantive_value"]) - EXPECTED_VALUE) < 1e-9
        and holder["context"] is not None
    ):
        raise RuntimeError({"facts": facts, "quality": quality})
    return result, facts, quality, holder["context"]


def policy_selection(data, quality, source, policy):
    return select_utilization_cluster_targets(
        data, quality, source, target_scope_size=4, policy=policy,
    )


def targeting_snapshot(data, quality, source, policy, selected):
    chosen = policy_selection(data, quality, source, policy)
    other_policy = TOP_BRANCH if policy == IA_BRANCH else IA_BRANCH
    shadow = policy_selection(data, quality, source, other_policy)
    return {
        "schema": "r16_direct_target_selection_snapshot_v1",
        "policy": policy,
        "operator": OPERATOR,
        "target_scope_size": 4,
        "selected_student_ids": list(selected),
        "scope_fingerprint": hashlib.sha256(json_bytes(sorted(selected))).hexdigest(),
        "active_selection": {
            "guidance_facts": chosen.guidance_facts,
            "pressure_facts": [asdict(item) for item in chosen.pressure_facts[:10]],
            "leverage_facts": [asdict(item) for item in chosen.leverage_facts[:20]],
        },
        "shadow_selection": {
            "policy": other_policy,
            "selected_student_ids": list(shadow.selected_student_ids),
            "scope_fingerprint": hashlib.sha256(json_bytes(sorted(shadow.selected_student_ids))).hexdigest(),
            "guidance_facts": shadow.guidance_facts,
        },
        "source_fingerprint": source_decision_fingerprint(source),
        "no_alternative_schedule_outcome_inferred": True,
    }


def branch_checkpoint(writer, *, data, result, source, branch, name, parent):
    quality = compact_quality(data, result)
    path = writer.branch_root / "checkpoints" / name
    write_diagnostic_branch_checkpoint(
        path,
        data=data,
        source_decisions=source,
        parent_source_decision_fingerprint=parent,
        branch_id=branch,
        provenance={"runner": "benchmark_r16_direct_ia_vs_top_90m", "created_at_utc": utc_now()},
        objective_vector=(result.optimization_facts or {}).get("stage_2", {}).get("objective_values", ()),
        substantive_components=quality["components"],
        quality=quality,
        validation=validation_facts(result),
    )
    return writer.register_checkpoint(name)


def source_diff(before, after):
    before = dict(tuple(before or ()))
    after = dict(tuple(after or ()))
    changed = []
    students = set()
    for key in sorted(set(before) | set(after), key=repr):
        if before.get(key) == after.get(key):
            continue
        changed.append({"source_key": key, "before": before.get(key), "after": after.get(key)})
        for value in (before.get(key), after.get(key)):
            if isinstance(value, (tuple, list)) and value:
                try:
                    students.add(int(value[0]))
                except (TypeError, ValueError):
                    pass
    return changed, sorted(students)


def run_branch_worker(root, branch, *, branch_seconds=BRANCH_SECONDS):
    root = Path(root).resolve()
    branch_started = time.monotonic()
    data, source, _manifest = prepare_target_source(root / "source" / "reference_target_common.json.gz")
    initial, source_facts, source_quality, trusted_context = validate_source(data, source)
    branch_root = root / "branches" / branch
    contract = FixedPhaseStudyContract(
        lineage_id=root.name,
        source_path=str(root / "source" / "reference_target_common.json.gz"),
        source_file_sha256=sha256_file(root / "source" / "reference_target_common.json.gz"),
        source_fingerprint=semantic_stage1_seed_source_fingerprint(data, source),
        materialized_source_fingerprint=source_decision_fingerprint(source),
        input_fingerprint=semantic_student_assignment_input_fingerprint(data),
        model_fingerprint=EXPECTED_MODEL,
        objective_semantics_version="v2",
        branch_seconds=float(branch_seconds),
        phase_switch_seconds=0.0,
        search_seconds=SEARCH_SECONDS,
        validation_seconds=VALIDATION_SECONDS,
        worker_count=WORKERS,
        validation_worker_count=VALIDATION_WORKERS,
        cp_sat_seed=SEED,
        branch_order=BRANCHES,
    )
    writer = FixedPhaseArtifactWriter(root, contract=contract, branch_id=branch)
    writer.write_manifest(status="running", extra={
        "target_policy": branch,
        "operator": OPERATOR,
        "search_semantics": "direct_exact_v2_optimization",
        "branch_clock_started_before_bootstrap": True,
    })
    atomic_json(branch_root / "bootstrap.json", {
        "schema": "r16_direct_target_selection_bootstrap_v1",
        "source_validation": source_facts,
        "source_quality": source_quality,
        "branch_started_monotonic": branch_started,
    })
    parent = source_decision_fingerprint(source)
    branch_checkpoint(writer, data=data, result=initial, source=source, branch=branch, name="source.json.gz", parent=parent)
    atomic_json(branch_root / "source_identity.json", {
        "schema": "r16_direct_target_selection_source_identity_v1",
        "source_sha256": sha256_file(root / "source" / "reference_target_common.json.gz"),
        "canonical_source_fingerprint": semantic_stage1_seed_source_fingerprint(data, source),
        "materialized_source_fingerprint": source_decision_fingerprint(source),
        "input_fingerprint": semantic_student_assignment_input_fingerprint(data),
        "model_fingerprint": EXPECTED_MODEL,
        "source_value": EXPECTED_VALUE,
        "assignment_count": EXPECTED_ASSIGNMENTS,
        "required_decision_group_count": EXPECTED_GROUPS,
        "special_commitment_count": EXPECTED_COMMITMENTS,
        "unmet_request_count": 0,
    })

    holder = {"controller": None}
    snapshot_counter = {"value": 0}

    def scope_selector(*, quality, state, decision, source_decisions):
        return policy_selection(data, quality, source_decisions, branch).selected_student_ids

    def snapshot_callback(snapshot):
        snapshot_counter["value"] += 1
        quality = _quality_report(data, holder["controller"].current_result)
        payload = targeting_snapshot(
            data, quality, tuple(holder["controller"].current_source_decisions),
            branch, tuple(snapshot.get("selected_student_ids") or ()),
        )
        payload["selector_state"] = snapshot.get("selector_state", {})
        return payload

    def phase_callback(phase, event="completed", **facts):
        writer.append_jsonl(branch_root / "phase_events.jsonl", {
            "schema": "r16_direct_target_selection_phase_event_v1",
            "utc_timestamp": utc_now(), "phase": str(phase), "event": str(event),
            "facts": normalize(facts),
        })

    controller = FixedFamilyPhaseController(
        data,
        initial_result=initial,
        initial_source_decisions=source,
        phases=(FixedFamilyPhase(branch, OPERATOR, 0.0, float(branch_seconds)),),
        branch_time_limit_seconds=branch_seconds,
        worker_count=WORKERS,
        candidate_validation_time_limit_seconds=VALIDATION_SECONDS,
        validation_worker_count=VALIDATION_WORKERS,
        cp_sat_random_seed=SEED,
        search_time_limit_seconds=SEARCH_SECONDS,
        search_semantics="direct_exact_v2_optimization",
        collect_search_start_telemetry=True,
        phase_callback=phase_callback,
        targeting_snapshot_callback=snapshot_callback,
        initial_trusted_branch_context=trusted_context,
        target_scope_selector=scope_selector,
        started_at=branch_started,
    )
    holder["controller"] = controller
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
        attempt = event.attempt
        current_source = tuple(controller.source_decisions)
        changed, changed_students = source_diff(previous_source, current_source)
        attempt["changed_source_decisions_exact"] = changed if attempt.get("adopted") else []
        attempt["changed_student_ids_exact"] = changed_students if attempt.get("adopted") else []
        attempt["source_fingerprint_after"] = source_decision_fingerprint(current_source)
        attempt["search_semantics"] = "direct_exact_v2_optimization"
        attempt["schema"] = ATTEMPT_SCHEMA
        if attempt.get("adopted"):
            info = branch_checkpoint(
                writer, data=data, result=controller.authoritative_result,
                source=current_source, branch=branch,
                name=f"incumbent_{controller.attempt_count:04d}.json.gz", parent=parent,
            )
            attempt["checkpoint_path"] = info["path"]
            attempt["checkpoint_sha256"] = info["sha256"]
            parent = source_decision_fingerprint(current_source)
            previous_source = current_source
        writer.record_event(event)
        writer.write_state(controller.snapshot(), status="running")
    final = branch_checkpoint(
        writer, data=data, result=controller.authoritative_result,
        source=controller.source_decisions, branch=branch,
        name=f"{branch}_final.json.gz", parent=parent,
    )
    atomic_json(branch_root / "targeting_telemetry_status.json", {
        "schema": "r16_direct_target_selection_telemetry_status_v1",
        "snapshot_count": snapshot_counter["value"],
        "complete": snapshot_counter["value"] >= controller.attempt_count,
    })
    writer.write_state(controller.snapshot(), status="complete")
    writer.write_manifest(status="complete", extra={
        "target_policy": branch, "final_checkpoint": final["path"],
        "search_semantics": "direct_exact_v2_optimization",
    })
    return {
        "schema": "r16_direct_target_selection_worker_result_v1",
        "status": "complete", "branch": branch,
        "attempt_count": controller.attempt_count,
        "final_checkpoint": final,
        "target_snapshot_count": snapshot_counter["value"],
        "controller": controller.snapshot(),
    }


def resource_sample(root_pid, started):
    processes = _tree_processes(root_pid)
    rss = uss = vms = threads = 0
    pids = []
    for process in processes:
        try:
            memory = process.memory_info()
            rss += int(memory.rss); vms += int(memory.vms)
            try:
                uss += int(process.memory_full_info().uss)
            except (psutil.Error, AttributeError):
                pass
            threads += int(process.num_threads()); pids.append(int(process.pid))
        except psutil.Error:
            pass
    return {
        "schema": "r16_direct_target_selection_resource_sample_v1",
        "utc_timestamp": utc_now(), "monotonic_timestamp": time.monotonic(),
        "elapsed_seconds": time.monotonic() - started, "root_pid": root_pid,
        "pids": pids, "tree_rss_bytes": rss, "tree_uss_bytes": uss,
        "tree_vms_bytes": vms, "thread_count": threads,
        "system_available_memory_bytes": psutil.virtual_memory().available,
    }


def supervise(root, branch, *, branch_seconds=BRANCH_SECONDS):
    branch_root = Path(root) / "branches" / branch
    log = (branch_root / "worker.log").open("w", encoding="utf-8")
    command = [sys.executable, "-m", "scheduling_engine.benchmark_r16_direct_ia_vs_top_90m",
               "--worker", "--root", str(root), "--branch", branch]
    started = time.monotonic()
    process = subprocess.Popen(command, cwd=Path(__file__).parents[1],
                               stdin=subprocess.DEVNULL, stdout=log,
                               stderr=subprocess.STDOUT)
    samples = 0; warnings = 0; hard_stop = None
    resource_path = branch_root / "resource_samples.jsonl"
    deadline = started + branch_seconds + 180.0
    try:
        while process.poll() is None:
            now = time.monotonic()
            sample = resource_sample(process.pid, started)
            with resource_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(sample, sort_keys=True) + "\n")
                stream.flush(); os.fsync(stream.fileno())
            samples += 1
            available = sample["system_available_memory_bytes"]
            if available < 2 * 1024**3 or sample["tree_rss_bytes"] > 3 * 1024**3:
                warnings += 1
            if available < 1024**3 or sample["tree_rss_bytes"] > 4 * 1024**3:
                hard_stop = "resource_guard"
                terminate_tree(process.pid)
                break
            if now >= deadline:
                hard_stop = "supervisor_deadline"
                terminate_tree(process.pid)
                break
            time.sleep(min(5.0, max(0.1, deadline - now)))
    finally:
        if process.poll() is None:
            terminate_tree(process.pid)
        log.close()
    returncode = process.poll()
    event = {
        "schema": "r16_direct_target_selection_supervisor_v1",
        "branch": branch, "command": command,
        "started_at_utc": utc_now(), "elapsed_seconds": time.monotonic() - started,
        "returncode": returncode, "termination": hard_stop,
        "sample_count": samples, "warning_count": warnings,
    }
    atomic_json(branch_root / "supervisor_event.json", event)
    return event


def analyze(root):
    root = Path(root)
    analysis = {"schema": "r16_direct_ia_vs_top_analysis_v1", "branches": {}}
    for branch in BRANCHES:
        branch_root = root / "branches" / branch
        attempts = []
        for path in sorted((branch_root / "attempts").glob("attempt_*.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            row = dict(payload.get("attempt") or {})
            row["attempt_index"] = payload.get("attempt_index")
            attempts.append(row)
        analysis["branches"][branch] = {
            "attempt_count": len(attempts),
            "adoption_count": sum(bool(row.get("adopted")) for row in attempts),
            "total_authoritative_gain": sum(float(row.get("gain") or 0) for row in attempts),
            "total_candidate_discovery_gain": sum(float(row.get("candidate_substantive_value") or 0) for row in attempts),
            "attempts": attempts,
            "scope_count": len({tuple(row.get("actual_target_scope") or row.get("target_scope") or ()) for row in attempts}),
            "supervisor": json.loads((branch_root / "supervisor_event.json").read_text(encoding="utf-8")) if (branch_root / "supervisor_event.json").exists() else {},
        }
    ia = analysis["branches"][IA_BRANCH]
    top = analysis["branches"][TOP_BRANCH]
    analysis["ia_minus_top_authoritative_gain"] = ia["total_authoritative_gain"] - top["total_authoritative_gain"]
    analysis["no_alternative_schedule_outcome_inferred"] = True
    write_atomic(root / "analysis" / "trajectory_analysis.json", analysis)
    lines = [
        "# Dynamic IA versus TOP R16/S4 direct exact-v2 trajectory",
        "",
        "This report compares only authoritative, full-model-validated strict improvements actually produced by the two branches. Shadow targeting and selector traces do not imply alternative schedule outcomes.",
        "",
    ]
    for branch in BRANCHES:
        facts = analysis["branches"][branch]
        lines.append(f"- `{branch}`: {facts['attempt_count']} attempts, {facts['adoption_count']} adoptions, {facts['total_authoritative_gain']:.1f} v2 points.")
    (root / "report" / "study_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return analysis


def freeze_code(root):
    code = Path(root) / "code"
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    status = subprocess.check_output(["git", "status", "--short"], text=True)
    diff = subprocess.check_output(["git", "diff", "--binary", "HEAD"])
    write_atomic(code / "git_head.txt", (head + "\n").encode())
    write_atomic(code / "git_status.txt", status.encode())
    write_atomic(code / "tracked_diff.patch", diff)
    atomic_json(code / "identity.json", {
        "schema": "r16_direct_target_selection_code_identity_v1",
        "git_head": head, "tracked_diff_sha256": hashlib.sha256(diff).hexdigest(),
        "tracked_worktree_clean": not bool(status.strip()),
        "runner_sha256": sha256_file(Path(__file__)),
        "python": sys.version, "platform": platform.platform(),
        "psutil": psutil.__version__, "ortools": __import__("ortools").__version__,
    })


def prepare_root(root, lineage_id, confirm):
    root = create_lineage(
        root, lineage_id=lineage_id, confirm_lineage_id=confirm,
        historical_roots=tuple(path for path in OUTER_RESEARCH_ROOT.iterdir() if path.is_dir()),
        forbidden_roots=(Path(__file__).parents[1],),
    )
    # ``create_lineage`` is shared with the earlier R16/R4 study and creates
    # its historical branch names.  This study has its own immutable branch
    # identities; create them before any worker or supervisor can publish.
    for branch in BRANCHES:
        for relative in (
            f"branches/{branch}/attempts",
            f"branches/{branch}/checkpoints",
            f"branches/{branch}/selectors",
            f"branches/{branch}/targeting",
        ):
            (root / relative).mkdir(parents=True, exist_ok=False)
    freeze_code(root)
    data, source, manifest = prepare_target_source(root / "source" / "reference_target_common.json.gz")
    identity = {
        "source_sha256": sha256_file(root / "source" / "reference_target_common.json.gz"),
        "canonical_source_fingerprint": semantic_stage1_seed_source_fingerprint(data, source),
        "materialized_source_fingerprint": source_decision_fingerprint(source),
        "input_fingerprint": semantic_student_assignment_input_fingerprint(data),
        "model_fingerprint": EXPECTED_MODEL,
        "source_value": EXPECTED_VALUE, "assignment_count": EXPECTED_ASSIGNMENTS,
        "required_decision_group_count": EXPECTED_GROUPS,
        "special_commitment_count": EXPECTED_COMMITMENTS, "unmet_request_count": 0,
    }
    if identity["source_sha256"] != EXPECTED_SOURCE_SHA256 or identity["input_fingerprint"] != EXPECTED_INPUT:
        raise RuntimeError(f"frozen source identity mismatch: {identity}")
    atomic_json(root / "source" / "source_identity.json", identity)
    atomic_json(root / "experiment_contract.json", {
        "schema": SCHEMA, "lineage_id": lineage_id, "branches": BRANCHES,
        "preferred_order": BRANCHES, "operator": OPERATOR, "target_scope_size": 4,
        "radius": 16, "changed_student_cap": 4, "branch_seconds": BRANCH_SECONDS,
        "search_seconds": SEARCH_SECONDS, "validation_seconds": VALIDATION_SECONDS,
        "worker_count": WORKERS, "validation_worker_count": VALIDATION_WORKERS,
        "seed": SEED, "search_semantics": "direct_exact_v2_optimization",
        "objective_semantics": "v2_balanced_unchanged",
        "hints": "current_complete_incumbent_derived_unchanged",
        "strict_full_model_validation": True, "strict_improvement_adoption": True,
        "dynamic_retargeting": True, "production_wiring_changed": False,
        "source_manifest": manifest, "source_identity": identity,
        "no_shadow_schedule_outcome_inferred": True,
    })
    atomic_json(root / "preflight.json", {
        "schema": "r16_direct_target_selection_preflight_v1",
        "created_at_utc": utc_now(), "source_identity": identity,
        "available_memory_bytes": psutil.virtual_memory().available,
        "branches": BRANCHES, "no_experiment_cells_executed_before_launch": True,
    })
    return root


def seal(root):
    manifest = hash_tree(root)
    write_atomic(Path(root) / "artifact_hashes.sha256", manifest.encode("utf-8"))
    digest = sha256_file(Path(root) / "artifact_hashes.sha256")
    atomic_json(Path(root) / "SEALED", {
        "schema": "r16_direct_target_selection_seal_v1",
        "artifact_hashes_sha256": digest, "sealed_at_utc": utc_now(),
    })


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--launch-heavy", action="store_true")
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--analyze", action="store_true")
    parser.add_argument("--validate-endpoint", action="store_true")
    parser.add_argument("--root", type=Path)
    parser.add_argument("--branch", choices=BRANCHES)
    parser.add_argument("--lineage-id")
    parser.add_argument("--confirm-lineage-id")
    args = parser.parse_args(argv)
    if args.worker:
        if not args.root or not args.branch:
            parser.error("--worker requires --root and --branch")
        print(json.dumps(normalize(run_branch_worker(args.root, args.branch)), sort_keys=True))
        return 0
    if args.validate_endpoint:
        print(json.dumps(normalize(validate_endpoint_in_clean_process(root=args.root, branch_id=args.branch)), sort_keys=True))
        return 0
    if args.analyze:
        print(json.dumps(normalize(analyze(args.root)), sort_keys=True))
        return 0
    if not args.launch_heavy:
        parser.error("use --launch-heavy, --worker, --validate-endpoint, or --analyze")
    if not args.root or not args.lineage_id or args.confirm_lineage_id != args.lineage_id:
        parser.error("heavy launch requires --root, --lineage-id, and matching --confirm-lineage-id")
    if psutil.virtual_memory().available < 4 * 1024**3:
        raise RuntimeError("available memory below 4 GiB hard preflight minimum")
    root = prepare_root(args.root, args.lineage_id, args.confirm_lineage_id)
    for branch in BRANCHES:
        supervisor = supervise(root, branch)
        if supervisor["returncode"] != 0 or supervisor["termination"]:
            raise RuntimeError(f"branch failed or was terminated: {supervisor}")
        validation = validate_endpoint_in_clean_process(root=root, branch_id=branch)
        atomic_json(root / "branches" / branch / "final_validation.json", validation)
        if not endpoint_is_authoritative(validation):
            raise RuntimeError(f"branch endpoint is not authoritative: {validation}")
        if branch == IA_BRANCH:
            time.sleep(120.0)
    result = analyze(root)
    atomic_json(root / "analysis" / "execution_summary.json", {
        "schema": "r16_direct_ia_vs_top_execution_summary_v1", "analysis": result,
    })
    seal(root)
    print(json.dumps({"root": str(root), "status": "complete"}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
