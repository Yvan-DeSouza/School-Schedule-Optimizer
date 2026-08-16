"""Pure-contract tests for counselor-reviewed semester/A-D placement."""

from scheduling_engine.dto import (
    OnlineSupervisionDemandDTO,
    OnlineSupervisionPlacementSessionDTO,
    PlacementConflictDTO,
    PlacementInputDTO,
    PlacementStudentTimetableDemandDTO,
    PlacementTeacherDTO,
    PlacementUnitDTO,
    TimeSlotDTO,
)
from ortools.sat.python import cp_model
import scheduling_engine.section_placement as section_placement
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
    online_sessions=(), online_demands=(), student_timetable_demands=(),
    timeslots=None,
):
    return solve_section_placement(PlacementInputDTO(
        academic_year_id=1, input_mode=mode, units=tuple(units), fixed_placements=(),
        timeslots=_slots() if timeslots is None else tuple(timeslots),
        teachers=teachers, conflicts=tuple(conflicts),
        online_supervision_sessions=tuple(online_sessions),
        online_supervision_demands=tuple(online_demands),
        student_timetable_demands=tuple(student_timetable_demands),
    ))


def test_annual_unit_respects_semester_only_rule_and_never_returns_teacher():
    result = _solve(units=(PlacementUnitDTO("annual:1:1", 1, (1,), (2,), annual_index=1, source_mode="annual_total"),))
    assert result.status == "complete"
    assert result.assignments[0].semester == 2
    assert not hasattr(result.assignments[0], "teacher_id")
    assert result.staffing_summary["teacher_names_or_assignments_returned"] is False


def test_complete_feasibility_seed_selects_one_slot_per_unit_not_every_candidate_slot():
    """A complete seed must not make mutually exclusive timing choices all true."""

    unit = PlacementUnitDTO(
        "section:1", 1, (1,), (1,), section_id=1, fixed_semester=1,
    )
    data = PlacementInputDTO(
        academic_year_id=1,
        input_mode="fixed_semester",
        units=(unit,),
        fixed_placements=(),
        timeslots=_slots(),
        teachers=(_teacher(),),
        conflicts=(),
    )
    model = cp_model.CpModel()
    placement_vars = {
        (unit.key, slot.id): model.NewBoolVar(f"placement_{slot.id}")
        for slot in _slots()
        if slot.semester == 1
    }
    model.Add(sum(placement_vars.values()) <= 1)
    seed = section_placement._solve_complete_timing_seed(
        model,
        data,
        {slot.id: slot for slot in data.timeslots},
        (unit,),
        placement_vars,
        1,
    )

    assert seed is not None


def test_multiple_locks_are_exact_and_teacher_witness_prevents_same_block_collision():
    units = (
        PlacementUnitDTO("annual:1:1", 1, (1,), (1, 2), locked_timeslot_id=11, annual_index=1, source_mode="annual_total"),
        PlacementUnitDTO("annual:1:2", 1, (1,), (1, 2), locked_timeslot_id=12, annual_index=2, source_mode="annual_total"),
    )
    result = _solve(units=units)
    assert {(item.unit_key, item.timeslot_id) for item in result.assignments} == {
        ("annual:1:1", 11), ("annual:1:2", 12),
    }


def test_post_placement_staffing_witness_rejects_a_double_booked_timing_candidate():
    """Timing cannot become reviewable when its hidden staffing proof fails."""

    result = _solve(
        units=(
            PlacementUnitDTO(
                "section:1", 1, (1,), (1,), section_id=1,
                fixed_semester=1, locked_timeslot_id=11,
            ),
            PlacementUnitDTO(
                "section:2", 2, (2,), (1,), section_id=2,
                fixed_semester=1, locked_timeslot_id=11,
            ),
        ),
        teachers=(_teacher(1, (1, 2)),),
        mode="fixed_semester",
    )

    assert result.status in {"partial", "infeasible"}
    assert result.status != "complete"
    assert result.staffing_summary["witness_proven"] is False


def test_unit_sort_key_uses_natural_numeric_identity_order():
    """Opaque database digits must not change deterministic search order."""

    units = (
        PlacementUnitDTO("section:10", 10, (1,), (1,), section_id=10),
        PlacementUnitDTO("section:2", 2, (1,), (1,), section_id=2),
    )

    assert [unit.key for unit in sorted(units, key=section_placement._unit_sort_key)] == [
        "section:2", "section:10",
    ]


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


