"""Pure-contract tests for counselor-reviewed semester/A-D placement."""

from scheduling_engine.dto import (
    PlacementConflictDTO,
    PlacementInputDTO,
    PlacementTeacherDTO,
    PlacementUnitDTO,
    TimeSlotDTO,
)
from scheduling_engine.section_placement import solve_section_placement


def _slots():
    return (
        TimeSlotDTO(11, 1, 1, "A"), TimeSlotDTO(12, 1, 1, "B"),
        TimeSlotDTO(21, 1, 2, "A"), TimeSlotDTO(22, 1, 2, "B"),
    )


def _teacher(identifier=1, courses=(1, 2), semester_capacity=2, annual_capacity=4, unavailable=()):
    return PlacementTeacherDTO(
        identifier, courses, semester_capacity, semester_capacity, annual_capacity,
        unavailable,
    )


def _solve(*, units, teachers=(_teacher(),), conflicts=(), mode="annual_total"):
    return solve_section_placement(PlacementInputDTO(
        academic_year_id=1, input_mode=mode, units=tuple(units), fixed_placements=(),
        timeslots=_slots(), teachers=teachers, conflicts=tuple(conflicts),
    ))


def test_annual_unit_respects_semester_only_rule_and_never_returns_teacher():
    result = _solve(units=(PlacementUnitDTO("annual:1:1", 1, (1,), (2,), annual_index=1, source_mode="annual_total"),))
    assert result.status == "complete"
    assert result.assignments[0].semester == 2
    assert not hasattr(result.assignments[0], "teacher_id")
    assert result.staffing_summary["teacher_names_or_assignments_returned"] is False


def test_multiple_locks_are_exact_and_teacher_witness_prevents_same_block_collision():
    units = (
        PlacementUnitDTO("annual:1:1", 1, (1,), (1, 2), locked_timeslot_id=11, annual_index=1, source_mode="annual_total"),
        PlacementUnitDTO("annual:1:2", 1, (1,), (1, 2), locked_timeslot_id=12, annual_index=2, source_mode="annual_total"),
    )
    result = _solve(units=units)
    assert {(item.unit_key, item.timeslot_id) for item in result.assignments} == {
        ("annual:1:1", 11), ("annual:1:2", 12),
    }


def test_unavailable_by_explicit_denial_but_absent_rows_default_to_available():
    unit = PlacementUnitDTO("section:1", 1, (1,), (1,), section_id=1, fixed_semester=1)
    available_default = _solve(units=(unit,), mode="fixed_semester")
    denied_a_only = _solve(units=(unit,), teachers=(_teacher(unavailable=(11,)),), mode="fixed_semester")
    assert available_default.status == "complete"
    assert denied_a_only.status == "complete"
    assert denied_a_only.assignments[0].timeslot_id == 12


def test_pair_collision_weight_chooses_different_blocks_when_staffing_allows_it():
    units = (
        PlacementUnitDTO("annual:1:1", 1, (1,), (1,), annual_index=1, source_mode="annual_total"),
        PlacementUnitDTO("annual:2:1", 2, (2,), (1,), annual_index=1, source_mode="annual_total"),
    )
    result = _solve(
        units=units,
        teachers=(_teacher(1, (1, 2)), _teacher(2, (1, 2))),
        conflicts=(PlacementConflictDTO(1, 2, 100, 20),),
    )
    assert result.status == "complete"
    assert len({item.block for item in result.assignments}) == 2


def test_missing_eligible_teacher_produces_a_non_approvable_result():
    result = _solve(
        units=(PlacementUnitDTO("annual:1:1", 1, (3,), (1,), annual_index=1, source_mode="annual_total"),),
    )
    assert result.status == "infeasible"
    assert result.unplaced_unit_keys == ("annual:1:1",)
    assert result.diagnostics[0]["code"] == "no_eligible_teacher"
