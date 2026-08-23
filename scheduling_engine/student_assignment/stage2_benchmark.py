"""Clean-process-friendly Stage 2 quality/runtime experiment helpers.

This module is deliberately diagnostic-only.  It prepares a detached DTO,
invokes the existing student-assignment engine, and returns normalized facts
for comparing solver horizons and incumbent strategies.  It does not change
the production entry point, objective definitions, or persisted workflow.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass, replace
import hashlib
import json
from pathlib import Path
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
    run_student_assignment_stage2_diagnostic,
    run_substantive_soft_tier_probe,
)


STAGE1_SEED_SNAPSHOT_SCHEMA = "student_assignment_stage1_seed_v1"
STUDENT_ASSIGNMENT_INPUT_SNAPSHOT_SCHEMA = "student_assignment_input_v1"


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
        allowed_fields = {field.name for field in fields(dto_type)}
        if set(encoded_fields) != allowed_fields:
            raise ValueError(
                f"DTO snapshot fields do not match {dto_type.__name__}"
            )
        return dto_type(**{
            name: _decode_snapshot_value(item)
            for name, item in encoded_fields.items()
        })
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
        "alternate_source_decisions": alternate_source_decisions,
        "alternate_source_variable_values": alternate_source_variable_values,
    }
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
        )
    else:
        raise ValueError(f"Unknown Stage 2 diagnostic strategy: {config.strategy}")

    final_quality = evaluate_student_assignment_quality(
        data,
        assignments=result.assignments,
        commitment_assignments=result.commitment_assignments,
    )
    facts = summarize_production_shaped_medium_fixture(data, result)
    facts.update({
        "strategy": config.strategy,
        "input_semantic_fingerprint": input_fingerprint,
        "elapsed_seconds": perf_counter() - started,
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
