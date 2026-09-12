"""Lossless, Django-free checkpoints for student-assignment research.

These artifacts are intentionally narrower than product persistence.  They
freeze one detached engine input and one independently validated source-decision
candidate so later analysis can replay *that* schedule without a database or a
fresh solve.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..dto import (
    StudentAssignmentDTO,
    StudentAssignmentInputDTO,
    StudentAssignmentResultDTO,
    StudentScheduleCommitmentAssignmentDTO,
)
from .core import run_student_assignment_source_decision_validation_diagnostic
from .runtime import semantic_student_assignment_input_fingerprint
from .stage2_benchmark import (
    _decode_snapshot_value,
    _encode_snapshot_value,
    semantic_stage1_seed_source_fingerprint,
)


FROZEN_INPUT_SCHEMA = "student_assignment_frozen_input_v1"
VALIDATED_STAGE1_SEED_SCHEMA = "student_assignment_validated_stage1_seed_v1"


def _canonical_json(payload):
    return json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"


def _write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_canonical_json(payload), encoding="utf-8")
    return payload


def _read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _source_decisions_from_result(result: StudentAssignmentResultDTO):
    """Return the validator's complete semantic source-decision namespace."""

    decisions = {}
    for item in result.assignments:
        decisions[("course", item.request_id)] = (
            item.student_id,
            item.section_id,
            item.online_supervision_session_id,
            item.semester,
            item.timeslot_id,
            item.half_semester_segment,
        )
    for item in result.commitment_assignments:
        decisions[("commitment", item.request_id)] = (
            item.student_id,
            item.commitment_kind,
            item.course_request_id,
            item.occupancy,
        )
    return tuple(sorted(decisions.items(), key=repr))


def serialize_frozen_student_assignment_input(path, *, data, provenance=None):
    """Write an immutable complete ``StudentAssignmentInputDTO`` snapshot."""

    if not isinstance(data, StudentAssignmentInputDTO):
        raise TypeError("data must be StudentAssignmentInputDTO")
    fingerprint = semantic_student_assignment_input_fingerprint(data)
    payload = {
        "schema": FROZEN_INPUT_SCHEMA,
        "input_semantic_fingerprint": fingerprint,
        "provenance": dict(provenance or {}),
        "dto": _encode_snapshot_value(data),
    }
    _write_json(path, payload)
    return payload


def load_frozen_student_assignment_input(path, *, expected_input_fingerprint=None):
    """Load and fail closed if a complete frozen input has drifted."""

    payload = _read_json(path)
    if payload.get("schema") != FROZEN_INPUT_SCHEMA:
        raise ValueError("Unsupported frozen student-assignment input schema")
    fingerprint = payload.get("input_semantic_fingerprint")
    if expected_input_fingerprint is not None and fingerprint != expected_input_fingerprint:
        raise ValueError("Frozen input fingerprint does not match expectation")
    data = _decode_snapshot_value(payload.get("dto"))
    if not isinstance(data, StudentAssignmentInputDTO):
        raise ValueError("Frozen input does not contain StudentAssignmentInputDTO")
    actual = semantic_student_assignment_input_fingerprint(data)
    if actual != fingerprint:
        raise ValueError("Frozen input semantic fingerprint is invalid")
    return {"data": data, "input_semantic_fingerprint": actual, "payload": payload}


def serialize_validated_stage1_seed(
    path,
    *,
    data,
    result: StudentAssignmentResultDTO,
    independent_full_model_validation=False,
    provenance=None,
):
    """Persist the exact candidate only after its caller has validated it.

    The artifact deliberately stores both source decisions (the authoritative
    replay transport) and the full extracted assignment/commitment ledger (the
    human/audit projection).  Neither is inferred on load.
    """

    if not independent_full_model_validation:
        raise ValueError("Only independently validated candidates may be frozen")
    if result.status != "complete" or result.unmet_requests:
        raise ValueError("Only complete zero-unmet candidates may be frozen")
    source_decisions = _source_decisions_from_result(result)
    expected_count = len(data.requests) + len(data.schedule_commitment_requests)
    if len(source_decisions) != expected_count:
        raise ValueError("Candidate does not include every completion decision")
    fingerprint = semantic_student_assignment_input_fingerprint(data)
    seed_fingerprint = semantic_stage1_seed_source_fingerprint(data, source_decisions)
    payload = {
        "schema": VALIDATED_STAGE1_SEED_SCHEMA,
        "input_semantic_fingerprint": fingerprint,
        "seed_source_decision_fingerprint": seed_fingerprint,
        "provenance": dict(provenance or {}),
        "independent_full_model_validation": True,
        "counts": {
            "assignment_count": len(result.assignments),
            "completion_group_count": expected_count,
            "unmet_mandatory_count": sum(item.is_mandatory for item in result.unmet_requests),
            "special_commitment_count": len(result.commitment_assignments),
            "source_decision_count": len(source_decisions),
        },
        "source_decisions": _encode_snapshot_value(source_decisions),
        "assignments": _encode_snapshot_value(tuple(result.assignments)),
        "commitment_assignments": _encode_snapshot_value(
            tuple(result.commitment_assignments)
        ),
    }
    _write_json(path, payload)
    return payload


