"""Lossless detached section-placement checkpoint contracts."""

from dataclasses import replace

import pytest

from scheduling_engine.dto import (
    OnlineSupervisionDemandDTO,
    OnlineSupervisionPlacementSessionDTO,
    PlacementAssignmentDTO,
    PlacementInputDTO,
    PlacementResultDTO,
    PlacementStudentTimetableDemandDTO,
    PlacementTeacherDTO,
    PlacementUnitDTO,
    TimeSlotDTO,
)
from scheduling_engine.placement_research_artifacts import (
    load_frozen_placement_input,
    load_frozen_placement_result,
    placement_input_semantic_fingerprint,
    placement_result_semantic_fingerprint,
    serialize_frozen_placement_input,
    serialize_frozen_placement_result,
    rebind_placement_result,
)
from scheduling_engine.section_placement import solve_section_placement


def _fixture():
    slots = tuple(
        TimeSlotDTO(semester * 10 + index, 1, semester, block, True)
        for semester in (1, 2)
        for index, block in enumerate(("A", "B", "C", "D"), start=1)
    )
    teachers = (
        PlacementTeacherDTO(1, (101, 102), 8, 8, 16),
        PlacementTeacherDTO(2, (101, 102), 8, 8, 16),
    )
    units = (
        PlacementUnitDTO(
            "annual:10:1", 10, (101,), (1, 2), annual_index=1,
            source_mode="annual_total", capacity_max=40,
        ),
        PlacementUnitDTO(
            "annual:11:1", 11, (102,), (1, 2), annual_index=1,
            source_mode="annual_total", capacity_max=40,
        ),
        PlacementUnitDTO(
            "annual:12:1", 12, (101,), (1, 2), annual_index=1,
            source_mode="annual_total", shared_placement_key="half-pair",
            shared_staffing_key="half-pair", capacity_max=40,
        ),
        PlacementUnitDTO(
            "annual:13:1", 13, (102,), (1, 2), annual_index=1,
            source_mode="annual_total", shared_placement_key="half-pair",
            shared_staffing_key="half-pair", capacity_max=40,
        ),
        PlacementUnitDTO(
            "online_supervision:1", -1, (), (1, 2), source_mode="annual_total",
            requires_course_qualification=False, online_supervision_session_id=1,
        ),
        PlacementUnitDTO(
            "online_supervision:2", -2, (), (1, 2), source_mode="annual_total",
            requires_course_qualification=False, online_supervision_session_id=2,
        ),
    )
    return PlacementInputDTO(
        academic_year_id=1,
        input_mode="annual_total",
        units=units,
        fixed_placements=(),
        timeslots=slots,
        teachers=teachers,
        conflicts=(),
        online_supervision_sessions=(
            OnlineSupervisionPlacementSessionDTO(1, 8, (1, 2)),
            OnlineSupervisionPlacementSessionDTO(2, 8, (1, 2)),
        ),
        online_supervision_demands=(
            OnlineSupervisionDemandDTO(1, 7, 101, (1, 2)),
            OnlineSupervisionDemandDTO(2, 7, 102, (1, 2)),
        ),
        student_timetable_demands=(
            PlacementStudentTimetableDemandDTO(3, 7, 101),
            PlacementStudentTimetableDemandDTO(4, 7, 102),
        ),
        time_limit_seconds=5,
    )


def test_placement_input_and_result_round_trip_preserves_semantics(tmp_path):
    data = _fixture()
    result = solve_section_placement(data)
    assert result.solver_outcome in {"feasible", "optimal"}
    assert len(result.assignments) >= 5
    assert {
        item.online_supervision_session_id
        for item in result.assignments
        if item.online_supervision_session_id is not None
    } == {1, 2}
    assert sum(item.unit_key.startswith("annual:12") for item in result.assignments) == 1
    assert sum(item.unit_key.startswith("annual:13") for item in result.assignments) == 1

    input_path = tmp_path / "placement-input.json"
    result_path = tmp_path / "placement-result.json"
    input_payload = serialize_frozen_placement_input(
        input_path, data=data, provenance={"origin": "test"}
    )
    result_payload = serialize_frozen_placement_result(
        result_path, data=data, result=result, provenance={"origin": "test"}
    )
    loaded_input = load_frozen_placement_input(input_path)
    loaded_result = load_frozen_placement_result(
        result_path, data=loaded_input["data"],
        expected_input_fingerprint=input_payload["semantic_fingerprint"],
    )
    assert loaded_input["semantic_fingerprint"] == input_payload["semantic_fingerprint"]
    assert loaded_result["semantic_fingerprint"] == result_payload["semantic_fingerprint"]
    assert placement_input_semantic_fingerprint(loaded_input["data"]) == input_payload["semantic_fingerprint"]
    assert placement_result_semantic_fingerprint(
        loaded_input["data"], loaded_result["result"]
    ) == result_payload["semantic_fingerprint"]


