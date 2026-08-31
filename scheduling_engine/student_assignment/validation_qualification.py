"""Diagnostic paired validation qualification for exact full-model witnesses.

One operator solve supplies one semantic candidate and one in-process CP-SAT
witness to both validation paths.  The ordinary source-fixed validator remains
the authority; the witness result is shadow evidence only.  Raw auxiliary
values never leave the process or enter a durable record.
"""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from time import monotonic

from .runtime import ProcessResourceMonitor, semantic_student_assignment_input_fingerprint
from .solver import (
    model_proto_fingerprint,
    prepare_validation_context,
    validate_source_decision_candidate_with_status,
)
from .stage2_benchmark import (
    read_durable_stage2_benchmark,
    semantic_stage1_seed_source_fingerprint,
)


VALIDATION_QUALIFICATION_SCHEMA = "student_assignment_validation_qualification_v1"
VALIDATION_QUALIFICATION_MANIFEST_SCHEMA = (
    "student_assignment_validation_qualification_manifest_v1"
)
VALIDATION_ORDERS = ("normal_first", "witness_first")


def _source_fingerprint(data, source_decisions):
    try:
        return semantic_stage1_seed_source_fingerprint(data, source_decisions)
    except ValueError:
        encoded = repr(tuple(sorted(source_decisions or (), key=repr))).encode()
        return sha256(encoded).hexdigest()


def _verified_input_fingerprint(data, expected_input_fingerprint=None):
    computed = semantic_student_assignment_input_fingerprint(data)
    if expected_input_fingerprint is None:
        return computed, computed
    if computed == expected_input_fingerprint:
        return expected_input_fingerprint, computed
    legacy = semantic_student_assignment_input_fingerprint(
        data,
        include_extended_facts=False,
    )
    if legacy != expected_input_fingerprint:
        raise ValueError("Qualification input does not match its durable fingerprint")
    return expected_input_fingerprint, computed


def _validation_record(outcome, *, elapsed_seconds, resource):
    telemetry = dict(outcome.telemetry or {})
    phase_seconds = {
        name: telemetry.get(name)
        for name in (
            "model_fingerprint_wall_time_seconds",
            "clone_wall_time_seconds",
            "completion_constraint_wall_time_seconds",
            "source_fix_constraint_wall_time_seconds",
            "variable_freedom_accounting_wall_time_seconds",
            "solver_creation_wall_time_seconds",
            "cp_sat_solve_external_wall_time_seconds",
            "result_classification_wall_time_seconds",
        )
        if telemetry.get(name) is not None
    }
    accounted = sum(float(value) for value in phase_seconds.values())
    telemetry["phase_seconds"] = phase_seconds
    telemetry["accounted_phase_seconds"] = accounted
    telemetry["unattributed_phase_seconds"] = max(
        0.0,
        float(elapsed_seconds) - accounted,
    )
    return {
        "classification": outcome.classification,
        "solver_outcome": outcome.solver_outcome,
        "error": outcome.error,
        "elapsed_seconds": elapsed_seconds,
        "cp_sat_external_wall_seconds": telemetry.get(
            "cp_sat_solve_external_wall_time_seconds"
        ),
        "cp_sat_solver_wall_seconds": telemetry.get(
            "cp_sat_solver_wall_time_seconds"
        ),
        "preparation": {
            "model_fingerprint_seconds": telemetry.get(
                "model_fingerprint_wall_time_seconds"
            ),
            "clone_seconds": telemetry.get("clone_wall_time_seconds"),
            "completion_constraints_seconds": telemetry.get(
                "completion_constraint_wall_time_seconds"
            ),
            "source_or_witness_fix_seconds": telemetry.get(
                "source_fix_constraint_wall_time_seconds"
            ),
            "solver_creation_seconds": telemetry.get(
                "solver_creation_wall_time_seconds"
            ),
            "variable_freedom_accounting_seconds": telemetry.get(
                "variable_freedom_accounting_wall_time_seconds"
            ),
        },
        "phase_seconds": phase_seconds,
        "accounted_phase_seconds": accounted,
        "unattributed_phase_seconds": telemetry["unattributed_phase_seconds"],
        "model": {
            "variables_before": telemetry.get("model_variable_count_before"),
            "constraints_before": telemetry.get("model_constraint_count_before"),
            "variables_after_fixes": telemetry.get(
                "candidate_model_variable_count_after_fixes"
            ),
            "constraints_after_fixes": telemetry.get(
                "candidate_model_constraint_count_after_fixes"
            ),
        },
        "witness": dict(telemetry.get("witness") or {}),
        "prepared_context": dict(telemetry.get("prepared_context") or {}),
        "resource": dict(resource or {}),
    }


