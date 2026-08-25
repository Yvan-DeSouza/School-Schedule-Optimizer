"""Clean-process-friendly Stage 2 quality/runtime experiment helpers.

This module is deliberately diagnostic-only.  It prepares a detached DTO,
invokes the existing student-assignment engine, and returns normalized facts
for comparing solver horizons and incumbent strategies.  It does not change
the production entry point, objective definitions, or persisted workflow.
"""

from __future__ import annotations

from dataclasses import MISSING, dataclass, fields, is_dataclass, replace
from datetime import datetime, timezone
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import sys
import tempfile
from time import perf_counter

from .. import dto as _dto_module
from ..dto import StudentAssignmentInputDTO
from ..realistic_student_assignment_validation import (
    build_production_shaped_medium_fixture,
    summarize_production_shaped_medium_fixture,
)
from ..student_assignment.quality import evaluate_student_assignment_quality
from ..student_assignment.runtime import semantic_student_assignment_input_fingerprint
from .core import (
    run_student_assignment_adaptive_local_bootstrap_diagnostic,
    run_student_assignment_local_bootstrap_diagnostic,
    run_student_assignment_mature_local_search_diagnostic,
    run_student_assignment_stage2_diagnostic,
    run_substantive_soft_tier_probe,
)


STAGE1_SEED_SNAPSHOT_SCHEMA = "student_assignment_stage1_seed_v1"
STUDENT_ASSIGNMENT_INPUT_SNAPSHOT_SCHEMA = "student_assignment_input_v1"
DURABLE_STAGE2_BENCHMARK_SCHEMA = "student_assignment_stage2_benchmark_v1"
DURABLE_STAGE2_BENCHMARK_MANIFEST_SCHEMA = (
    "student_assignment_stage2_benchmark_manifest_v1"
)
MATURE_R2_CHECKPOINT_SCHEMA = "student_assignment_mature_r2_checkpoint_v1"


def _atomic_write_bytes(path, payload):
    """Replace a diagnostic artifact only after its complete bytes are durable.

    Mature-R2 checkpoints are the restart frontier.  A process interruption
    must therefore leave either the previous complete checkpoint or the new
    complete checkpoint, never a truncated file that looks readable.
    """

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def _dto_dataclass_types():
    return {
        name: value
        for name, value in vars(_dto_module).items()
        if isinstance(value, type) and is_dataclass(value)
    }


def _encode_snapshot_value(value):
    """Encode tuple-shaped source decisions into transparent JSON data."""

    if is_dataclass(value):
        return {
            "type": "dataclass",
            "class": type(value).__name__,
            "fields": {
                field.name: _encode_snapshot_value(getattr(value, field.name))
                for field in fields(value)
            },
        }
    if isinstance(value, tuple):
        return {
            "type": "tuple",
            "items": [_encode_snapshot_value(item) for item in value],
        }
    if isinstance(value, list):
        return {
            "type": "list",
            "items": [_encode_snapshot_value(item) for item in value],
        }
    if isinstance(value, dict):
        return {
            "type": "dict",
            "items": [
                {
                    "key": _encode_snapshot_value(key),
                    "value": _encode_snapshot_value(item),
                }
                for key, item in sorted(value.items(), key=lambda pair: repr(pair[0]))
            ],
        }
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise TypeError(f"Unsupported Stage 1 snapshot value: {type(value)!r}")


def _decode_snapshot_value(value):
    if not isinstance(value, dict) or "type" not in value:
        return value
    value_type = value["type"]
    if value_type == "tuple":
        return tuple(_decode_snapshot_value(item) for item in value["items"])
    if value_type == "list":
        return [_decode_snapshot_value(item) for item in value["items"]]
    if value_type == "dict":
        return {
            _decode_snapshot_value(item["key"]): _decode_snapshot_value(item["value"])
            for item in value["items"]
        }
    if value_type == "dataclass":
        dto_type = _dto_dataclass_types().get(value.get("class"))
        if dto_type is None:
            raise ValueError(
                f"Unsupported DTO snapshot class: {value.get('class')!r}"
            )
        encoded_fields = value.get("fields")
        if not isinstance(encoded_fields, dict):
            raise ValueError("DTO snapshot fields must be an object")
        dto_fields = {field.name: field for field in fields(dto_type)}
        unknown_fields = set(encoded_fields) - set(dto_fields)
        if unknown_fields:
            raise ValueError(
                f"DTO snapshot contains unknown {dto_type.__name__} fields: "
                f"{sorted(unknown_fields)!r}"
            )
        decoded_fields = {
            name: _decode_snapshot_value(item)
            for name, item in encoded_fields.items()
        }
        # Durable v1 artifacts predate additive DTO metadata such as the
        # objective-semantics version and canonical score mapping.  Rehydrate
        # omitted fields from their dataclass defaults so historical benchmark
        # fingerprints and source decisions remain readable. Required fields
        # still fail closed rather than being invented.
        for name, field in dto_fields.items():
            if name in decoded_fields:
                continue
            if field.default is not MISSING:
                decoded_fields[name] = field.default
            elif field.default_factory is not MISSING:
                decoded_fields[name] = field.default_factory()
            else:
                raise ValueError(
                    f"DTO snapshot is missing required {dto_type.__name__} field: {name}"
                )
        return dto_type(**decoded_fields)
    raise ValueError(f"Unsupported Stage 1 snapshot value type: {value_type!r}")