def load_validated_stage1_seed(path, *, data, expected_input_fingerprint=None):
    """Load a seed and prove its decision identity matches the frozen input."""

    payload = _read_json(path)
    if payload.get("schema") != VALIDATED_STAGE1_SEED_SCHEMA:
        raise ValueError("Unsupported validated Stage-1 seed schema")
    if payload.get("independent_full_model_validation") is not True:
        raise ValueError("Validated seed lacks independent full-model validation")
    input_fingerprint = semantic_student_assignment_input_fingerprint(data)
    stored_input_fingerprint = payload.get("input_semantic_fingerprint")
    if expected_input_fingerprint is not None and stored_input_fingerprint != expected_input_fingerprint:
        raise ValueError("Validated seed input fingerprint does not match expectation")
    if input_fingerprint != stored_input_fingerprint:
        raise ValueError("Validated seed belongs to a different frozen input")
    source_decisions = tuple(_decode_snapshot_value(payload.get("source_decisions")))
    actual_seed_fingerprint = semantic_stage1_seed_source_fingerprint(data, source_decisions)
    if actual_seed_fingerprint != payload.get("seed_source_decision_fingerprint"):
        raise ValueError("Validated seed source-decision fingerprint is invalid")
    assignments = tuple(_decode_snapshot_value(payload.get("assignments")))
    commitments = tuple(_decode_snapshot_value(payload.get("commitment_assignments")))
    if not all(isinstance(item, StudentAssignmentDTO) for item in assignments):
        raise ValueError("Validated seed assignment ledger is invalid")
    if not all(isinstance(item, StudentScheduleCommitmentAssignmentDTO) for item in commitments):
        raise ValueError("Validated seed commitment ledger is invalid")
    counts = dict(payload.get("counts") or {})
    if counts.get("assignment_count") != len(assignments):
        raise ValueError("Validated seed assignment count is invalid")
    if counts.get("special_commitment_count") != len(commitments):
        raise ValueError("Validated seed commitment count is invalid")
    if counts.get("source_decision_count") != len(source_decisions):
        raise ValueError("Validated seed decision count is invalid")
    return {
        "input_semantic_fingerprint": input_fingerprint,
        "seed_source_decision_fingerprint": actual_seed_fingerprint,
        "source_decisions": source_decisions,
        "assignments": assignments,
        "commitment_assignments": commitments,
        "counts": counts,
        "payload": payload,
    }


def validate_loaded_stage1_seed(data, seed, *, time_limit_seconds=60.0, worker_count=8):
    """Replay a loaded seed through the canonical full-model validator only."""

    result = run_student_assignment_source_decision_validation_diagnostic(
        data,
        source_decisions=seed["source_decisions"],
        time_limit_seconds=time_limit_seconds,
        worker_count=worker_count,
        capture_final_source_decisions=True,
        collect_resource_telemetry=False,
    )
    stage_2 = dict((result.optimization_facts or {}).get("stage_2") or {})
    replayed = _source_decisions_from_result(result)
    replayed_fingerprint = semantic_stage1_seed_source_fingerprint(data, replayed)
    valid = (
        bool(stage_2.get("alternate_seed_validated"))
        and result.status == "complete"
        and not result.unmet_requests
        and replayed_fingerprint == seed["seed_source_decision_fingerprint"]
    )
    return {
        "valid": valid,
        "result": result,
        "replayed_seed_source_decision_fingerprint": replayed_fingerprint,
        "assignment_count": len(result.assignments),
        "special_commitment_count": len(result.commitment_assignments),
        "unmet_mandatory_count": sum(item.is_mandatory for item in result.unmet_requests),
        "independent_full_model_validation": bool(stage_2.get("alternate_seed_validated")),
    }