def _compact_operator_facts(local_result, *, data, parent_source_fingerprint):
    candidate_decisions = tuple(local_result.candidate_source_decisions or ())
    candidate_fingerprint = _source_fingerprint(data, candidate_decisions)
    summary = dict(local_result.candidate_summary or {})
    quality = dict(local_result.candidate_quality_summary or {})
    return {
        "parent_source_fingerprint": parent_source_fingerprint,
        "candidate_source_fingerprint": candidate_fingerprint,
        "candidate_source_decision_count": len(candidate_decisions),
        "changed_source_decision_count": local_result.changed_source_decision_count,
        "changed_student_count": local_result.changed_student_count,
        "candidate_substantive_value": local_result.candidate_substantive_value,
        "candidate_objective_vector": list(local_result.candidate_objective_vector),
        "candidate_component_values": dict(local_result.candidate_component_values),
        "component_deltas": dict(local_result.component_deltas),
        "assignment_count": summary.get(
            "assigned_request_count", local_result.candidate_assignment_count
        ),
        "unmet_request_count": 0 if summary.get("fulfillment_complete") else None,
        "special_commitment_count": summary.get("special_commitment_count"),
        "complete_candidate_found": bool(local_result.complete_candidate_found),
        "candidate_summary": summary,
        "candidate_quality_summary": quality,
    }


def compare_validation_classifications(
    model,
    required_decision_groups,
    source_variable_values,
    witness,
    *,
    time_limit_seconds=5.0,
    worker_count=1,
    expected_model_fingerprint=None,
):
    """Run ordinary and witness validation for one explicit candidate.

    This small differential utility is intended for the adversarial corpus. It
    does not authorize adoption and deliberately reports a witness false
    acceptance whenever the shadow path is more permissive than the current
    authority.
    """

    def run(*, use_witness):
        started = monotonic()
        outcome = validate_source_decision_candidate_with_status(
            model,
            required_decision_groups,
            source_variable_values,
            time_limit_seconds,
            worker_count=worker_count,
            random_seed=0,
            collect_validation_telemetry=True,
            base_model_variable_values=witness if use_witness else None,
            expected_base_model_fingerprint=(
                expected_model_fingerprint if use_witness else None
            ),
        )
        return _validation_record(
            outcome,
            elapsed_seconds=monotonic() - started,
            resource={},
        )

    ordinary = run(use_witness=False)
    witness_result = run(use_witness=True)
    return {
        "ordinary": ordinary,
        "witness": witness_result,
        "classification_parity": ordinary["classification"]
        == witness_result["classification"],
        "false_acceptance": (
            witness_result["classification"] == "validated"
            and ordinary["classification"] != "validated"
        ),
    }