def test_placement_checkpoint_fails_closed_on_input_drift(tmp_path):
    data = _fixture()
    input_path = tmp_path / "placement-input.json"
    serialize_frozen_placement_input(input_path, data=data)
    drifted = replace(data, time_limit_seconds=6)
    with pytest.raises(ValueError, match="fingerprint"):
        load_frozen_placement_input(
            input_path,
            expected_fingerprint=placement_input_semantic_fingerprint(drifted),
        )


def test_result_artifact_rejects_different_input(tmp_path):
    data = _fixture()
    result = PlacementResultDTO(
        status="complete",
        solver_outcome="feasible",
        assignments=(
            PlacementAssignmentDTO("annual:10:1", None, 10, 1, 11, "A", 1),
        ),
        unplaced_unit_keys=(),
        diagnostics=(),
        objective_components={},
        staffing_summary={},
    )
    result_path = tmp_path / "placement-result.json"
    serialize_frozen_placement_result(result_path, data=data, result=result)
    with pytest.raises(ValueError, match="input fingerprint"):
        load_frozen_placement_result(
            result_path,
            data=replace(data, time_limit_seconds=6),
        )


def test_frozen_result_rebinds_across_renumbered_semantic_namespace():
    source = _fixture()
    result = solve_section_placement(source)
    key_map = {
        "annual:10:1": "annual:110:1",
        "annual:11:1": "annual:111:1",
        "annual:12:1": "annual:112:1",
        "annual:13:1": "annual:113:1",
        "online_supervision:1": "online_supervision:11",
        "online_supervision:2": "online_supervision:12",
    }
    target_slots = tuple(
        replace(slot, id=slot.id + 100, academic_year_id=2)
        for slot in source.timeslots
    )
    target_units = tuple(
        replace(
            unit,
            key=key_map[unit.key],
                delivery_group_id=(
                    unit.delivery_group_id + 100
                    if unit.delivery_group_id >= 0 else unit.delivery_group_id - 10
                ),
                member_course_ids=tuple(course_id + 100 for course_id in unit.member_course_ids),
            online_supervision_session_id=(
                unit.online_supervision_session_id + 10
                if unit.online_supervision_session_id is not None else None
            ),
        )
        for unit in source.units
    )
    target = replace(
        source,
        academic_year_id=2,
        units=target_units,
        timeslots=target_slots,
        teachers=tuple(
            replace(
                teacher,
                id=teacher.id + 100,
                eligible_course_ids=tuple(course_id + 100 for course_id in teacher.eligible_course_ids),
            )
            for teacher in source.teachers
        ),
        online_supervision_sessions=tuple(
            replace(session, session_id=session.session_id + 10)
            for session in source.online_supervision_sessions
        ),
        online_supervision_demands=tuple(
            replace(
                demand,
                request_id=demand.request_id + 100,
                student_id=demand.student_id + 100,
                course_id=demand.course_id + 100,
            )
            for demand in source.online_supervision_demands
        ),
        student_timetable_demands=tuple(
            replace(
                demand,
                request_id=demand.request_id + 100,
                student_id=demand.student_id + 100,
                course_id=demand.course_id + 100,
                paired_half_course_id=(
                    demand.paired_half_course_id + 100
                    if demand.paired_half_course_id is not None else None
                ),
            )
            for demand in source.student_timetable_demands
        ),
    )
    assert placement_input_semantic_fingerprint(source) == placement_input_semantic_fingerprint(target)
    rebound = rebind_placement_result(source, result, target)
    assert placement_result_semantic_fingerprint(source, result) == placement_result_semantic_fingerprint(target, rebound)
    assert all(item.timeslot_id >= 100 for item in rebound.assignments)
    assert all(item.unit_key in {unit.key for unit in target.units} for item in rebound.assignments)
