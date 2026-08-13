"""Pure-contract tests for counselor-reviewed semester/A-D placement."""

from scheduling_engine.dto import (
    OnlineSupervisionDemandDTO,
    OnlineSupervisionPlacementSessionDTO,
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


def _solve(
    *, units, teachers=(_teacher(),), conflicts=(), mode="annual_total",
    online_sessions=(), online_demands=(), timeslots=None,
):
    return solve_section_placement(PlacementInputDTO(
        academic_year_id=1, input_mode=mode, units=tuple(units), fixed_placements=(),
        timeslots=_slots() if timeslots is None else tuple(timeslots),
        teachers=teachers, conflicts=tuple(conflicts),
        online_supervision_sessions=tuple(online_sessions),
        online_supervision_demands=tuple(online_demands),
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


def test_online_supervision_is_placed_without_subject_qualification():
    """A supervisor needs workload-safe time, not the online course's teachable."""

    result = _solve(units=(PlacementUnitDTO(
        "online_supervision:9", -9, (), (1,), annual_index=1,
        source_mode="annual_total", requires_course_qualification=False,
        online_supervision_session_id=9,
    ),), teachers=(_teacher(courses=()),))

    assert result.status == "complete"
    assert result.assignments[0].online_supervision_session_id == 9


def test_online_co_requests_force_generic_sessions_into_distinct_blocks():
    """Two online courses for one student need two supervision blocks, not two course rooms."""

    result = _solve(
        units=(
            PlacementUnitDTO(
                "online_supervision:9", -9, (), (1,), annual_index=1,
                source_mode="annual_total", requires_course_qualification=False,
                online_supervision_session_id=9,
            ),
            PlacementUnitDTO(
                "online_supervision:10", -10, (), (1,), annual_index=2,
                source_mode="annual_total", requires_course_qualification=False,
                online_supervision_session_id=10,
            ),
        ),
        teachers=(_teacher(1, courses=()), _teacher(2, courses=())),
        online_sessions=(
            OnlineSupervisionPlacementSessionDTO(9, 3, (1,)),
            OnlineSupervisionPlacementSessionDTO(10, 3, (1,)),
        ),
        online_demands=(
            OnlineSupervisionDemandDTO(101, 50, 1, (1,)),
            OnlineSupervisionDemandDTO(102, 50, 2, (1,)),
        ),
    )

    assert result.status == "complete"
    assert len({item.timeslot_id for item in result.assignments}) == 2


def test_generic_online_session_can_supervise_multiple_course_codes():
    """Generic capacity remains shared even when course codes differ."""

    result = _solve(
        units=(PlacementUnitDTO(
            "online_supervision:9", -9, (), (1,), annual_index=1,
            source_mode="annual_total", requires_course_qualification=False,
            online_supervision_session_id=9,
        ),),
        teachers=(_teacher(courses=()),),
        online_sessions=(OnlineSupervisionPlacementSessionDTO(9, 3, (1,)),),
        online_demands=(
            OnlineSupervisionDemandDTO(101, 50, 1, (1,)),
            OnlineSupervisionDemandDTO(102, 51, 2, (1,)),
        ),
    )

    assert result.status == "complete"
    assert result.assignments[0].online_supervision_session_id == 9


def test_online_demand_with_no_distinct_supervision_blocks_is_diagnostic():
    """Enough aggregate seats cannot compensate for one student's time collision."""

    result = _solve(
        units=(
            PlacementUnitDTO(
                "online_supervision:9", -9, (), (1,), annual_index=1,
                source_mode="annual_total", requires_course_qualification=False,
                online_supervision_session_id=9,
            ),
            PlacementUnitDTO(
                "online_supervision:10", -10, (), (1,), annual_index=2,
                source_mode="annual_total", requires_course_qualification=False,
                online_supervision_session_id=10,
            ),
        ),
        teachers=(_teacher(1, courses=()), _teacher(2, courses=())),
        timeslots=(TimeSlotDTO(11, 1, 1, "A"),),
        online_sessions=(
            OnlineSupervisionPlacementSessionDTO(9, 3, (1,)),
            OnlineSupervisionPlacementSessionDTO(10, 3, (1,)),
        ),
        online_demands=(
            OnlineSupervisionDemandDTO(101, 50, 1, (1,)),
            OnlineSupervisionDemandDTO(102, 50, 2, (1,)),
        ),
    )

    assert result.status == "infeasible"
    assert any(
        item["code"] == "online_supervision_block_diversity_insufficient"
        for item in result.diagnostics
    )


def test_online_demand_beyond_generic_session_capacity_is_diagnostic():
    """Placement reports a seat shortage separately from a block-diversity shortage."""

    result = _solve(
        units=(PlacementUnitDTO(
            "online_supervision:9", -9, (), (1,), annual_index=1,
            source_mode="annual_total", requires_course_qualification=False,
            online_supervision_session_id=9,
        ),),
        teachers=(_teacher(courses=()),),
        online_sessions=(OnlineSupervisionPlacementSessionDTO(9, 1, (1,)),),
        online_demands=(
            OnlineSupervisionDemandDTO(101, 50, 1, (1,)),
            OnlineSupervisionDemandDTO(102, 51, 2, (1,)),
        ),
    )

    assert result.status == "infeasible"
    assert any(
        item["code"] == "online_supervision_capacity_insufficient"
        for item in result.diagnostics
    )