def test_student_timetable_witness_rejects_aggregate_capacity_with_a_block_collision():
    """Annual seats cannot mask a mandatory pathway's recurring-block deficit."""

    result = _solve(
        units=(
            PlacementUnitDTO(
                "section:1", 1, (1,), (1,), section_id=1,
                fixed_semester=1, locked_timeslot_id=12, capacity_max=3,
            ),
            PlacementUnitDTO(
                "section:2", 2, (2,), (1,), section_id=2,
                fixed_semester=1, locked_timeslot_id=11, capacity_max=3,
            ),
            PlacementUnitDTO(
                "section:3", 3, (3,), (1,), section_id=3,
                fixed_semester=1, locked_timeslot_id=13, capacity_max=3,
            ),
            PlacementUnitDTO(
                "section:4", 4, (4,), (1,), section_id=4,
                fixed_semester=1, locked_timeslot_id=11, capacity_max=3,
            ),
        ),
        teachers=(
            _teacher(1, (1, 2, 3, 4), semester_capacity=4),
            _teacher(2, (1, 2, 3, 4), semester_capacity=4),
        ),
        mode="fixed_semester",
        timeslots=(
            TimeSlotDTO(11, 1, 1, "A"), TimeSlotDTO(12, 1, 1, "B"),
            TimeSlotDTO(13, 1, 1, "C"), TimeSlotDTO(14, 1, 1, "D"),
        ),
        student_timetable_demands=tuple(
            PlacementStudentTimetableDemandDTO(
                request_id=(student_id * 10) + course_id,
                student_id=student_id,
                course_id=course_id,
                allowed_semesters=(1,),
            )
            for student_id in (1, 2, 3)
            for course_id in (1, 2, 3, 4)
        ),
    )

    assert result.status == "infeasible"


def test_student_timetable_witness_keeps_a_capacity_safe_pathway_placeable():
    """The witness permits the same demand when the fourth course uses D."""

    result = _solve(
        units=(
            PlacementUnitDTO(
                "section:1", 1, (1,), (1,), section_id=1,
                fixed_semester=1, locked_timeslot_id=12, capacity_max=3,
            ),
            PlacementUnitDTO(
                "section:2", 2, (2,), (1,), section_id=2,
                fixed_semester=1, locked_timeslot_id=11, capacity_max=3,
            ),
            PlacementUnitDTO(
                "section:3", 3, (3,), (1,), section_id=3,
                fixed_semester=1, locked_timeslot_id=13, capacity_max=3,
            ),
            PlacementUnitDTO(
                "section:4", 4, (4,), (1,), section_id=4,
                fixed_semester=1, locked_timeslot_id=14, capacity_max=3,
            ),
        ),
        teachers=(_teacher(1, (1, 2, 3, 4), semester_capacity=4),),
        mode="fixed_semester",
        timeslots=(
            TimeSlotDTO(11, 1, 1, "A"), TimeSlotDTO(12, 1, 1, "B"),
            TimeSlotDTO(13, 1, 1, "C"), TimeSlotDTO(14, 1, 1, "D"),
        ),
        student_timetable_demands=tuple(
            PlacementStudentTimetableDemandDTO(
                request_id=(student_id * 10) + course_id,
                student_id=student_id,
                course_id=course_id,
                allowed_semesters=(1,),
            )
            for student_id in (1, 2, 3)
            for course_id in (1, 2, 3, 4)
        ),
    )

    assert result.status == "complete"


def test_conflict_weight_lookup_is_compiled_once_per_placement_solve(monkeypatch):
    """Large section runs must not rebuild unchanged annual conflict facts per pair."""

    calls = 0
    original = section_placement._course_pair_weights

    def count_compilations(data):
        nonlocal calls
        calls += 1
        return original(data)

    monkeypatch.setattr(section_placement, "_course_pair_weights", count_compilations)
    result = _solve(
        units=(
            PlacementUnitDTO("section:1", 1, (1,), (1,), section_id=1, fixed_semester=1),
            PlacementUnitDTO("section:2", 2, (2,), (1,), section_id=2, fixed_semester=1),
            PlacementUnitDTO("section:3", 3, (1,), (1,), section_id=3, fixed_semester=1),
        ),
        teachers=(_teacher(1, (1, 2)), _teacher(2, (1, 2))),
        conflicts=(PlacementConflictDTO(1, 2, 100, 20),),
        mode="fixed_semester",
    )

    assert result.status == "complete"
    assert calls == 1


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