def run_prepared_validation_sequence(
    model,
    required_decision_groups,
    source_variable_values,
    *,
    repetitions=5,
    time_limit_seconds=5.0,
    worker_count=1,
    collect_resource_telemetry=False,
):
    """Compare repeated ordinary validation with diagnostic prepared reuse.

    The same semantic source values are sent through both paths.  The
    prepared context is created once and contains only candidate-independent
    model/index state; each repetition still gets a fresh clone and solver.
    This helper is diagnostic infrastructure and never authorizes a result.
    """

    repetitions = max(1, int(repetitions))
    context_monitor = ProcessResourceMonitor(
        interval_seconds=0.10,
        enabled=collect_resource_telemetry,
    ).start()
    preparation_started = monotonic()
    try:
        context = prepare_validation_context(model, required_decision_groups)
    finally:
        context_resource = context_monitor.stop()
    context_creation_seconds = monotonic() - preparation_started

    def run(*, prepared):
        records = []
        for index in range(repetitions):
            monitor = ProcessResourceMonitor(
                interval_seconds=0.10,
                enabled=collect_resource_telemetry,
            ).start()
            started = monotonic()
            try:
                outcome = validate_source_decision_candidate_with_status(
                    model,
                    required_decision_groups,
                    source_variable_values,
                    time_limit_seconds,
                    worker_count=worker_count,
                    random_seed=0,
                    collect_validation_telemetry=True,
                    prepared_context=context if prepared else None,
                )
            finally:
                resource = monitor.stop()
            records.append({
                "index": index + 1,
                **_validation_record(
                    outcome,
                    elapsed_seconds=monotonic() - started,
                    resource=resource,
                ),
            })
        return records

    ordinary = run(prepared=False)
    prepared = run(prepared=True)
    ordinary_total = sum(float(item["elapsed_seconds"]) for item in ordinary)
    prepared_total = sum(float(item["elapsed_seconds"]) for item in prepared)
    prefix_amortized = {}
    for count in (1, 2, 3, 5):
        if count <= repetitions:
            prefix_amortized[str(count)] = {
                "ordinary_seconds": sum(
                    float(item["elapsed_seconds"])
                    for item in ordinary[:count]
                ),
                "prepared_seconds": (
                    context_creation_seconds
                    + sum(
                        float(item["elapsed_seconds"])
                        for item in prepared[:count]
                    )
                ),
                "ordinary_amortized_seconds": (
                    sum(
                        float(item["elapsed_seconds"])
                        for item in ordinary[:count]
                    ) / count
                ),
                "prepared_amortized_seconds": (
                    context_creation_seconds
                    + sum(
                        float(item["elapsed_seconds"])
                        for item in prepared[:count]
                    )
                ) / count,
            }
    return {
        "context_creation_seconds": context_creation_seconds,
        "context_resource": dict(context_resource or {}),
        "repetitions": repetitions,
        "ordinary": ordinary,
        "prepared": prepared,
        "ordinary_total_seconds": ordinary_total,
        "prepared_total_seconds": prepared_total,
        "ordinary_amortized_seconds": ordinary_total / repetitions,
        "prepared_amortized_seconds": (
            context_creation_seconds + prepared_total
        ) / repetitions,
        "prefix_amortized": prefix_amortized,
        "classification_parity": all(
            left["classification"] == right["classification"]
            for left, right in zip(ordinary, prepared)
        ),
        "false_acceptance": any(
            right["classification"] == "validated"
            and left["classification"] != "validated"
            for left, right in zip(ordinary, prepared)
        ),
    }


def run_prepared_validation_corpus(
    model,
    required_decision_groups,
    candidate_source_variable_values,
    *,
    time_limit_seconds=5.0,
    worker_count=1,
    collect_resource_telemetry=False,
):
    """Compare ordinary and prepared validation across distinct candidates.

    The corpus contains semantic source-variable maps only.  It deliberately
    does not retain solver objects or auxiliary witnesses.  A single prepared
    context is reused for the ordered candidate sequence, which models an
    incumbent transition without allowing candidate-specific state to enter
    the context.
    """

    context_started = monotonic()
    context = prepare_validation_context(model, required_decision_groups)
    context_creation_seconds = monotonic() - context_started
    records = []
    for index, source_values in enumerate(candidate_source_variable_values, 1):
        source_values = dict(source_values)
        results = {}
        for label, prepared in (("ordinary", False), ("prepared", True)):
            monitor = ProcessResourceMonitor(
                interval_seconds=0.10,
                enabled=collect_resource_telemetry,
            ).start()
            started = monotonic()
            try:
                outcome = validate_source_decision_candidate_with_status(
                    model,
                    required_decision_groups,
                    source_values,
                    time_limit_seconds,
                    worker_count=worker_count,
                    random_seed=0,
                    collect_validation_telemetry=True,
                    prepared_context=context if prepared else None,
                )
            finally:
                resource = monitor.stop()
            results[label] = _validation_record(
                outcome,
                elapsed_seconds=monotonic() - started,
                resource=resource,
            )
        records.append({
            "candidate_index": index,
            "ordinary": results["ordinary"],
            "prepared": results["prepared"],
            "classification_parity": (
                results["ordinary"]["classification"]
                == results["prepared"]["classification"]
            ),
            "false_acceptance": (
                results["prepared"]["classification"] == "validated"
                and results["ordinary"]["classification"] != "validated"
            ),
        })
    return {
        "candidate_count": len(records),
        "context_creation_seconds": context_creation_seconds,
        "records": records,
        "classification_parity": all(
            record["classification_parity"] for record in records
        ),
        "false_acceptance": any(
            record["false_acceptance"] for record in records
        ),
    }