def stage1_seed_source_fingerprint(source_decisions):
    """Return a stable fingerprint for a semantic source-decision map."""

    encoded = _encode_snapshot_value(tuple(sorted(source_decisions, key=repr)))
    return hashlib.sha256(
        json.dumps(encoded, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def write_student_assignment_input_snapshot(path, *, data, input_fingerprint):
    """Write a temporary, transparent DTO replay snapshot for diagnostics."""

    actual_fingerprint = semantic_student_assignment_input_fingerprint(data)
    if actual_fingerprint != input_fingerprint:
        raise ValueError("Student-assignment input fingerprint is not current")
    payload = {
        "schema": STUDENT_ASSIGNMENT_INPUT_SNAPSHOT_SCHEMA,
        "input_semantic_fingerprint": actual_fingerprint,
        "dto": _encode_snapshot_value(data),
    }
    Path(path).write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return payload


def read_student_assignment_input_snapshot(path, *, expected_input_fingerprint=None):
    """Read and fingerprint-check one temporary DTO replay snapshot."""

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("schema") != STUDENT_ASSIGNMENT_INPUT_SNAPSHOT_SCHEMA:
        raise ValueError("Unsupported student-assignment input snapshot schema")
    stored_fingerprint = payload.get("input_semantic_fingerprint")
    if (
        expected_input_fingerprint is not None
        and stored_fingerprint != expected_input_fingerprint
    ):
        raise ValueError("Student-assignment input snapshot fingerprint does not match")
    data = _decode_snapshot_value(payload.get("dto"))
    if not isinstance(data, StudentAssignmentInputDTO):
        raise ValueError("Student-assignment input snapshot does not contain the expected DTO")
    actual_fingerprint = semantic_student_assignment_input_fingerprint(data)
    if actual_fingerprint != stored_fingerprint:
        raise ValueError("Student-assignment input snapshot fingerprint is invalid")
    return {
        "input_semantic_fingerprint": actual_fingerprint,
        "data": data,
    }


def _json_bytes(payload):
    return json.dumps(payload, indent=2, sort_keys=True).encode("utf-8") + b"\n"


def _gzip_bytes(payload):
    """Return deterministic gzip bytes for one transparent JSON payload."""

    output = io.BytesIO()
    with gzip.GzipFile(
        fileobj=output,
        mode="wb",
        filename="",
        mtime=0,
    ) as compressed:
        compressed.write(_json_bytes(payload))
    return output.getvalue()


def _artifact_metadata(relative_path, compressed_bytes, uncompressed_bytes):
    return {
        "path": relative_path,
        "sha256_compressed": hashlib.sha256(compressed_bytes).hexdigest(),
        "sha256_uncompressed": hashlib.sha256(uncompressed_bytes).hexdigest(),
        "compressed_bytes": len(compressed_bytes),
        "uncompressed_bytes": len(uncompressed_bytes),
    }


def _student_ids(data):
    return {
        item.student_id
        for item in (
            *data.requests,
            *data.fixed_enrollments,
            *data.schedule_commitment_requests,
            *data.fixed_schedule_commitments,
        )
    } | set(data.student_ids_with_alternate_requests)


def _input_snapshot_payload(data, input_fingerprint):
    return {
        "schema": STUDENT_ASSIGNMENT_INPUT_SNAPSHOT_SCHEMA,
        "input_semantic_fingerprint": input_fingerprint,
        "dto": _encode_snapshot_value(data),
    }


def _seed_snapshot_payload(data, input_fingerprint, seed):
    source_decisions = _canonical_seed_source_decisions(
        data,
        tuple(seed["seed_source_decisions"]),
    )
    return {
        "schema": STAGE1_SEED_SNAPSHOT_SCHEMA,
        "input_semantic_fingerprint": input_fingerprint,
        "seed_objective_vector": list(seed.get("seed_objective_vector", ())),
        "seed_source_decision_fingerprint": stage1_seed_source_fingerprint(
            source_decisions
        ),
        "seed_source_decisions": _encode_snapshot_value(source_decisions),
    }


def _read_input_snapshot_payload(payload, *, expected_input_fingerprint=None):
    if payload.get("schema") != STUDENT_ASSIGNMENT_INPUT_SNAPSHOT_SCHEMA:
        raise ValueError("Unsupported student-assignment input snapshot schema")
    stored_fingerprint = payload.get("input_semantic_fingerprint")
    if (
        expected_input_fingerprint is not None
        and stored_fingerprint != expected_input_fingerprint
    ):
        raise ValueError("Student-assignment input snapshot fingerprint does not match")
    data = _decode_snapshot_value(payload.get("dto"))
    if not isinstance(data, StudentAssignmentInputDTO):
        raise ValueError(
            "Student-assignment input snapshot does not contain the expected DTO"
        )
    actual_fingerprint = semantic_student_assignment_input_fingerprint(data)
    if actual_fingerprint != stored_fingerprint:
        raise ValueError("Student-assignment input snapshot fingerprint is invalid")
    return {
        "input_semantic_fingerprint": actual_fingerprint,
        "data": data,
    }


def _read_seed_snapshot_payload(payload, *, data, expected_input_fingerprint):
    if payload.get("schema") != STAGE1_SEED_SNAPSHOT_SCHEMA:
        raise ValueError("Unsupported Stage 1 seed snapshot schema")
    if payload.get("input_semantic_fingerprint") != expected_input_fingerprint:
        raise ValueError("Stage 1 seed snapshot input fingerprint does not match")
    canonical_source_decisions = tuple(
        _decode_snapshot_value(payload.get("seed_source_decisions"))
    )
    actual_fingerprint = stage1_seed_source_fingerprint(canonical_source_decisions)
    if actual_fingerprint != payload.get("seed_source_decision_fingerprint"):
        raise ValueError("Stage 1 seed snapshot source fingerprint is invalid")
    return {
        "input_semantic_fingerprint": expected_input_fingerprint,
        "seed_objective_vector": tuple(payload.get("seed_objective_vector", ())),
        "seed_source_decisions": _materialize_seed_source_decisions(
            data,
            canonical_source_decisions,
        ),
        "seed_source_decision_fingerprint": actual_fingerprint,
    }


def write_durable_stage2_benchmark(directory, *, data, seed, metadata=None):
    """Write one durable, transparent target-scale benchmark.

    The detached input and semantic Stage 1 seed remain separate artifacts so
    either can be inspected or validated independently.  Gzip is used only
    for storage efficiency; the payloads remain versioned JSON and never
    depend on ORM IDs or Python pickle state.
    """

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    input_fingerprint = semantic_student_assignment_input_fingerprint(data)
    if seed.get("input_semantic_fingerprint") not in (None, input_fingerprint):
        raise ValueError("Stage 1 seed belongs to a different input fingerprint")
    input_payload = _input_snapshot_payload(data, input_fingerprint)
    seed_payload = _seed_snapshot_payload(data, input_fingerprint, seed)
    input_uncompressed = _json_bytes(input_payload)
    seed_uncompressed = _json_bytes(seed_payload)
    input_compressed = _gzip_bytes(input_payload)
    seed_compressed = _gzip_bytes(seed_payload)
    input_path = directory / "input.json.gz"
    seed_path = directory / "stage1_seed.json.gz"
    manifest_path = directory / "manifest.json"
    input_path.write_bytes(input_compressed)
    seed_path.write_bytes(seed_compressed)

    seed_summary = dict(seed.get("seed_summary") or {})
    seed_components = dict(seed.get("seed_component_values") or {})
    source_decision_count = len(tuple(seed["seed_source_decisions"]))
    special_commitment_request_count = len(data.schedule_commitment_requests)
    special_commitment_count = special_commitment_request_count + sum(
        item.delivery_kind == "co_op" for item in data.requests
    )
    try:
        from ortools import __version__ as ortools_version
    except ImportError:  # pragma: no cover - only defensive metadata handling
        ortools_version = "unknown"
    manifest = {
        "schema": DURABLE_STAGE2_BENCHMARK_MANIFEST_SCHEMA,
        "benchmark_schema": DURABLE_STAGE2_BENCHMARK_SCHEMA,
        "benchmark_name": metadata.get("benchmark_name", directory.name)
        if metadata else directory.name,
        "input_semantic_fingerprint": input_fingerprint,
        "seed_source_decision_fingerprint": seed_payload[
            "seed_source_decision_fingerprint"
        ],
        "artifacts": {
            "input": _artifact_metadata(
                "input.json.gz", input_compressed, input_uncompressed
            ),
            "stage1_seed": _artifact_metadata(
                "stage1_seed.json.gz", seed_compressed, seed_uncompressed
            ),
        },
        "counts": {
            "student_count": len(_student_ids(data)),
            "request_count": len(data.requests),
            "required_source_decision_group_count": seed_summary.get(
                "source_decision_count", source_decision_count
            ),
            "normal_section_count": sum(
                item.section_id > 0 for item in data.sections
            ),
            "student_assignment_section_record_count": len(data.sections),
            "online_supervision_session_count": len(
                data.online_supervision_sessions
            ),
            "special_commitment_count": special_commitment_count,
            "special_commitment_request_count": special_commitment_request_count,
        },
        "stage1": {
            "objective_vector": list(seed.get("seed_objective_vector", ())),
            "substantive_components": seed_components,
            "source_decision_count": seed.get(
                "seed_assignment_count", source_decision_count
            ),
            "complete": bool(
                seed_summary.get("fulfillment_complete", seed.get("seed_validated"))
            ),
            "validated": bool(seed.get("seed_validated", True)),
        },
        "source": (metadata or {}).get("source", "synthetic production-scale fixture"),
        "solver": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "ortools": ortools_version,
        },
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def _read_gzip_json_artifact(directory, artifact_name, artifact_metadata):
    path = Path(directory) / artifact_metadata["path"]
    compressed = path.read_bytes()
    if hashlib.sha256(compressed).hexdigest() != artifact_metadata["sha256_compressed"]:
        raise ValueError(f"Durable benchmark artifact hash mismatch: {artifact_name}")
    with gzip.GzipFile(fileobj=io.BytesIO(compressed), mode="rb") as source:
        uncompressed = source.read()
    if hashlib.sha256(uncompressed).hexdigest() != artifact_metadata["sha256_uncompressed"]:
        raise ValueError(
            f"Durable benchmark uncompressed hash mismatch: {artifact_name}"
        )
    return json.loads(uncompressed.decode("utf-8"))


def read_durable_stage2_benchmark(directory):
    """Read and verify one durable benchmark and return ``manifest/data/seed``."""

    directory = Path(directory)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema") != DURABLE_STAGE2_BENCHMARK_MANIFEST_SCHEMA:
        raise ValueError("Unsupported durable Stage 2 benchmark manifest schema")
    if manifest.get("benchmark_schema") != DURABLE_STAGE2_BENCHMARK_SCHEMA:
        raise ValueError("Unsupported durable Stage 2 benchmark schema")
    input_payload = _read_gzip_json_artifact(
        directory, "input", manifest["artifacts"]["input"]
    )
    input_snapshot = _read_input_snapshot_payload(
        input_payload,
        expected_input_fingerprint=manifest["input_semantic_fingerprint"],
    )
    seed_payload = _read_gzip_json_artifact(
        directory, "stage1_seed", manifest["artifacts"]["stage1_seed"]
    )
    seed = _read_seed_snapshot_payload(
        seed_payload,
        data=input_snapshot["data"],
        expected_input_fingerprint=input_snapshot["input_semantic_fingerprint"],
    )
    if seed["seed_source_decision_fingerprint"] != manifest[
        "seed_source_decision_fingerprint"
    ]:
        raise ValueError("Durable benchmark Stage 1 seed fingerprint does not match")
    return {
        "manifest": manifest,
        "input_semantic_fingerprint": input_snapshot["input_semantic_fingerprint"],
        "data": input_snapshot["data"],
        "seed": seed,
    }


def write_mature_r2_checkpoint(
    path,
    *,
    data,
    source_decisions,
    result_facts,
    experiment_id,
    parent_benchmark_fingerprint=None,
):
    """Write one transparent, versioned mature-R2 source-decision checkpoint.

    This is a diagnostic artifact, not a replacement for the immutable run
    snapshot.  It stores semantic source decisions rather than solver objects
    or pickle state, so a later clean process can validate the checkpoint
    against the current detached DTO before using it as a diagnostic seed.
    """

    input_fingerprint = semantic_student_assignment_input_fingerprint(data)
    canonical = _canonical_seed_source_decisions(data, tuple(source_decisions))
    source_fingerprint = stage1_seed_source_fingerprint(canonical)
    facts = dict(result_facts or {})
    optimization = dict(facts.get("optimization_facts", {}))
    stage_2 = dict(optimization.get("stage_2", {}))
    local_bootstrap = dict(optimization.get("stage_2_local_bootstrap", {}))
    quality = dict(facts.get("quality", {}))
    substantive_components = dict(
        facts.get("substantive_components")
        or local_bootstrap.get("component_values")
        or stage_2.get("substantive_components", {})
    )
    complete = facts.get("status") == "complete"
    validated = bool(
        facts.get("seed_validated")
        or stage_2.get("alternate_seed_validated")
        or local_bootstrap.get("candidate_validated")
        or complete
    )
    payload = {
        "schema": MATURE_R2_CHECKPOINT_SCHEMA,
        "experiment_id": str(experiment_id),
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "input_semantic_fingerprint": input_fingerprint,
        "parent_benchmark_fingerprint": parent_benchmark_fingerprint,
        "parent_checkpoint_source_decision_fingerprint": parent_benchmark_fingerprint,
        "source_decision_fingerprint": source_fingerprint,
        "source_decisions": _encode_snapshot_value(canonical),
        "objective_vector": list(facts.get("objective_vector", stage_2.get("objective_values", ()))),
        "substantive_components": substantive_components,
        "quality": quality,
        "counts": {
            "assigned_request_count": facts.get("assignment_count"),
            "unmet_request_count": facts.get("unmet_request_count"),
            "special_commitment_count": facts.get(
                "special_commitment_count",
                (quality.get("request_fulfillment", {})
                 .get("special_commitments", {})
                 .get("fulfilled_count")),
            ),
            "source_decision_count": len(canonical),
        },
        "validation": {
            "seed_validated": validated,
            "complete": complete,
            "full_model_validation": validated,
        },
    }
    path = Path(path)
    if path.suffix == ".gz":
        encoded = _gzip_bytes(payload)
    else:
        encoded = _json_bytes(payload)
    _atomic_write_bytes(path, encoded)
    return payload


def read_mature_r2_checkpoint(path, *, expected_input_fingerprint=None):
    """Read and verify a mature-R2 checkpoint without trusting opaque state."""

    path = Path(path)
    if path.suffix == ".gz":
        with gzip.GzipFile(fileobj=io.BytesIO(path.read_bytes()), mode="rb") as source:
            payload = json.loads(source.read().decode("utf-8"))
    else:
        payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != MATURE_R2_CHECKPOINT_SCHEMA:
        raise ValueError("Unsupported mature R2 checkpoint schema")
    if (
        expected_input_fingerprint is not None
        and payload.get("input_semantic_fingerprint") != expected_input_fingerprint
    ):
        raise ValueError("Mature R2 checkpoint input fingerprint does not match")
    canonical = tuple(_decode_snapshot_value(payload.get("source_decisions")))
    actual_fingerprint = stage1_seed_source_fingerprint(canonical)
    if actual_fingerprint != payload.get("source_decision_fingerprint"):
        raise ValueError("Mature R2 checkpoint source fingerprint is invalid")
    return {
        **payload,
        "source_decisions": canonical,
        "source_decision_fingerprint": actual_fingerprint,
    }


def replay_mature_r2_checkpoint(data, checkpoint, config):
    """Run a diagnostic Stage-2 replay from a validated checkpoint seed."""

    input_fingerprint = semantic_student_assignment_input_fingerprint(data)
    if checkpoint.get("input_semantic_fingerprint") != input_fingerprint:
        raise ValueError("Mature R2 checkpoint belongs to a different input")
    source_decisions = _materialize_seed_source_decisions(
        data, tuple(checkpoint["source_decisions"])
    )
    if semantic_stage1_seed_source_fingerprint(data, source_decisions) != checkpoint[
        "source_decision_fingerprint"
    ]:
        raise ValueError("Mature R2 checkpoint cannot be materialized for this input")
    return run_stage2_experiment(
        data,
        config,
        alternate_source_decisions=source_decisions,
    )


def run_mature_r2_local_session(
    directory,
    *,
    checkpoint_path=None,
    max_iterations=64,
    per_probe_time_limit_seconds=600.0,
    total_time_limit_seconds=3600.0,
    worker_count=8,
    validation_time_limit_seconds=30.0,
    validation_worker_count=1,
    collect_resource_telemetry=True,
    persist_best_checkpoint=False,
    frontier_path=None,
    experiment_id=None,
):
    """Run one mature-R2 session without ordinary Stage 2 afterward.

    This is a diagnostic-only clean-process boundary. It loads and verifies
    the frozen benchmark/checkpoint once, performs one in-memory R2 descent,
    and returns bounded phase timings alongside the engine result. The
    ordinary lexicographic optimizer remains unchanged and is not invoked by
    this session.
    """

    timings = {}
    operation_started = perf_counter()

    started = perf_counter()
    benchmark = read_durable_stage2_benchmark(directory)
    timings["benchmark_load_seconds"] = perf_counter() - started

    started = perf_counter()
    checkpoint_destination = Path(
        checkpoint_path or Path(directory) / "mature_r2_checkpoint.json.gz"
    )
    checkpoint = read_mature_r2_checkpoint(
        checkpoint_destination,
        expected_input_fingerprint=benchmark["input_semantic_fingerprint"],
    )
    timings["checkpoint_load_seconds"] = perf_counter() - started

    started = perf_counter()
    source_decisions = _materialize_seed_source_decisions(
        benchmark["data"], tuple(checkpoint["source_decisions"])
    )
    timings["checkpoint_materialization_seconds"] = perf_counter() - started

    started = perf_counter()
    materialized_fingerprint = semantic_stage1_seed_source_fingerprint(
        benchmark["data"], source_decisions
    )
    timings["checkpoint_fingerprint_seconds"] = perf_counter() - started
    if materialized_fingerprint != checkpoint["source_decision_fingerprint"]:
        raise ValueError("Mature checkpoint fingerprint failed after materialization")

    started = perf_counter()
    result = run_student_assignment_mature_local_search_diagnostic(
        benchmark["data"],
        mature_source_decisions=source_decisions,
        max_iterations=max_iterations,
        per_probe_time_limit_seconds=per_probe_time_limit_seconds,
        total_time_limit_seconds=total_time_limit_seconds,
        worker_count=worker_count,
        hard_feasibility_validation_time_limit_seconds=validation_time_limit_seconds,
        hard_feasibility_validation_worker_count=validation_worker_count,
        capture_final_source_decisions=True,
        collect_resource_telemetry=collect_resource_telemetry,
    )
    timings["engine_local_session_seconds"] = perf_counter() - started

    started = perf_counter()
    quality = result.optimization_facts.get("quality", {})
    stage_2_facts = result.optimization_facts.get("stage_2", {})
    local_facts = result.optimization_facts.get("stage_2_local_bootstrap", {})
    final_source_decisions = stage_2_facts.get("final_source_decisions", ())
    local_component_values = dict(local_facts.get("component_values", {}))
    if not local_component_values:
        local_component_values = next(
            (
                dict(item.get("component_values", {}))
                for item in reversed(tuple(local_facts.get("iterations", ())))
                if item.get("component_values")
            ),
            {},
        )
    timings["result_reconstruction_seconds"] = perf_counter() - started

    facts = {
        "strategy": "mature_local_only",
        "input_semantic_fingerprint": benchmark["input_semantic_fingerprint"],
        "checkpoint_source_decision_fingerprint": checkpoint[
            "source_decision_fingerprint"
        ],
        "final_source_decisions": final_source_decisions,
        "status": result.status,
        "solver_outcome": result.solver_outcome,
        "stopping_reason": local_facts.get("stopping_reason"),
        "assignment_count": len(result.assignments),
        "unmet_request_count": len(result.unmet_requests),
        "special_commitment_count": len(result.commitment_assignments),
        "objective_vector": stage_2_facts.get("objective_values", ()),
        "quality": quality,
        "stage_1": dict(
            result.optimization_facts.get("stage_1", {})
        ),
        "stage_2": stage_2_facts,
        "local_bootstrap": local_facts,
        "timings": {
            **timings,
            "total_operation_seconds": perf_counter() - operation_started,
        },
        "result": result,
    }
    if persist_best_checkpoint:
        if not experiment_id:
            raise ValueError(
                "experiment_id is required when persisting a mature-R2 session"
            )
        if facts["status"] != "complete" or not facts["final_source_decisions"]:
            raise RuntimeError(
                "A mature-R2 checkpoint may only persist a complete final result"
            )
        checkpoint_write_started = perf_counter()
        persisted_payload = write_mature_r2_checkpoint(
            checkpoint_destination,
            data=benchmark["data"],
            source_decisions=facts["final_source_decisions"],
            result_facts={
                "status": facts["status"],
                "seed_validated": True,
                "objective_vector": facts["objective_vector"],
                "unmet_request_count": facts["unmet_request_count"],
                "assignment_count": facts["assignment_count"],
                "special_commitment_count": facts["special_commitment_count"],
                "substantive_components": dict(
                    local_component_values
                ),
                "quality": facts["quality"],
                "optimization_facts": result.optimization_facts,
            },
            experiment_id=experiment_id,
            parent_benchmark_fingerprint=checkpoint[
                "source_decision_fingerprint"
            ],
        )
        checkpoint_write_seconds = perf_counter() - checkpoint_write_started
        frontier_destination = Path(
            frontier_path
            or Path(directory) / "mature_r2_frontier.jsonl"
        )
        frontier_record = compact_mature_r2_session_record(
            facts,
            experiment_id=experiment_id,
            parent_checkpoint_source_fingerprint=checkpoint[
                "source_decision_fingerprint"
            ],
        )
        frontier_record.update({
            "checkpoint_updated": True,
            "resulting_checkpoint_source_decision_fingerprint": persisted_payload[
                "source_decision_fingerprint"
            ],
            "checkpoint_write_seconds": checkpoint_write_seconds,
            "session_elapsed_seconds": facts["timings"][
                "total_operation_seconds"
            ],
        })
        frontier_write_started = perf_counter()
        append_experiment_record(frontier_destination, frontier_record)
        frontier_write_seconds = perf_counter() - frontier_write_started
        facts["persistence"] = {
            "checkpoint_updated": True,
            "checkpoint_path": str(checkpoint_destination),
            "frontier_path": str(frontier_destination),
            "parent_checkpoint_source_decision_fingerprint": checkpoint[
                "source_decision_fingerprint"
            ],
            "resulting_checkpoint_source_decision_fingerprint": persisted_payload[
                "source_decision_fingerprint"
            ],
            "checkpoint_write_seconds": checkpoint_write_seconds,
            "frontier_write_seconds": frontier_write_seconds,
        }
    else:
        facts["persistence"] = {"checkpoint_updated": False}
    return facts


def compact_mature_r2_session_record(
    facts,
    *,
    experiment_id,
    parent_checkpoint_source_fingerprint,
):
    """Return bounded durable telemetry for one mature-local session."""

    result = facts["result"]
    local = dict(facts.get("local_bootstrap", {}))
    stage_1 = dict(facts.get("stage_1", {}))
    stage_2 = dict(facts.get("stage_2", {}))
    local_iterations = tuple(local.get("iterations", ()))
    local_component_values = dict(local.get("component_values", {}))
    if not local_component_values:
        local_component_values = next(
            (
                dict(item.get("component_values", {}))
                for item in reversed(local_iterations)
                if item.get("component_values")
            ),
            {},
        )
    final_source_decisions = tuple(facts.get("final_source_decisions", ()))
    final_substantive_value = (
        stage_2.get("objective_values", [None] * 5)[4]
        if stage_2.get("objective_values")
        else None
    )
    first_value = local.get("baseline_substantive_value")
    changed_students = local.get("changed_student_count", 0)
    return {
        "schema": "student_assignment_mature_r2_session_v1",
        "experiment_id": experiment_id,
        "parent_checkpoint_source_fingerprint": parent_checkpoint_source_fingerprint,
        "input_semantic_fingerprint": facts["input_semantic_fingerprint"],
        "start_substantive_value": first_value,
        "final_substantive_value": final_substantive_value,
        "status": result.status,
        "solver_outcome": result.solver_outcome,
        "stopping_reason": local.get("stopping_reason"),
        "candidate_validated": bool(
            local.get("candidate_validated", False)
            or result.status == "complete"
        ),
        "assignment_count": len(result.assignments),
        "unmet_request_count": len(result.unmet_requests),
        "special_commitment_count": len(result.commitment_assignments),
        "source_decision_count": len(final_source_decisions),
        "changed_source_decision_count": local.get(
            "changed_source_decision_count", 0
        ),
        "changed_student_count": changed_students,
        "iterations": local_iterations,
        "stage_1_timings": stage_1.get("timings", {}),
        "stage_2_timings": {
            "operation_wall_time_seconds": stage_2.get(
                "operation_wall_time_seconds"
            ),
            "post_local_optimization_wall_time_seconds": stage_2.get(
                "post_local_optimization_wall_time_seconds"
            ),
            "configured_deadline_seconds": stage_2.get(
                "configured_deadline_seconds"
            ),
            "deadline_remaining_seconds": stage_2.get(
                "deadline_remaining_seconds"
            ),
        },
        "optimization_passes": facts.get("result").optimization_facts.get(
            "optimization_passes", []
        ),
        "finalization_timings": facts.get("result").optimization_facts.get(
            "finalization_timings", {}
        ),
        "phase_timings": facts.get("timings", {}),
        "resource_monitor": facts.get("result").optimization_facts.get(
            "operation_resource_monitor", {}
        ),
        "local_resource_monitor": local.get("memory", {}),
        "objective_vector": stage_2.get("objective_values", ()),
        "component_values": dict(
            local_component_values
            or facts.get("quality", {}).get("stage_2", {}).get(
                "substantive_components", {}
            )
        ),
        "checkpoint_updated": False,
        "resulting_checkpoint_source_decision_fingerprint": None,
        "checkpoint_write_seconds": None,
        "session_elapsed_seconds": facts.get("timings", {}).get(
            "total_operation_seconds"
        ),
    }


def replay_durable_stage1_seed(
    directory,
    *,
    validation_time_limit_seconds=120.0,
    validation_worker_count=8,
):
    """Validate a frozen semantic seed against the current full model.

    The short feasibility allowance is intentional: this replay is checking
    the supplied seed, not creating a replacement seed.  CP-SAT still proves
    acceptance through the unchanged full model and its hard constraints.
    """

    benchmark = read_durable_stage2_benchmark(directory)
    seed = benchmark["seed"]
    probe = run_substantive_soft_tier_probe(
        benchmark["data"],
        threshold=None,
        time_limit_seconds=0.1,
        worker_count=1,
        alternate_source_decisions=seed["seed_source_decisions"],
        hard_feasibility_time_limit_seconds=0.1,
        hard_feasibility_validation_time_limit_seconds=(
            validation_time_limit_seconds
        ),
        hard_feasibility_worker_count=1,
        hard_feasibility_validation_worker_count=validation_worker_count,
    )
    materialized_seed_fingerprint = semantic_stage1_seed_source_fingerprint(
        benchmark["data"],
        probe.seed_source_decisions,
    ) if probe.seed_validated else None
    objective_matches = tuple(probe.seed_objective_vector) == tuple(
        seed["seed_objective_vector"]
    )
    fingerprint_matches = (
        materialized_seed_fingerprint
        == benchmark["manifest"]["seed_source_decision_fingerprint"]
    )
    return {
        "input_semantic_fingerprint": benchmark["input_semantic_fingerprint"],
        "seed_source_decision_fingerprint": materialized_seed_fingerprint,
        "seed_validated_against_full_model": bool(probe.seed_validated),
        "objective_vector": tuple(probe.seed_objective_vector),
        "objective_matches_manifest": objective_matches,
        "seed_fingerprint_matches_manifest": fingerprint_matches,
        "status": (
            "complete"
            if probe.seed_validated and objective_matches and fingerprint_matches
            else "failed"
        ),
        "probe_status": probe.status,
        "probe_seed_solver_outcome": probe.seed_solver_outcome,
        "validation_timings": dict(probe.timings),
    }


def _seed_identity_maps(data):
    """Build the same semantic ranks used by the input fingerprint."""

    def rank(values):
        return {value: index for index, value in enumerate(sorted(set(values)))}

    student_rank = rank(
        [item.student_id for item in data.requests]
        + [item.student_id for item in data.fixed_enrollments]
        + [item.student_id for item in data.schedule_commitment_requests]
        + [item.student_id for item in data.fixed_schedule_commitments]
        + list(data.student_ids_with_alternate_requests)
    )
    course_values = (
        [item.course_id for item in data.requests]
        + [course_id for item in data.sections for course_id in item.member_course_ids]
        + [item.course_id for item in data.course_difficulties]
        + [item.course_id for item in data.fixed_enrollments]
        + [
            item.course_id
            for item in data.fixed_schedule_commitments
            if item.course_id is not None
        ]
    )
    course_rank = rank(course_values)
    offering_rank = rank(
        [item.course_offering_id for item in data.requests]
        + [
            offering_id
            for item in data.sections
            for offering_id in item.member_course_offering_ids
        ]
        + [item.course_offering_id for item in data.fixed_enrollments]
        + [
            item.course_offering_id
            for item in data.fixed_schedule_commitments
            if item.course_offering_id is not None
        ]
    )
    teacher_rank = rank(
        [item.teacher_id for item in data.sections if item.teacher_id is not None]
        + [
            item.supervisor_id
            for item in data.online_supervision_sessions
            if item.supervisor_id is not None
        ]
        + [
            item.teacher_id
            for item in data.student_assignment_locks
            if item.teacher_id is not None
        ]
    )
    timeslot_rank = {
        item.id: index
        for index, item in enumerate(
            sorted(
                data.timeslots,
                key=lambda item: (item.semester, item.block, item.is_available, item.id),
            )
        )
    }
    timeslot_ids_by_rank = {
        value: key for key, value in timeslot_rank.items()
    }
    section_records = [
        (
            tuple(
                sorted(
                    offering_rank[value]
                    for value in item.member_course_offering_ids
                )
            ),
            tuple(sorted(course_rank[value] for value in item.member_course_ids)),
            item.semester,
            timeslot_rank.get(item.timeslot_id),
            item.capacity_max,
            item.target_capacity,
            item.half_semester_segment,
            item.half_semester_pair_key,
            teacher_rank.get(item.teacher_id),
            item.delivery_group_id,
            item.section_id,
        )
        for item in data.sections
    ]
    delivery_group_rank = rank(item[9] for item in section_records)
    section_rank = {
        item[10]: index
        for index, item in enumerate(
            sorted(
                section_records,
                key=lambda item: (
                    repr(item[:9]),
                    delivery_group_rank[item[9]],
                    item[10],
                ),
            )
        )
    }
    section_ids_by_rank = {
        value: key for key, value in section_rank.items()
    }
    request_records = [
        (
            student_rank[item.student_id],
            course_rank[item.course_id],
            offering_rank[item.course_offering_id],
            item.is_primary,
            item.is_mandatory,
            item.priority_tier,
            item.assignment_basis,
            item.delivery_kind,
            item.duration,
            item.credit_value,
            item.half_semester_segment,
            course_rank.get(item.paired_half_course_id),
        )
        for item in data.requests
    ]
    request_rank = {
        request.request_id: index
        for index, (request, _record) in enumerate(
            sorted(
                zip(data.requests, request_records),
                key=lambda pair: (pair[1], pair[0].request_id),
            )
        )
    }
    request_ids_by_rank = {
        value: key for key, value in request_rank.items()
    }
    commitment_rank = {
        item.request_id: index
        for index, item in enumerate(
            sorted(
                data.schedule_commitment_requests,
                key=lambda item: (
                    student_rank[item.student_id],
                    item.commitment_type,
                    item.is_in_scope,
                    item.request_id,
                ),
            )
        )
    }
    commitment_ids_by_rank = {
        value: key for key, value in commitment_rank.items()
    }
    online_records = [
        (
            item.semester,
            timeslot_rank.get(item.timeslot_id),
            item.capacity_max,
            item.target_capacity,
            teacher_rank.get(item.supervisor_id),
            item.is_in_scope,
            item.session_id,
        )
        for item in data.online_supervision_sessions
    ]
    online_rank = {
        item[6]: index
        for index, item in enumerate(
            sorted(online_records, key=lambda item: (repr(item[:6]), item[6]))
        )
    }
    online_ids_by_rank = {
        value: key for key, value in online_rank.items()
    }
    return {
        "student_rank": student_rank,
        "student_ids_by_rank": {value: key for key, value in student_rank.items()},
        "timeslot_rank": timeslot_rank,
        "timeslot_ids_by_rank": timeslot_ids_by_rank,
        "section_rank": section_rank,
        "section_ids_by_rank": section_ids_by_rank,
        "request_rank": request_rank,
        "request_ids_by_rank": request_ids_by_rank,
        "commitment_rank": commitment_rank,
        "commitment_ids_by_rank": commitment_ids_by_rank,
        "online_rank": online_rank,
        "online_ids_by_rank": online_ids_by_rank,
    }


def _canonical_seed_source_decisions(data, source_decisions):
    maps = _seed_identity_maps(data)
    requests_by_id = {item.request_id: item for item in data.requests}
    commitments_by_id = {
        item.request_id: item for item in data.schedule_commitment_requests
    }
    canonical = []
    for source_key, value in source_decisions:
        if source_key[0] == "course":
            request_id = source_key[1]
            request = requests_by_id[request_id]
            canonical_key = ("course", maps["request_rank"][request_id])
            canonical_value = (
                maps["student_rank"][value[0]],
                maps["section_rank"].get(value[1]),
                maps["online_rank"].get(value[2]),
                value[3],
                maps["timeslot_rank"].get(value[4]),
                value[5],
            )
        elif source_key[0] == "commitment":
            request_id = source_key[1]
            # Study/Focus assignments originate from a schedule-commitment
            # request. Co-op is represented as an academic course request but
            # is extracted as a commitment assignment, so its public source
            # key uses the course-request namespace. Preserve that distinction
            # in the snapshot; otherwise a large course request ID cannot be
            # looked up as a Study/Focus request during replay.
            if request_id in commitments_by_id:
                canonical_key = (
                    "commitment_request",
                    maps["commitment_rank"][request_id],
                )
            elif request_id in requests_by_id:
                canonical_key = (
                    "commitment_course",
                    maps["request_rank"][request_id],
                )
            else:
                raise ValueError(
                    "Commitment source decision references an unknown request: "
                    f"{request_id}."
                )
            canonical_value = (
                maps["student_rank"][value[0]],
                value[1],
                (
                    maps["request_rank"].get(value[2])
                    if value[2] is not None
                    else None
                ),
                tuple(
                    (maps["timeslot_rank"][slot], segment)
                    for slot, segment in value[3]
                ),
            )
        else:
            raise ValueError(f"Unsupported source decision kind: {source_key[0]!r}")
        canonical.append((canonical_key, canonical_value))
    return tuple(sorted(canonical, key=repr))


def _materialize_seed_source_decisions(data, canonical_source_decisions):
    maps = _seed_identity_maps(data)
    materialized = []
    for source_key, value in canonical_source_decisions:
        if source_key[0] == "course":
            raw_key = (
                "course",
                maps["request_ids_by_rank"][source_key[1]],
            )
            raw_value = (
                maps["student_ids_by_rank"][value[0]],
                (
                    maps["section_ids_by_rank"][value[1]]
                    if value[1] is not None
                    else None
                ),
                (
                    maps["online_ids_by_rank"][value[2]]
                    if value[2] is not None
                    else None
                ),
                value[3],
                (
                    maps["timeslot_ids_by_rank"][value[4]]
                    if value[4] is not None
                    else None
                ),
                value[5],
            )
        elif source_key[0] in {"commitment_request", "commitment_course"}:
            if source_key[0] == "commitment_request":
                raw_request_id = maps["commitment_ids_by_rank"][source_key[1]]
            else:
                raw_request_id = maps["request_ids_by_rank"][source_key[1]]
            raw_key = ("commitment", raw_request_id)
            raw_value = (
                maps["student_ids_by_rank"][value[0]],
                value[1],
                (
                    maps["request_ids_by_rank"][value[2]]
                    if value[2] is not None
                    else None
                ),
                tuple(
                    (maps["timeslot_ids_by_rank"][slot], segment)
                    for slot, segment in value[3]
                ),
            )
        else:
            raise ValueError(f"Unsupported source decision kind: {source_key[0]!r}")
        materialized.append((raw_key, raw_value))
    return tuple(sorted(materialized, key=repr))


def semantic_stage1_seed_source_fingerprint(data, source_decisions):
    return stage1_seed_source_fingerprint(
        _canonical_seed_source_decisions(data, source_decisions)
    )


def write_stage1_seed_snapshot(path, *, data, input_fingerprint, seed):
    """Write a versioned, transparent diagnostic seed snapshot."""

    source_decisions = _canonical_seed_source_decisions(
        data,
        tuple(seed["seed_source_decisions"]),
    )
    payload = {
        "schema": STAGE1_SEED_SNAPSHOT_SCHEMA,
        "input_semantic_fingerprint": input_fingerprint,
        "seed_objective_vector": list(seed.get("seed_objective_vector", ())),
        "seed_source_decision_fingerprint": stage1_seed_source_fingerprint(source_decisions),
        "seed_source_decisions": _encode_snapshot_value(source_decisions),
    }
    Path(path).write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return payload


def read_stage1_seed_snapshot(path, *, data, expected_input_fingerprint):
    """Load and validate one transparent diagnostic seed snapshot."""

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("schema") != STAGE1_SEED_SNAPSHOT_SCHEMA:
        raise ValueError("Unsupported Stage 1 seed snapshot schema")
    if payload.get("input_semantic_fingerprint") != expected_input_fingerprint:
        raise ValueError("Stage 1 seed snapshot input fingerprint does not match")
    canonical_source_decisions = tuple(
        _decode_snapshot_value(payload.get("seed_source_decisions"))
    )
    actual_fingerprint = stage1_seed_source_fingerprint(canonical_source_decisions)
    if actual_fingerprint != payload.get("seed_source_decision_fingerprint"):
        raise ValueError("Stage 1 seed snapshot source fingerprint is invalid")
    return {
        "input_semantic_fingerprint": expected_input_fingerprint,
        "seed_objective_vector": tuple(payload.get("seed_objective_vector", ())),
        "seed_source_decisions": _materialize_seed_source_decisions(
            data,
            canonical_source_decisions,
        ),
        "seed_source_decision_fingerprint": actual_fingerprint,
    }


@dataclass(frozen=True)
class Stage2ExperimentConfig:
    """One bounded, reproducible diagnostic trial configuration."""

    stage1_time_limit_seconds: float = 30.0
    stage1_validation_time_limit_seconds: float = 15.0
    stage1_worker_count: int = 8
    stage1_validation_worker_count: int = 8
    stage2_time_limit_seconds: float = 60.0
    stage2_worker_count: int = 8
    strategy: str = "ordinary"
    neighborhood_radius: int = 2
    adaptive_radii: tuple[int, ...] = (2, 4)
    max_iterations: int = 2
    per_probe_time_limit_seconds: float = 15.0
    timeline_max_events: int = 128
    max_changed_students: int | None = None
    collect_resource_telemetry: bool = True
    capture_final_source_decisions: bool = False


def run_stage2_experiment(
    data,
    config: Stage2ExperimentConfig,
    *,
    alternate_source_decisions=(),
    alternate_source_variable_values=None,
):
    """Run one diagnostic trial and return JSON-compatible facts.

    ``alternate_source_decisions`` and ``alternate_source_variable_values``
    are optional diagnostic-only inputs.  They let paired ordinary/retention
    trials start from the same already-validated Stage 1 source assignment,
    so differences are attributable to the Stage 2 policy rather than to a
    new parallel Stage 1 search result.
    """

    return _run_stage2_experiment(
        data,
        config,
        alternate_source_decisions=alternate_source_decisions,
        alternate_source_variable_values=alternate_source_variable_values,
    )


def _run_stage2_experiment(
    data,
    config: Stage2ExperimentConfig,
    *,
    alternate_source_decisions,
    alternate_source_variable_values,
):
    """Implementation shared by ordinary and paired diagnostic trials."""

    data = replace(data, time_limit_seconds=config.stage1_time_limit_seconds)
    input_fingerprint = semantic_student_assignment_input_fingerprint(data)
    started = perf_counter()

    common = {
        "hard_feasibility_time_limit_seconds": config.stage1_time_limit_seconds,
        "hard_feasibility_validation_time_limit_seconds": (
            config.stage1_validation_time_limit_seconds
        ),
        "hard_feasibility_worker_count": config.stage1_worker_count,
        "hard_feasibility_validation_worker_count": (
            config.stage1_validation_worker_count
        ),
        "optimization_worker_count": config.stage2_worker_count,
        "timeline_max_events": config.timeline_max_events,
        "collect_resource_telemetry": config.collect_resource_telemetry,
        "capture_final_source_decisions": config.capture_final_source_decisions,
        "alternate_source_decisions": alternate_source_decisions,
        "alternate_source_variable_values": alternate_source_variable_values,
    }
    engine_started = perf_counter()
    if config.strategy == "ordinary":
        result = run_student_assignment_stage2_diagnostic(
            data,
            total_time_limit_seconds=config.stage2_time_limit_seconds,
            retain_incumbent_on_non_improvement=False,
            **common,
        )
    elif config.strategy == "retention":
        result = run_student_assignment_stage2_diagnostic(
            data,
            total_time_limit_seconds=config.stage2_time_limit_seconds,
            retain_incumbent_on_non_improvement=True,
            **common,
        )
    elif config.strategy == "local":
        result = run_student_assignment_local_bootstrap_diagnostic(
            data,
            neighborhood_radius=config.neighborhood_radius,
            time_limit_seconds=config.per_probe_time_limit_seconds,
            total_time_limit_seconds=config.stage2_time_limit_seconds,
            worker_count=config.stage2_worker_count,
            hard_feasibility_time_limit_seconds=config.stage1_time_limit_seconds,
            hard_feasibility_validation_time_limit_seconds=(
                config.stage1_validation_time_limit_seconds
            ),
            hard_feasibility_worker_count=config.stage1_worker_count,
            hard_feasibility_validation_worker_count=(
                config.stage1_validation_worker_count
            ),
            alternate_source_decisions=alternate_source_decisions,
            alternate_source_variable_values=alternate_source_variable_values,
            timeline_max_events=config.timeline_max_events,
            max_changed_students=config.max_changed_students,
            collect_resource_telemetry=config.collect_resource_telemetry,
            capture_final_source_decisions=config.capture_final_source_decisions,
        )
    elif config.strategy == "adaptive":
        result = run_student_assignment_adaptive_local_bootstrap_diagnostic(
            data,
            neighborhood_radii=config.adaptive_radii,
            max_iterations=config.max_iterations,
            per_probe_time_limit_seconds=config.per_probe_time_limit_seconds,
            total_time_limit_seconds=config.stage2_time_limit_seconds,
            worker_count=config.stage2_worker_count,
            hard_feasibility_time_limit_seconds=config.stage1_time_limit_seconds,
            hard_feasibility_validation_time_limit_seconds=(
                config.stage1_validation_time_limit_seconds
            ),
            hard_feasibility_worker_count=config.stage1_worker_count,
            hard_feasibility_validation_worker_count=(
                config.stage1_validation_worker_count
            ),
            timeline_max_events=config.timeline_max_events,
            max_changed_students=config.max_changed_students,
            collect_resource_telemetry=config.collect_resource_telemetry,
            capture_final_source_decisions=config.capture_final_source_decisions,
        )
    else:
        raise ValueError(f"Unknown Stage 2 diagnostic strategy: {config.strategy}")
    engine_elapsed = perf_counter() - engine_started

    wrapper_quality_started = perf_counter()
    final_quality = evaluate_student_assignment_quality(
        data,
        assignments=result.assignments,
        commitment_assignments=result.commitment_assignments,
    )
    wrapper_quality_elapsed = perf_counter() - wrapper_quality_started
    wrapper_summary_started = perf_counter()
    facts = summarize_production_shaped_medium_fixture(data, result)
    wrapper_summary_elapsed = perf_counter() - wrapper_summary_started
    wrapper_packaging_started = perf_counter()
    facts.update({
        "strategy": config.strategy,
        "input_semantic_fingerprint": input_fingerprint,
        "elapsed_seconds": (
            engine_elapsed
            + wrapper_quality_elapsed
            + wrapper_summary_elapsed
        ),
        "objective_vector": list(
            result.optimization_facts.get("stage_2", {}).get(
                "objective_values", ()
            )
        ),
        "optimization_facts": result.optimization_facts,
        "quality": final_quality,
        "unmet_diagnostic_codes": sorted({
            item.diagnostic_code for item in result.unmet_requests
        }),
    })
    wrapper_packaging_elapsed = perf_counter() - wrapper_packaging_started
    facts["wrapper_timings"] = {
        "engine_result_seconds": engine_elapsed,
        "duplicate_quality_evaluation_seconds": wrapper_quality_elapsed,
        "production_shaped_summary_seconds": wrapper_summary_elapsed,
        "fact_packaging_seconds": wrapper_packaging_elapsed,
        "total_wrapper_seconds": perf_counter() - started,
    }
    return facts


def prepare_validated_stage1_seed(data, config: Stage2ExperimentConfig):
    """Prepare one validated Stage 1 source seed for paired diagnostics.

    The helper uses the existing CP-SAT substantive probe boundary only to
    obtain the validated seed facts.  It never creates a heuristic schedule
    and is not used by the ordinary production entry point.
    """

    data = replace(data, time_limit_seconds=config.stage1_time_limit_seconds)
    result = run_substantive_soft_tier_probe(
        data,
        threshold=None,
        time_limit_seconds=config.stage1_time_limit_seconds,
        worker_count=config.stage1_worker_count,
        hard_feasibility_time_limit_seconds=config.stage1_time_limit_seconds,
        hard_feasibility_validation_time_limit_seconds=(
            config.stage1_validation_time_limit_seconds
        ),
        hard_feasibility_worker_count=config.stage1_worker_count,
        hard_feasibility_validation_worker_count=(
            config.stage1_validation_worker_count
        ),
    )
    if not result.seed_validated:
        raise RuntimeError(
            "The diagnostic seed preparation did not produce a validated CP-SAT "
            f"seed (status={result.status}, "
            f"seed_solver_outcome={result.seed_solver_outcome}, "
            f"elapsed_seconds={result.elapsed_seconds:.3f}, "
            f"requested_time_limit_seconds={result.requested_time_limit_seconds:.3f})."
        )
    return {
        "input_semantic_fingerprint": semantic_student_assignment_input_fingerprint(data),
        "seed_objective_vector": result.seed_objective_vector,
        "seed_component_values": result.seed_component_values,
        "seed_assignment_count": result.seed_assignment_count,
        "seed_validated": result.seed_validated,
        "seed_source_decisions": result.seed_source_decisions,
        "seed_source_variable_values": result.seed_source_variable_values,
        "seed_source_decision_fingerprint": semantic_stage1_seed_source_fingerprint(
            data,
            result.seed_source_decisions,
        ),
        "seed_summary": result.seed_summary,
    }


def run_production_shaped_medium_experiment(
    *,
    student_count=120,
    config: Stage2ExperimentConfig | None = None,
):
    """Build the deterministic medium fixture and run one trial."""

    return run_stage2_experiment(
        build_production_shaped_medium_fixture(student_count=student_count),
        config or Stage2ExperimentConfig(),
    )


def run_strict_substantive_probe(data, config: Stage2ExperimentConfig):
    """Run a strict ``seed substantive value - 1`` diagnostic query."""

    data = replace(data, time_limit_seconds=config.stage1_time_limit_seconds)
    started = perf_counter()
    result = run_substantive_soft_tier_probe(
        data,
        threshold=None,
        strict_improvement=True,
        time_limit_seconds=config.stage2_time_limit_seconds,
        worker_count=config.stage2_worker_count,
        hard_feasibility_time_limit_seconds=config.stage1_time_limit_seconds,
        hard_feasibility_validation_time_limit_seconds=(
            config.stage1_validation_time_limit_seconds
        ),
        hard_feasibility_worker_count=config.stage1_worker_count,
        hard_feasibility_validation_worker_count=(
            config.stage1_validation_worker_count
        ),
    )
    return {
        "strategy": "strict_substantive_probe",
        "input_semantic_fingerprint": semantic_student_assignment_input_fingerprint(data),
        "elapsed_seconds": perf_counter() - started,
        "status": result.status,
        "seed_validated": result.seed_validated,
        "baseline_substantive_value": result.baseline_substantive_value,
        "requested_threshold": result.requested_threshold,
        "candidate_substantive_value": result.candidate_substantive_value,
        "complete_candidate_found": result.complete_candidate_found,
        "seed_assignment_count": result.seed_assignment_count,
        "candidate_assignment_count": result.candidate_assignment_count,
        "changed_source_decision_count": result.changed_source_decision_count,
        "changed_student_count": result.changed_student_count,
        "max_changed_students": result.max_changed_students,
        "seed_component_values": result.seed_component_values,
        "candidate_component_values": result.candidate_component_values,
        "component_deltas": result.component_deltas,
        "model_variable_count": result.model_variable_count,
        "model_constraint_count": result.model_constraint_count,
        "model_family_variable_counts": result.model_family_variable_counts,
        "conflicts": result.conflicts,
        "branches": result.branches,
        "solver_wall_time_seconds": result.solver_wall_time_seconds,
        "timings": result.timings,
    }


def compact_substantive_probe_record(
    result,
    *,
    experiment_id,
    input_semantic_fingerprint,
    seed_source_decision_fingerprint,
    radius=None,
    configured_time_limit_seconds=None,
    configured_worker_count=None,
    representative_memory_bytes=None,
    peak_memory_bytes=None,
):
    """Return bounded JSON facts for one diagnostic strict-improvement probe."""

    local_facts = getattr(result, "optimization_facts", {}).get(
        "stage_2_local_bootstrap", {}
    )
    memory = dict(local_facts.get("memory", {}))
    representative_memory_bytes = (
        representative_memory_bytes
        if representative_memory_bytes is not None
        else memory.get("representative_working_set_bytes")
    )
    peak_memory_bytes = (
        peak_memory_bytes
        if peak_memory_bytes is not None
        else memory.get("peak_working_set_bytes")
    )

    return {
        "experiment_id": experiment_id,
        "input_semantic_fingerprint": input_semantic_fingerprint,
        "seed_source_decision_fingerprint": seed_source_decision_fingerprint,
        "radius": radius,
        "configured_time_limit_seconds": configured_time_limit_seconds,
        "configured_worker_count": configured_worker_count,
        "start_substantive_value": result.baseline_substantive_value,
        "strict_threshold": result.requested_threshold,
        "status": result.status,
        "solver_outcome": result.status,
        "candidate_substantive_value": result.candidate_substantive_value,
        "candidate_component_values": dict(result.candidate_component_values),
        "candidate_validation_result": bool(result.complete_candidate_found),
        "candidate_adopted": False,
        "source_decision_hamming_distance": result.changed_source_decision_count,
        "affected_student_count": len(result.affected_student_ids),
        "affected_student_ids": list(result.affected_student_ids),
        "affected_section_count": len(result.affected_section_ids),
        "affected_section_ids": list(result.affected_section_ids),
        "best_bound": result.best_bound,
        "conflicts": result.conflicts,
        "branches": result.branches,
        "solver_wall_time_seconds": result.solver_wall_time_seconds,
        "operation_wall_time_seconds": result.timings.get(
            "operation_total_seconds", result.elapsed_seconds
        ),
        "representative_memory_bytes": representative_memory_bytes,
        "peak_memory_bytes": peak_memory_bytes,
        "memory": memory,
        "model_variable_count": result.model_variable_count,
        "model_constraint_count": result.model_constraint_count,
        "component_deltas": dict(result.component_deltas),
        "candidate_quality_summary": dict(result.candidate_quality_summary),
        "quality_comparison": dict(result.quality_comparison),
        "timings": dict(result.timings),
    }


def append_experiment_record(path, record):
    """Append one compact diagnostic record as a transparent JSONL row."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, sort_keys=True, default=str) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def run_component_minimum_probe(data, config: Stage2ExperimentConfig, component_name):
    """Minimize one existing substantive component for diagnosis only."""

    data = replace(data, time_limit_seconds=config.stage1_time_limit_seconds)
    started = perf_counter()
    result = run_substantive_soft_tier_probe(
        data,
        threshold=None,
        time_limit_seconds=config.stage2_time_limit_seconds,
        worker_count=config.stage2_worker_count,
        minimize_component=component_name,
        hard_feasibility_time_limit_seconds=config.stage1_time_limit_seconds,
        hard_feasibility_validation_time_limit_seconds=(
            config.stage1_validation_time_limit_seconds
        ),
        hard_feasibility_worker_count=config.stage1_worker_count,
        hard_feasibility_validation_worker_count=(
            config.stage1_validation_worker_count
        ),
    )
    return {
        "strategy": "component_minimum_probe",
        "component": component_name,
        "input_semantic_fingerprint": semantic_student_assignment_input_fingerprint(data),
        "elapsed_seconds": perf_counter() - started,
        "status": result.status,
        "seed_validated": result.seed_validated,
        "seed_value": result.seed_component_values.get(component_name),
        "best_value": result.minimized_component_value,
        "best_bound": result.best_bound,
        "candidate_found": result.complete_candidate_found,
        "candidate_assignment_count": result.candidate_assignment_count,
        "changed_source_decision_count": result.changed_source_decision_count,
        "component_deltas": result.component_deltas,
        "model_variable_count": result.model_variable_count,
        "model_constraint_count": result.model_constraint_count,
        "conflicts": result.conflicts,
        "branches": result.branches,
        "solver_wall_time_seconds": result.solver_wall_time_seconds,
        "timings": result.timings,
    }


if __name__ == "__main__":  # pragma: no cover - manual experiment surface
    import argparse
    import json

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--students", type=int, default=120)
    parser.add_argument("--strategy", choices=("ordinary", "retention", "local", "adaptive"), default="ordinary")
    parser.add_argument("--stage1-seconds", type=float, default=30.0)
    parser.add_argument("--stage1-validation-seconds", type=float, default=15.0)
    parser.add_argument("--stage2-seconds", type=float, default=60.0)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--radius", type=int, default=2)
    args = parser.parse_args()
    config = Stage2ExperimentConfig(
        stage1_time_limit_seconds=args.stage1_seconds,
        stage1_validation_time_limit_seconds=args.stage1_validation_seconds,
        stage2_time_limit_seconds=args.stage2_seconds,
        stage1_worker_count=args.workers,
        stage1_validation_worker_count=args.workers,
        stage2_worker_count=args.workers,
        strategy=args.strategy,
        neighborhood_radius=args.radius,
    )
    print(json.dumps(
        run_production_shaped_medium_experiment(
            student_count=args.students,
            config=config,
        ),
        indent=2,
        default=str,
    ))
