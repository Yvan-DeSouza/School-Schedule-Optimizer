"""Lossless, Django-free section-placement research checkpoints."""

from __future__ import annotations

import hashlib
import json
from dataclasses import fields, is_dataclass
from pathlib import Path

from .dto import PlacementAssignmentDTO, PlacementInputDTO, PlacementResultDTO
from .student_assignment.stage2_benchmark import (
    _decode_snapshot_value,
    _encode_snapshot_value,
)


PLACEMENT_INPUT_SCHEMA = "section_placement_frozen_input_v1"
PLACEMENT_RESULT_SCHEMA = "section_placement_result_v1"


def _write(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _fingerprint(payload):
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _ranks(values):
    return {value: index for index, value in enumerate(sorted(set(values)))}


def _semantic_context(data):
    course_rank = _ranks(
        list(course_id for unit in data.units for course_id in unit.member_course_ids)
        + [item.course_a_id for item in data.conflicts]
        + [item.course_b_id for item in data.conflicts]
        + [item.course_id for item in data.online_supervision_demands]
        + [item.course_id for item in data.student_timetable_demands]
    )
    slot_rows = {
        slot.id: (slot.semester, slot.block, slot.is_available)
        for slot in data.timeslots
    }
    slot_rank = {
        slot_id: index
        for index, (slot_id, _row) in enumerate(
            sorted(slot_rows.items(), key=lambda item: (item[1], item[0]))
        )
    }
    student_rank = _ranks(
        [item.student_id for item in data.online_supervision_demands]
        + [item.student_id for item in data.student_timetable_demands]
    )
    request_rank = _ranks(
        [item.request_id for item in data.online_supervision_demands]
        + [item.request_id for item in data.student_timetable_demands]
    )
    group_signatures = {
        unit.delivery_group_id: (
            tuple(sorted(course_rank[course_id] for course_id in unit.member_course_ids)),
            unit.capacity_max,
        )
        for unit in data.units
        if unit.delivery_group_id >= 0
    }
    group_rank = {
        group_id: index
        for index, (group_id, _signature) in enumerate(
            sorted(group_signatures.items(), key=lambda item: (item[1], item[0]))
        )
    }
    session_rows = {
        item.session_id: (
            item.capacity_max,
            tuple(item.allowed_semesters),
            slot_rank.get(item.fixed_timeslot_id),
        )
        for item in data.online_supervision_sessions
    }
    session_rank = {
        session_id: index
        for index, (session_id, _row) in enumerate(
            sorted(session_rows.items(), key=lambda item: (item[1], item[0]))
        )
    }
    teacher_rows = {
        teacher.id: (
            tuple(sorted(course_rank.get(course_id) for course_id in teacher.eligible_course_ids)),
            teacher.remaining_semester_1,
            teacher.remaining_semester_2,
            teacher.remaining_annual,
            tuple(sorted(slot_rank.get(slot_id) for slot_id in teacher.unavailable_timeslot_ids)),
        )
        for teacher in data.teachers
    }
    teacher_rank = {
        teacher_id: index
        for index, (teacher_id, _row) in enumerate(
            sorted(teacher_rows.items(), key=lambda item: (item[1], item[0]))
        )
    }
    unit_stable = {}
    anonymous_unit_counts = {}
    for unit in data.units:
        if unit.online_supervision_session_id is not None:
            stable = f"online:{session_rank[unit.online_supervision_session_id]}"
        elif unit.annual_index is not None:
            stable = f"annual:{group_rank[unit.delivery_group_id]}:{unit.annual_index}"
        else:
            signature = (
                tuple(sorted(course_rank[course_id] for course_id in unit.member_course_ids)),
                tuple(unit.allowed_semesters),
                unit.fixed_semester,
                slot_rank.get(unit.locked_timeslot_id),
                unit.source_mode,
                unit.capacity_max,
            )
            ordinal = anonymous_unit_counts.get(signature, 0)
            anonymous_unit_counts[signature] = ordinal + 1
            stable = f"unit:{signature!r}:{ordinal}"
        unit_stable[unit.key] = stable
    shared_rows = {}
    for unit in data.units:
        if unit.shared_placement_key is not None:
            shared_rows.setdefault(unit.shared_placement_key, []).append(
                unit_stable[unit.key]
            )
    shared_items = sorted(
        (key, tuple(sorted(members))) for key, members in shared_rows.items()
    )
    shared_rank = {key: index for index, (key, _members) in enumerate(shared_items)}
    staffing_rows = {}
    for unit in data.units:
        if unit.shared_staffing_key is not None:
            staffing_rows.setdefault(unit.shared_staffing_key, []).append(
                unit_stable[unit.key]
            )
    staffing_items = sorted(
        (key, tuple(sorted(members))) for key, members in staffing_rows.items()
    )
    staffing_rank = {key: index for index, (key, _members) in enumerate(staffing_items)}
    return {
        "course_rank": course_rank,
        "slot_rank": slot_rank,
        "group_rank": group_rank,
        "session_rank": session_rank,
        "teacher_rank": teacher_rank,
        "unit_stable": unit_stable,
        "shared_rank": shared_rank,
        "staffing_rank": staffing_rank,
        "student_rank": student_rank,
        "request_rank": request_rank,
    }


def _stable_input_payload(data):
    context = _semantic_context(data)
    course_rank = context["course_rank"]
    slot_rank = context["slot_rank"]
    group_rank = context["group_rank"]
    session_rank = context["session_rank"]
    teacher_rank = context["teacher_rank"]
    shared_rank = context["shared_rank"]
    staffing_rank = context["staffing_rank"]
    student_rank = context["student_rank"]
    request_rank = context["request_rank"]
    unit_stable = context["unit_stable"]
    return {
        "academic_year_shape": (
            "two_semester",
            tuple(sorted(
                (slot.semester, slot.block, bool(slot.is_available))
                for slot in data.timeslots
            )),
        ),
        "input_mode": data.input_mode,
        "units": sorted(
            (
                unit_stable[unit.key],
                group_rank.get(unit.delivery_group_id),
                tuple(sorted(course_rank[course_id] for course_id in unit.member_course_ids)),
                tuple(unit.allowed_semesters),
                unit.fixed_semester,
                slot_rank.get(unit.locked_timeslot_id),
                teacher_rank.get(unit.locked_teacher_id),
                unit.annual_index,
                unit.source_mode,
                unit.requires_course_qualification,
                session_rank.get(unit.online_supervision_session_id),
                shared_rank.get(unit.shared_placement_key),
                staffing_rank.get(unit.shared_staffing_key),
                unit.capacity_max,
            )
            for unit in data.units
        ),
        "fixed_placements": sorted(
            (
                tuple(sorted(course_rank.get(course_id) for course_id in item.member_course_ids)),
                slot_rank.get(item.timeslot_id),
                teacher_rank.get(item.teacher_id),
                session_rank.get(item.online_supervision_session_id),
                item.capacity_max,
            )
            for item in data.fixed_placements
        ),
        "timeslots": tuple(sorted(
            (slot_rank[slot.id], slot.semester, slot.block, bool(slot.is_available))
            for slot in data.timeslots
        )),
        "teachers": sorted(
            (
                teacher_rank[teacher.id],
                tuple(sorted(course_rank.get(course_id) for course_id in teacher.eligible_course_ids)),
                teacher.remaining_semester_1,
                teacher.remaining_semester_2,
                teacher.remaining_annual,
                tuple(sorted(slot_rank.get(slot_id) for slot_id in teacher.unavailable_timeslot_ids)),
            )
            for teacher in data.teachers
        ),
        "conflicts": sorted(
            (course_rank[item.course_a_id], course_rank[item.course_b_id], item.weight,
             item.estimated_retained_co_request_count)
            for item in data.conflicts
        ),
        "online_sessions": sorted(
            (session_rank[item.session_id], item.capacity_max,
             tuple(item.allowed_semesters), slot_rank.get(item.fixed_timeslot_id))
            for item in data.online_supervision_sessions
        ),
        "online_demands": sorted(
            (request_rank[item.request_id], student_rank[item.student_id],
             course_rank[item.course_id], tuple(item.allowed_semesters))
            for item in data.online_supervision_demands
        ),
        "student_timetable_demands": sorted(
            (request_rank[item.request_id], student_rank[item.student_id],
             course_rank[item.course_id], tuple(item.allowed_semesters), item.duration,
             course_rank.get(item.paired_half_course_id))
            for item in data.student_timetable_demands
        ),
        "time_limit_seconds": data.time_limit_seconds,
    }


def placement_input_semantic_fingerprint(data):
    return _fingerprint(_stable_input_payload(data))


def _result_semantic_payload(data, result):
    context = _semantic_context(data)
    slot_rank = context["slot_rank"]
    group_rank = context["group_rank"]
    session_rank = context["session_rank"]
    shared_rank = context["shared_rank"]
    stable_units = context["unit_stable"]
    unit_by_key = {item.key: item for item in data.units}
    assignments = []
    for item in result.assignments:
        unit = unit_by_key.get(item.unit_key)
        assignments.append((
            stable_units.get(item.unit_key, item.unit_key),
            group_rank.get(item.delivery_group_id),
            item.semester,
            slot_rank.get(item.timeslot_id),
            item.block,
            item.annual_index,
            session_rank.get(item.online_supervision_session_id),
            shared_rank.get(unit.shared_placement_key) if unit else None,
            unit.capacity_max if unit else None,
        ))
    return {
        "status": result.status,
        "solver_outcome": result.solver_outcome,
        "assignments": sorted(assignments),
        "unplaced_unit_keys": sorted(
            stable_units.get(key, key) for key in result.unplaced_unit_keys
        ),
        "diagnostics": result.diagnostics,
        "objective_components": result.objective_components,
        "staffing_summary": result.staffing_summary,
    }


def placement_result_semantic_fingerprint(data, result):
    return _fingerprint(_result_semantic_payload(data, result))


def rebind_placement_result(source_data, result, target_data):
    """Rebind a frozen result onto a fresh equivalent DTO namespace.

    This is research replay infrastructure only.  It never chooses a new
    semester or block: every assignment must map through the normalized unit,
    timeslot, and online-session identities, and all semantic domains must
    already match.  Ambiguous or missing mappings fail closed.
    """

    source_fingerprint = placement_input_semantic_fingerprint(source_data)
    target_fingerprint = placement_input_semantic_fingerprint(target_data)
    if source_fingerprint != target_fingerprint:
        raise ValueError("Frozen placement result belongs to a different input semantics")
    source_context = _semantic_context(source_data)
    target_context = _semantic_context(target_data)

    source_units_by_stable = {
        stable: key for key, stable in source_context["unit_stable"].items()
    }
    target_units_by_stable = {
        stable: key for key, stable in target_context["unit_stable"].items()
    }
    if set(source_units_by_stable) != set(target_units_by_stable):
        raise ValueError("Frozen placement units cannot be mapped to fresh input")
    source_units = {unit.key: unit for unit in source_data.units}
    target_units = {unit.key: unit for unit in target_data.units}

    def inverse(mapping):
        values = list(mapping.values())
        if len(values) != len(set(values)):
            raise ValueError("Placement semantic identity is ambiguous")
        return {value: key for key, value in mapping.items()}

    source_slots = {
        slot.id: (slot.semester, slot.block, bool(slot.is_available))
        for slot in source_data.timeslots
    }
    target_slots = {
        slot.id: (slot.semester, slot.block, bool(slot.is_available))
        for slot in target_data.timeslots
    }
    source_slot_by_semantics = {}
    target_slot_by_semantics = {}
    for slot_id, semantic in source_slots.items():
        if semantic in source_slot_by_semantics:
            raise ValueError("Frozen placement source has duplicate timeslot semantics")
        source_slot_by_semantics[semantic] = slot_id
    for slot_id, semantic in target_slots.items():
        if semantic in target_slot_by_semantics:
            raise ValueError("Fresh placement target has duplicate timeslot semantics")
        target_slot_by_semantics[semantic] = slot_id
    if set(source_slot_by_semantics) != set(target_slot_by_semantics):
        raise ValueError("Frozen placement timeslot domain cannot be mapped")

    source_sessions = inverse(source_context["session_rank"])
    target_sessions = inverse(target_context["session_rank"])

    rebound_assignments = []
    for assignment in result.assignments:
        stable = source_context["unit_stable"].get(assignment.unit_key)
        if stable is None or stable not in target_units_by_stable:
            raise ValueError("Frozen placement assignment references an unknown unit")
        target_key = target_units_by_stable[stable]
        source_unit = source_units[assignment.unit_key]
        target_unit = target_units[target_key]
        source_slot_semantic = source_slots.get(assignment.timeslot_id)
        if source_slot_semantic is None:
            raise ValueError("Frozen placement assignment references an unknown timeslot")
        target_timeslot_id = target_slot_by_semantics.get(source_slot_semantic)
        if target_timeslot_id is None:
            raise ValueError("Frozen placement assignment timeslot is not reproducible")
        target_session_id = None
        if assignment.online_supervision_session_id is not None:
            source_session_rank = source_context["session_rank"].get(
                assignment.online_supervision_session_id
            )
            if source_session_rank is None or source_session_rank not in target_sessions:
                raise ValueError("Frozen online supervision identity cannot be mapped")
            target_session_id = target_sessions[source_session_rank]
            if target_unit.online_supervision_session_id != target_session_id:
                raise ValueError("Frozen online unit does not match fresh session identity")
        elif target_unit.online_supervision_session_id is not None:
            raise ValueError("Frozen ordinary assignment maps to an online unit")
        target_slot_semantic = target_slots[target_timeslot_id]
        if target_slot_semantic[:2] != (assignment.semester, assignment.block):
            raise ValueError("Frozen assignment timing is not valid in fresh input")
        rebound_assignments.append(PlacementAssignmentDTO(
            unit_key=target_key,
            section_id=target_unit.section_id if assignment.section_id is not None else None,
            delivery_group_id=target_unit.delivery_group_id,
            semester=assignment.semester,
            timeslot_id=target_timeslot_id,
            block=assignment.block,
            annual_index=target_unit.annual_index,
            online_supervision_session_id=target_session_id,
        ))

    rebound_unplaced = []
    for key in result.unplaced_unit_keys:
        stable = source_context["unit_stable"].get(key)
        if stable is None or stable not in target_units_by_stable:
            raise ValueError("Frozen unplaced unit cannot be mapped")
        rebound_unplaced.append(target_units_by_stable[stable])
    return PlacementResultDTO(
        status=result.status,
        solver_outcome=result.solver_outcome,
        assignments=tuple(rebound_assignments),
        unplaced_unit_keys=tuple(rebound_unplaced),
        diagnostics=tuple(result.diagnostics),
        objective_components=dict(result.objective_components),
        staffing_summary=dict(result.staffing_summary),
    )


def placement_pair_summary(data, result):
    """Audit the paired-half placement contract without rerunning CP-SAT."""

    pair_units = {}
    for unit in data.units:
        if unit.shared_placement_key is not None:
            pair_units.setdefault(unit.shared_placement_key, []).append(unit)
    assignments = {item.unit_key: item for item in result.assignments}
    missing = []
    co_timed = 0
    split = 0
    valid_capacity = 0
    for pair_key, units in sorted(pair_units.items()):
        if len(units) != 2:
            raise ValueError(
                f"Shared placement key {pair_key!r} does not identify two units"
            )
        rows = [assignments.get(unit.key) for unit in units]
        if any(row is None for row in rows):
            missing.append(pair_key)
            continue
        first, second = rows
        same_position = (
            first.semester == second.semester
            and first.timeslot_id == second.timeslot_id
            and first.block == second.block
        )
        if same_position:
            co_timed += 1
            if units[0].capacity_max == units[1].capacity_max:
                valid_capacity += units[0].capacity_max
        else:
            split += 1
    return {
        "pair_count": len(pair_units),
        "co_timed_pair_count": co_timed,
        "split_pair_count": split,
        "missing_pair_count": len(missing),
        "valid_pair_capacity": valid_capacity,
        "pair_keys": tuple(sorted(pair_units)),
    }


def _result_from_payload(payload):
    assignments = tuple(
        PlacementAssignmentDTO(**item)
        for item in payload["assignments"]
    )
    return PlacementResultDTO(
        status=payload["status"],
        solver_outcome=payload["solver_outcome"],
        assignments=assignments,
        unplaced_unit_keys=tuple(payload.get("unplaced_unit_keys", ())),
        diagnostics=tuple(payload.get("diagnostics", ())),
        objective_components=dict(payload.get("objective_components", {})),
        staffing_summary=dict(payload.get("staffing_summary", {})),
    )


def serialize_frozen_placement_input(path, *, data, provenance=None):
    fingerprint = placement_input_semantic_fingerprint(data)
    payload = {
        "schema": PLACEMENT_INPUT_SCHEMA,
        "semantic_fingerprint": fingerprint,
        "provenance": dict(provenance or {}),
        "dto": _encode_snapshot_value(data),
    }
    _write(path, payload)
    return payload


def load_frozen_placement_input(path, *, expected_fingerprint=None):
    payload = _read(path)
    if payload.get("schema") != PLACEMENT_INPUT_SCHEMA:
        raise ValueError("Unsupported frozen placement input schema")
    if expected_fingerprint is not None and payload.get("semantic_fingerprint") != expected_fingerprint:
        raise ValueError("Frozen placement input fingerprint mismatch")
    data = _decode_snapshot_value(payload.get("dto"))
    if not isinstance(data, PlacementInputDTO):
        raise ValueError("Frozen placement input does not contain PlacementInputDTO")
    actual = placement_input_semantic_fingerprint(data)
    if actual != payload.get("semantic_fingerprint"):
        raise ValueError("Frozen placement input fingerprint is invalid")
    return {"data": data, "semantic_fingerprint": actual, "payload": payload}


def serialize_frozen_placement_result(path, *, data, result, provenance=None):
    if not isinstance(result, PlacementResultDTO):
        raise TypeError("result must be PlacementResultDTO")
    input_fingerprint = placement_input_semantic_fingerprint(data)
    result_fingerprint = placement_result_semantic_fingerprint(data, result)
    payload = {
        "schema": PLACEMENT_RESULT_SCHEMA,
        "input_semantic_fingerprint": input_fingerprint,
        "semantic_fingerprint": result_fingerprint,
        "provenance": dict(provenance or {}),
        "dto": _encode_snapshot_value(result),
    }
    _write(path, payload)
    return payload


def load_frozen_placement_result(path, *, data, expected_input_fingerprint=None):
    payload = _read(path)
    if payload.get("schema") != PLACEMENT_RESULT_SCHEMA:
        raise ValueError("Unsupported frozen placement result schema")
    input_fingerprint = placement_input_semantic_fingerprint(data)
    if payload.get("input_semantic_fingerprint") != input_fingerprint:
        raise ValueError("Frozen placement result input fingerprint mismatch")
    if expected_input_fingerprint is not None and input_fingerprint != expected_input_fingerprint:
        raise ValueError("Frozen placement result input differs from expectation")
    result = _decode_snapshot_value(payload.get("dto"))
    if not isinstance(result, PlacementResultDTO):
        raise ValueError("Frozen placement result does not contain PlacementResultDTO")
    actual = placement_result_semantic_fingerprint(data, result)
    if actual != payload.get("semantic_fingerprint"):
        raise ValueError("Frozen placement result fingerprint is invalid")
    return {"result": result, "semantic_fingerprint": actual, "payload": payload}


def placement_result_from_mapping(payload):
    """Rehydrate the JSON result stored by ``SectionPlacementRun``."""

    return _result_from_payload(payload)