def run_paired_validation_trial(
    data,
    *,
    initial_source_decisions,
    candidate_name="candidate",
    trial_id="trial",
    validation_order="normal_first",
    operator_family="targeted_r4_s2",
    selected_student_ids=(204, 604),
    operator_worker_count=1,
    probe_time_limit_seconds=300.0,
    session_time_limit_seconds=900.0,
    validation_time_limit_seconds=180.0,
    collect_resource_telemetry=True,
    expected_input_fingerprint=None,
    expected_parent_source_fingerprint=None,
):
    """Run one same-candidate ordinary/witness validation comparison.

    The operator callback is intentionally diagnostic-only.  It prevents the
    surrounding operator session from adopting the candidate while the
    callback runs both validators against the same base model, source map, and
    exact witness.  The callback stores compact facts only; raw witness values
    remain in process memory for the duration of the pair.
    """

    if validation_order not in VALIDATION_ORDERS:
        raise ValueError(f"Unsupported validation order: {validation_order}")
    input_fingerprint, computed_input_fingerprint = _verified_input_fingerprint(
        data,
        expected_input_fingerprint,
    )
    parent_source_fingerprint = (
        expected_parent_source_fingerprint
        or _source_fingerprint(data, initial_source_decisions)
    )
    captured = {}

    def capture_candidate(*, model, required_decision_groups, local_result):
        candidate_facts = _compact_operator_facts(
            local_result,
            data=data,
            parent_source_fingerprint=parent_source_fingerprint,
        )
        source_values = dict(local_result.candidate_source_variable_values or {})
        witness = dict(local_result.candidate_base_model_variable_values or {})
        base_fingerprint = local_result.candidate_base_model_fingerprint
        if (
            not candidate_facts["complete_candidate_found"]
            or not source_values
            or not witness
            or not base_fingerprint
        ):
            captured["pair"] = {
                "valid": False,
                "reason": local_result.candidate_base_model_witness_error
                or "complete candidate did not contain an exact base witness",
                "candidate": candidate_facts,
            }
            return

        def validate(*, use_witness):
            monitor = ProcessResourceMonitor(
                interval_seconds=0.10,
                enabled=collect_resource_telemetry,
            ).start()
            started = monotonic()
            try:
                outcome = validate_source_decision_candidate_with_status(
                    model,
                    required_decision_groups,
                    source_values,
                    validation_time_limit_seconds,
                    worker_count=operator_worker_count,
                    random_seed=0,
                    collect_validation_telemetry=True,
                    base_model_variable_values=witness if use_witness else None,
                    expected_base_model_fingerprint=(
                        base_fingerprint if use_witness else None
                    ),
                )
            finally:
                resource = monitor.stop()
            return _validation_record(
                outcome,
                elapsed_seconds=monotonic() - started,
                resource=resource,
            )

        results = {}
        for label in (
            ("ordinary", "witness")
            if validation_order == "normal_first"
            else ("witness", "ordinary")
        ):
            results[label] = validate(use_witness=label == "witness")

        ordinary = results["ordinary"]
        witness_result = results["witness"]
        captured["pair"] = {
            "valid": True,
            "candidate": candidate_facts,
            "model_fingerprint": model_proto_fingerprint(model),
            "base_model_variable_count": len(witness),
            "witness_coverage_count": len(witness),
            "validation_order": validation_order,
            "ordinary": ordinary,
            "witness": witness_result,
            "classification_parity": ordinary["classification"]
            == witness_result["classification"],
            "false_acceptance": (
                witness_result["classification"] == "validated"
                and ordinary["classification"] != "validated"
            ),
        }

    operator_started = monotonic()
    from .core import run_student_assignment_operator_session_diagnostic

    operator_result = run_student_assignment_operator_session_diagnostic(
        data,
        operator_family=operator_family,
        initial_source_decisions=initial_source_decisions,
        total_time_limit_seconds=session_time_limit_seconds,
        max_attempts=1,
        per_attempt_time_limit_seconds=probe_time_limit_seconds,
        worker_count=operator_worker_count,
        target_policy="fixed",
        selected_student_ids=tuple(selected_student_ids),
        hard_feasibility_validation_time_limit_seconds=validation_time_limit_seconds,
        hard_feasibility_validation_worker_count=operator_worker_count,
        candidate_validation_time_limit_seconds=validation_time_limit_seconds,
        capture_final_source_decisions=True,
        collect_resource_telemetry=collect_resource_telemetry,
        capture_candidate_base_model_witness=True,
        candidate_capture_callback=capture_candidate,
        skip_candidate_validation=True,
    )
    operator_facts = dict(operator_result.optimization_facts or {})
    local_facts = dict(operator_facts.get("stage_2_local_bootstrap") or {})
    pair = dict(captured.get("pair") or {
        "valid": False,
        "reason": "operator produced no complete candidate",
    })
    pair["operator_elapsed_seconds"] = monotonic() - operator_started
    pair["operator"] = {
        "status": operator_result.status,
        "solver_outcome": operator_result.solver_outcome,
        "stopping_reason": local_facts.get("stopping_reason"),
        "attempt_count": len(tuple(local_facts.get("iterations") or ())),
        "resource": dict(operator_facts.get("operation_resource_monitor") or {}),
    }
    pair["schema"] = VALIDATION_QUALIFICATION_SCHEMA
    pair["trial_id"] = str(trial_id)
    pair["candidate_name"] = candidate_name
    pair["input_fingerprint"] = input_fingerprint
    pair["computed_input_fingerprint"] = computed_input_fingerprint
    pair["operator_family"] = operator_family
    pair["configuration"] = {
        "operator_worker_count": operator_worker_count,
        "probe_time_limit_seconds": probe_time_limit_seconds,
        "session_time_limit_seconds": session_time_limit_seconds,
        "validation_time_limit_seconds": validation_time_limit_seconds,
    }
    pair["durable_witness_values"] = False
    return pair


def write_validation_qualification_record(path, record):
    """Write compact paired facts without any raw anonymous witness values."""

    record = dict(record)
    if record.get("durable_witness_values"):
        raise ValueError("Raw witnesses may not be written to qualification records")
    record.pop("artifact_hash", None)
    encoded = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
    record["artifact_hash"] = sha256(encoded).hexdigest()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(record, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    return record


def write_validation_qualification_manifest(path, *, record_paths, metadata=None):
    """Write a compact, hash-bound manifest for one qualification study."""

    artifacts = {}
    for record_path in sorted((Path(item) for item in record_paths), key=str):
        payload = record_path.read_bytes()
        artifacts[record_path.name] = {
            "path": record_path.name,
            "sha256": sha256(payload).hexdigest(),
            "bytes": len(payload),
        }
    manifest = {
        "schema": VALIDATION_QUALIFICATION_MANIFEST_SCHEMA,
        "study_schema": VALIDATION_QUALIFICATION_SCHEMA,
        "metadata": dict(metadata or {}),
        "artifacts": artifacts,
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def run_reference_target_paired_trial(
    benchmark_directory,
    *,
    output_path=None,
    **kwargs,
):
    """Load a verified durable target and run one paired qualification trial."""

    benchmark = read_durable_stage2_benchmark(benchmark_directory)
    record = run_paired_validation_trial(
        benchmark["data"],
        initial_source_decisions=benchmark["seed"]["seed_source_decisions"],
        expected_input_fingerprint=benchmark["input_semantic_fingerprint"],
        expected_parent_source_fingerprint=benchmark["manifest"][
            "seed_source_decision_fingerprint"
        ],
        **kwargs,
    )
    record["benchmark"] = {
        "directory": str(benchmark_directory),
        "manifest_schema": benchmark["manifest"]["schema"],
        "expected_input_fingerprint": benchmark["input_semantic_fingerprint"],
        "expected_seed_source_fingerprint": benchmark["manifest"][
            "seed_source_decision_fingerprint"
        ],
    }
    if output_path is not None:
        return write_validation_qualification_record(output_path, record)
    return record


if __name__ == "__main__":  # pragma: no cover - clean-process study surface
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark-directory", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--trial-id", required=True)
    parser.add_argument("--validation-order", choices=VALIDATION_ORDERS, required=True)
    parser.add_argument("--operator-family", default="targeted_r4_s2")
    parser.add_argument("--selected-student-ids", default="204,604")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--probe-seconds", type=float, default=300.0)
    parser.add_argument("--session-seconds", type=float, default=900.0)
    parser.add_argument("--validation-seconds", type=float, default=180.0)
    args = parser.parse_args()
    selected = tuple(
        int(value.strip())
        for value in args.selected_student_ids.split(",")
        if value.strip()
    )
    result = run_reference_target_paired_trial(
        args.benchmark_directory,
        output_path=args.output,
        trial_id=args.trial_id,
        validation_order=args.validation_order,
        operator_family=args.operator_family,
        selected_student_ids=selected,
        operator_worker_count=args.workers,
        probe_time_limit_seconds=args.probe_seconds,
        session_time_limit_seconds=args.session_seconds,
        validation_time_limit_seconds=args.validation_seconds,
    )
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
