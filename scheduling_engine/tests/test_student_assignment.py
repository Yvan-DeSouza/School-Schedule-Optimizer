"""Contracts for pure student-to-section assignment; no Django dependency."""

from ortools.sat.python import cp_model

import scheduling_engine.student_assignment as student_assignment_module
from scheduling_engine.dto import (
    CourseCategoryRelationshipDTO,
    CourseDifficultyDTO,
    CoursePrerequisiteDTO,
    CourseSequencePreferenceDTO,
    FixedEnrollmentDTO,
    FixedStudentScheduleCommitmentDTO,
    StudentScheduleCommitmentRequestDTO,
    StudentSpecialCommitmentLockDTO,
    StudentAssignmentInputDTO,
    StudentAssignmentLockDTO,
    StudentAssignmentRequestDTO,
    StudentAssignmentSectionDTO,
    StudentAssignmentScopeDTO,
    TimeSlotDTO,
)
from scheduling_engine.student_assignment import solve_student_assignment


def _request(request_id=1, **overrides):
    values = dict(
        request_id=request_id, student_id=1, course_id=1, course_offering_id=11,
        is_primary=True, is_mandatory=False, priority_tier=4,
    )
    values.update(overrides)
    return StudentAssignmentRequestDTO(**values)


def _section(section_id=1, **overrides):
    values = dict(
        section_id=section_id, delivery_group_id=1, member_course_offering_ids=(11,),
        member_course_ids=(1,), semester=1, timeslot_id=101,
        capacity_max=2, target_capacity=2,
    )
    values.update(overrides)
    return StudentAssignmentSectionDTO(**values)


def _input(**overrides):
    values = dict(
        academic_year_id=1, requests=(_request(),), sections=(_section(),),
        fixed_enrollments=(), hard_prerequisites=(), soft_sequence_preferences=(),
        section_utilization_balance_importance="important",
        student_semester_balance_importance="important",
        course_sequence_preferences_importance="important",
    )
    values.update(overrides)
    return StudentAssignmentInputDTO(**values)


def _timeslots():
    """One available A-D pattern in each semester for special commitments."""

    return tuple(
        TimeSlotDTO(
            1000 + semester * 10 + index,
            1,
            semester,
            block,
            True,
        )
        for semester in (1, 2)
        for index, block in enumerate(("A", "B", "C", "D"), start=1)
    )


def _difficulty(course_id, score, category="math"):
    return CourseDifficultyDTO(
        course_id=course_id,
        category=category,
        calculated_difficulty=score,
        manual_difficulty_override=None,
        effective_difficulty=score,
        calculation_version="test_v1",
    )


def _semester_choice_sections():
    return (
        _section(1, semester=1, timeslot_id=101),
        _section(2, semester=2, timeslot_id=201),
        _section(3, delivery_group_id=2, member_course_offering_ids=(22,), member_course_ids=(2,), semester=1, timeslot_id=102),
        _section(4, delivery_group_id=2, member_course_offering_ids=(22,), member_course_ids=(2,), semester=2, timeslot_id=202),
    )


def test_assigns_primary_to_accepted_section_deterministically():
    first = solve_student_assignment(_input())
    second = solve_student_assignment(_input())

    assert first.status == "complete"
    assert first.assignments == second.assignments
    assert first.assignments[0].section_id == 1


def test_fixed_enrollment_blocks_student_timeslot_and_consumes_capacity():
    fixed = FixedEnrollmentDTO(
        student_id=1, section_id=2, course_offering_id=22, course_id=2,
        semester=1, timeslot_id=101,
    )
    result = solve_student_assignment(_input(
        sections=(_section(), _section(2, delivery_group_id=2, member_course_offering_ids=(22,), member_course_ids=(2,), timeslot_id=101, capacity_max=1)),
        fixed_enrollments=(fixed,),
    ))

    assert result.status == "partial"
    assert result.unmet_requests[0].diagnostic_code == "student_assignment_timeslot_collision"


def test_combined_section_has_shared_physical_capacity():
    result = solve_student_assignment(_input(
        requests=(
            _request(1, student_id=1, course_id=1, course_offering_id=11),
            _request(2, student_id=2, course_id=2, course_offering_id=22),
        ),
        sections=(_section(
            member_course_offering_ids=(11, 22), member_course_ids=(1, 2),
            capacity_max=1,
        ),),
    ))

    assert result.status == "partial"
    assert len(result.assignments) == 1


def test_hard_same_year_prerequisite_requires_semester_one_then_two():
    result = solve_student_assignment(_input(
        requests=(
            _request(1, course_id=1, course_offering_id=11),
            _request(2, course_id=2, course_offering_id=22),
        ),
        sections=(
            _section(1, semester=1, timeslot_id=101),
            _section(2, delivery_group_id=2, member_course_offering_ids=(22,), member_course_ids=(2,), semester=2, timeslot_id=202),
        ),
        hard_prerequisites=(CoursePrerequisiteDTO(course_id=2, prerequisite_id=1),),
    ))

    assert result.status == "complete"
    assert {(row.course_id, row.semester) for row in result.assignments} == {(1, 1), (2, 2)}


def test_soft_sequence_is_reported_when_both_courses_apply():
    result = solve_student_assignment(_input(
        requests=(
            _request(1, course_id=1, course_offering_id=11),
            _request(2, course_id=2, course_offering_id=22),
        ),
        sections=(
            _section(1, semester=1, timeslot_id=101),
            _section(2, delivery_group_id=2, member_course_offering_ids=(22,), member_course_ids=(2,), semester=2, timeslot_id=202),
        ),
        soft_sequence_preferences=(CourseSequencePreferenceDTO(earlier_course_id=1, later_course_id=2),),
    ))

    assert result.status == "complete"
    assert result.sequence_outcomes == ({
        "student_id": 1, "earlier_course_id": 1, "later_course_id": 2, "satisfied": True,
    },)


def test_difficulty_balance_prefers_a_less_imbalanced_semester_split():
    """Difficulty changes a soft preference only after request fulfillment."""

    result = solve_student_assignment(_input(
        requests=(
            _request(1, course_id=1, course_offering_id=11),
            _request(2, course_id=2, course_offering_id=22),
        ),
        sections=_semester_choice_sections(),
        section_utilization_balance_importance="not_important",
        student_semester_balance_importance="not_important",
        course_sequence_preferences_importance="not_important",
        difficulty_balance_importance="important",
        course_difficulties=(_difficulty(1, 80), _difficulty(2, 20, "science")),
    ))

    by_request = {item.request_id: item for item in result.assignments}
    assert result.status == "complete"
    assert by_request[1].semester != by_request[2].semester
    assert result.objective_components["difficulty_balance_penalty"] == 60


def test_category_diversity_splits_repeated_categories_when_feasible():
    result = solve_student_assignment(_input(
        requests=(
            _request(1, course_id=1, course_offering_id=11),
            _request(2, course_id=2, course_offering_id=22),
        ),
        sections=_semester_choice_sections(),
        section_utilization_balance_importance="not_important",
        student_semester_balance_importance="not_important",
        course_sequence_preferences_importance="not_important",
        course_category_diversity_importance="important",
        course_difficulties=(_difficulty(1, 50, "math"), _difficulty(2, 50, "math")),
    ))

    by_request = {item.request_id: item for item in result.assignments}
    assert by_request[1].semester != by_request[2].semester
    assert result.objective_components["course_category_diversity_penalty"] == 0


def test_difficulty_and_category_importance_resolve_a_real_soft_preference_tradeoff():
    """Counselor labels, rather than exposed weights, decide the winning tier."""

    fixed = FixedEnrollmentDTO(
        student_id=1, section_id=5, course_offering_id=33, course_id=3,
        semester=1, timeslot_id=103,
    )
    sections = _semester_choice_sections() + (
        _section(5, delivery_group_id=3, member_course_offering_ids=(33,), member_course_ids=(3,), semester=1, timeslot_id=103),
    )
    common = dict(
        requests=(
            _request(1, course_id=1, course_offering_id=11),
            _request(2, course_id=2, course_offering_id=22),
        ),
        sections=sections,
        fixed_enrollments=(fixed,),
        section_utilization_balance_importance="not_important",
        student_semester_balance_importance="not_important",
        course_sequence_preferences_importance="not_important",
        course_difficulties=(
            _difficulty(1, 90, "math"),
            _difficulty(2, 10, "math"),
            _difficulty(3, 100, "science"),
        ),
    )
    difficulty_first = solve_student_assignment(_input(
        **common,
        difficulty_balance_importance="extremely_important",
        course_category_diversity_importance="important",
    ))
    category_first = solve_student_assignment(_input(
        **common,
        difficulty_balance_importance="important",
        course_category_diversity_importance="extremely_important",
    ))

    assert {item.semester for item in difficulty_first.assignments} == {2}
    assert {item.semester for item in category_first.assignments} == {1, 2}
    assert difficulty_first.objective_components["difficulty_balance_penalty"] == 0
    assert category_first.objective_components["course_category_diversity_penalty"] == 0


def test_category_diversity_never_overrides_a_hard_semester_constraint():
    result = solve_student_assignment(_input(
        requests=(
            _request(1, course_id=1, course_offering_id=11),
            _request(2, course_id=2, course_offering_id=22),
        ),
        sections=(
            _section(1, semester=1, timeslot_id=101),
            _section(2, delivery_group_id=2, member_course_offering_ids=(22,), member_course_ids=(2,), semester=1, timeslot_id=102),
        ),
        section_utilization_balance_importance="not_important",
        student_semester_balance_importance="not_important",
        course_sequence_preferences_importance="not_important",
        course_category_diversity_importance="extremely_important",
        course_difficulties=(_difficulty(1, 50, "math"), _difficulty(2, 50, "math")),
    ))

    assert result.status == "complete"
    assert {item.semester for item in result.assignments} == {1}
    assert result.objective_components["course_category_diversity_penalty"] == 100


def test_locked_active_enrollment_cannot_be_moved_in_a_rerun():
    result = solve_student_assignment(_input(
        sections=(
            _section(1, capacity_max=1),
            _section(2, delivery_group_id=2, timeslot_id=202),
        ),
        fixed_enrollments=(FixedEnrollmentDTO(
            enrollment_id=101,
            student_id=1,
            section_id=1,
            course_offering_id=11,
            course_id=1,
            semester=1,
            timeslot_id=101,
            is_locked=True,
            is_in_scope=True,
            lock_ids=(41,),
        ),),
    ))

    assert result.assignments == ()
    assert result.unmet_requests[0].diagnostic_code == "student_assignment_locked_enrollment_blocks_request"
    assert result.unmet_requests[0].blocking_lock_id == 41


def test_group_lock_assigns_all_members_to_one_section_or_none():
    result = solve_student_assignment(_input(
        requests=(
            _request(1, student_id=1),
            _request(2, student_id=2, is_in_scope=False),
        ),
        sections=(
            _section(1, capacity_max=1),
            _section(2, delivery_group_id=2, timeslot_id=202, capacity_max=2),
        ),
        student_assignment_locks=(StudentAssignmentLockDTO(
            lock_id=51,
            lock_type="student_group_same_section",
            course_id=1,
            member_student_ids=(1, 2),
        ),),
    ))

    assert result.status == "complete"
    assert {row.section_id for row in result.assignments} == {2}


def test_priority_request_beats_ordinary_primary_for_one_remaining_seat():
    result = solve_student_assignment(_input(
        requests=(
            _request(1, student_id=1),
            _request(2, student_id=2),
        ),
        sections=(_section(capacity_max=1),),
        priority_request_ids=(2,),
        priority_request_limit=100,
    ))

    assert [row.request_id for row in result.assignments] == [2]
    assert result.objective_components["priority_primary_fulfilled"] == 1


def test_strong_schedule_preservation_penalizes_a_move_from_current_enrollment():
    movable = FixedEnrollmentDTO(
        enrollment_id=71,
        student_id=1,
        section_id=2,
        course_offering_id=11,
        course_id=1,
        semester=1,
        timeslot_id=202,
        is_in_scope=True,
    )
    values = dict(
        sections=(
            _section(1, capacity_max=2),
            _section(2, delivery_group_id=2, timeslot_id=202, capacity_max=2),
        ),
        fixed_enrollments=(movable,),
        section_utilization_balance_importance="not_important",
        student_semester_balance_importance="not_important",
        course_sequence_preferences_importance="not_important",
    )

    without_preservation = solve_student_assignment(_input(**values))
    with_strong_preservation = solve_student_assignment(_input(
        **values,
        schedule_preservation_level="strong",
    ))

    assert without_preservation.assignments[0].section_id == 1
    assert with_strong_preservation.assignments[0].section_id == 2
    assert with_strong_preservation.objective_components["schedule_preservation_move_penalty"] == 0


def test_unresolved_request_includes_a_stable_structured_reason_and_remediation():
    result = solve_student_assignment(_input(
        requests=(_request(course_id=9, course_offering_id=99),),
    ))

    unmet = result.unmet_requests[0]
    assert unmet.diagnostic_code == "student_assignment_no_active_placed_section"
    assert unmet.remediation_codes == ("student_assignment_requires_placed_section",)


def test_historical_enrollment_is_audit_context_not_capacity_or_timeslot_context():
    result = solve_student_assignment(_input(
        fixed_enrollments=(FixedEnrollmentDTO(
            enrollment_id=99,
            student_id=1,
            section_id=1,
            course_offering_id=11,
            course_id=1,
            semester=1,
            timeslot_id=101,
            is_historical=True,
        ),),
        sections=(_section(capacity_max=1),),
    ))

    assert result.status == "complete"
    assert result.assignments[0].section_id == 1


def test_active_lock_cost_and_section_review_facts_are_returned():
    result = solve_student_assignment(_input(
        requests=(
            _request(1, student_id=1),
            _request(2, student_id=2),
        ),
        sections=(
            _section(1, capacity_max=0, target_capacity=1),
            _section(2, delivery_group_id=2, timeslot_id=202, capacity_max=1, target_capacity=1),
        ),
        student_assignment_locks=(StudentAssignmentLockDTO(
            lock_id=61,
            lock_type="exact_student_section",
            student_id=1,
            course_id=1,
            section_id=1,
        ),),
    ))

    lock_cost = result.lock_costs[0]
    assert lock_cost.lock_id == 61
    assert lock_cost.unresolved_request_ids == (1,)
    assert lock_cost.attributable_request_count == 1
    assert result.seat_contention[0].section_id == 2
    assert result.seat_contention[0].competing_request_ids == (2,)
    assert result.section_balance_facts[0].diagnostic_code == "student_assignment_section_below_target_capacity"


def test_partial_scope_moves_only_in_scope_requests_and_preserves_out_of_scope_context():
    result = solve_student_assignment(_input(
        requests=(
            _request(1, student_id=1),
            _request(2, student_id=2),
        ),
        sections=(_section(1, capacity_max=1), _section(2, delivery_group_id=2, timeslot_id=202, capacity_max=1)),
        fixed_enrollments=(FixedEnrollmentDTO(
            enrollment_id=80, student_id=2, section_id=1, course_offering_id=11,
            course_id=1, semester=1, timeslot_id=101, is_in_scope=False,
        ),),
        scope=StudentAssignmentScopeDTO(
            scope_type="scoped", student_ids=(1,),
        ),
    ))

    assert {item.student_id for item in result.assignments} == {1}
    assert all(item.student_id != 2 for item in result.assignments)


def test_each_lock_type_is_a_hard_candidate_boundary():
    cases = (
        ("exact_student_section", {"student_id": 1, "course_id": 1, "section_id": 2}, 2),
        ("section_roster", {"section_id": 1}, 2),
        ("course_roster", {"course_id": 1}, None),
        ("whole_student_schedule", {"student_id": 1}, None),
        ("student_teacher_course", {"student_id": 1, "course_id": 1, "teacher_id": 7}, 2),
    )
    for lock_type, targets, expected_section in cases:
        result = solve_student_assignment(_input(
            sections=(_section(1, teacher_id=8), _section(2, delivery_group_id=2, timeslot_id=202, teacher_id=7)),
            student_assignment_locks=(StudentAssignmentLockDTO(
                lock_id=100 + len(lock_type), lock_type=lock_type, **targets,
            ),),
        ))
        if expected_section is None:
            assert result.assignments == ()
            assert result.unmet_requests[0].diagnostic_code == "student_assignment_locked_enrollment_blocks_request"
        else:
            assert result.assignments[0].section_id == expected_section


def test_all_schedule_preservation_levels_protect_a_current_movable_enrollment():
    movable = FixedEnrollmentDTO(
        enrollment_id=91, student_id=1, section_id=2, course_offering_id=11,
        course_id=1, semester=1, timeslot_id=202, is_in_scope=True,
    )
    for level in ("none", "slight", "moderate", "strong"):
        result = solve_student_assignment(_input(
            sections=(_section(1), _section(2, delivery_group_id=2, timeslot_id=202)),
            fixed_enrollments=(movable,),
            section_utilization_balance_importance="not_important",
            student_semester_balance_importance="not_important",
            course_sequence_preferences_importance="not_important",
            schedule_preservation_level=level,
        ))
        assert result.assignments
        if level == "none":
            assert result.assignments[0].section_id == 1
        else:
            assert result.assignments[0].section_id == 2


def test_teacher_lock_only_accepts_the_section_with_the_named_teacher():
    result = solve_student_assignment(_input(
        sections=(_section(1, teacher_id=7), _section(2, delivery_group_id=2, timeslot_id=202, teacher_id=8)),
        student_assignment_locks=(StudentAssignmentLockDTO(
            lock_id=201, lock_type="student_teacher_course", student_id=1,
            course_id=1, teacher_id=8,
        ),),
    ))

    assert result.assignments[0].section_id == 2


def test_unresolved_capacity_reason_identifies_the_competing_section_and_student():
    result = solve_student_assignment(_input(
        requests=(_request(1, student_id=1), _request(2, student_id=2)),
        sections=(_section(capacity_max=1),),
    ))

    unmet = next(item for item in result.unmet_requests if item.request_id == 2)
    assert unmet.diagnostic_code == "student_assignment_section_capacity_exhausted"
    assert unmet.blocking_section_id == 1
    assert unmet.blocking_student_id == 1


def test_requested_study_occupies_one_block_without_becoming_a_course_assignment():
    result = solve_student_assignment(_input(
        requests=(),
        sections=(),
        timeslots=_timeslots(),
        schedule_commitment_requests=(StudentScheduleCommitmentRequestDTO(
            request_id=71, student_id=1, commitment_type="study",
        ),),
    ))

    assert result.status == "complete"
    assert result.assignments == ()
    study = result.commitment_assignments[0]
    assert study.commitment_kind == "study"
    assert len(study.occupancy) == 2
    assert {segment for _timeslot_id, segment in study.occupancy} == {
        "first_half", "second_half",
    }


def test_study_exact_lock_uses_the_counselor_selected_block():
    slots = _timeslots()
    locked_slot = next(slot for slot in slots if slot.semester == 2 and slot.block == "C")
    result = solve_student_assignment(_input(
        requests=(), sections=(), timeslots=slots,
        schedule_commitment_requests=(StudentScheduleCommitmentRequestDTO(
            request_id=72, student_id=1, commitment_type="study",
        ),),
        special_commitment_locks=(StudentSpecialCommitmentLockDTO(
            lock_id=1, lock_type="study_time", lock_mode="exact",
            schedule_commitment_request_id=72, timeslot_id=locked_slot.id,
            semester=2,
        ),),
    ))

    assert {timeslot_id for timeslot_id, _segment in result.commitment_assignments[0].occupancy} == {
        locked_slot.id,
    }


def test_focus_reserves_all_school_blocks_and_excludes_semester_balance():
    slots = _timeslots()
    first_semester_section = _section(timeslot_id=slots[0].id, semester=1)
    result = solve_student_assignment(_input(
        requests=(_request(1, student_id=1),),
        sections=(first_semester_section,),
        timeslots=slots,
        schedule_commitment_requests=(StudentScheduleCommitmentRequestDTO(
            request_id=73, student_id=1, commitment_type="focus",
        ),),
        special_commitment_locks=(StudentSpecialCommitmentLockDTO(
            lock_id=2, lock_type="focus_semester", lock_mode="exact",
            schedule_commitment_request_id=73, semester=2,
        ),),
    ))

    assert result.status == "complete"
    focus = result.commitment_assignments[0]
    assert focus.commitment_kind == "focus"
    assert {timeslot_id for timeslot_id, _segment in focus.occupancy} == {
        slot.id for slot in slots if slot.semester == 2
    }
    assert result.assignments[0].semester == 1
    assert result.objective_components["student_semester_balance_penalty"] == 0


def test_co_op_is_one_two_credit_paired_block_commitment_not_a_section_enrollment():
    slots = _timeslots()
    result = solve_student_assignment(_input(
        requests=(_request(
            74, course_id=9, course_offering_id=99, delivery_kind="co_op",
            credit_value=2.0,
        ),),
        sections=(), timeslots=slots,
        special_commitment_locks=(StudentSpecialCommitmentLockDTO(
            lock_id=3, lock_type="co_op_time", lock_mode="exact",
            course_request_id=74, semester=1, co_op_block_pair="a_b",
        ),),
    ))

    assert result.status == "complete"
    assert result.assignments == ()
    co_op = result.commitment_assignments[0]
    assert co_op.commitment_kind == "co_op"
    assert {timeslot_id for timeslot_id, _segment in co_op.occupancy} == {
        slot.id for slot in slots if slot.semester == 1 and slot.block in {"A", "B"}
    }


def test_half_semester_pair_can_share_a_block_without_a_student_collision():
    result = solve_student_assignment(_input(
        requests=(
            _request(
                75, course_id=1, course_offering_id=11, duration="half_semester",
                credit_value=0.5, half_semester_segment="first_half", paired_half_course_id=2,
            ),
            _request(
                76, course_id=2, course_offering_id=22, duration="half_semester",
                credit_value=0.5, half_semester_segment="second_half", paired_half_course_id=1,
            ),
        ),
        sections=(
            _section(
                1, timeslot_id=101, half_semester_segment="first_half",
                half_semester_pair_key="pair:1",
            ),
            _section(
                2, delivery_group_id=2, member_course_offering_ids=(22,), member_course_ids=(2,),
                timeslot_id=101, half_semester_segment="second_half",
                half_semester_pair_key="pair:1",
            ),
        ),
        section_utilization_balance_importance="not_important",
        student_semester_balance_importance="not_important",
        course_sequence_preferences_importance="not_important",
    ))

    assert result.status == "complete"
    assert {item.section_id for item in result.assignments} == {1, 2}
    assert result.review_items == ()


def test_unpaired_half_semester_course_is_assigned_then_flagged_for_review():
    result = solve_student_assignment(_input(
        requests=(_request(
            77, course_id=1, course_offering_id=11, duration="half_semester",
            credit_value=0.5, half_semester_segment="first_half", paired_half_course_id=2,
        ),),
        sections=(_section(half_semester_segment="first_half"),),
    ))

    assert result.status == "complete"
    assert result.review_items[0].code == "student_assignment_half_semester_unallocated_opposite_half"


def test_half_semester_online_keeps_full_term_supervision_and_flags_unused_half():
    result = solve_student_assignment(_input(
        requests=(_request(
            78, course_id=1, course_offering_id=11, delivery_kind="online",
            duration="half_semester", credit_value=0.5,
            half_semester_segment="first_half", paired_half_course_id=2,
        ),),
        sections=(_section(-1, timeslot_id=101),),
    ))

    assignment = result.assignments[0]
    assert assignment.section_id is None
    assert assignment.online_supervision_session_id == 1
    assert assignment.half_semester_segment == "first_half"
    assert {item.code for item in result.review_items} == {
        "student_assignment_half_semester_unallocated_opposite_half",
        "student_assignment_online_half_semester_unused_supervision_half",
    }


def test_unknown_solver_outcome_is_failed_not_reported_as_infeasible(monkeypatch):
    """A bounded search timeout cannot be presented as a proof of impossibility."""

    monkeypatch.setattr(
        student_assignment_module,
        "_solve_lexicographically",
        lambda *_args, **_kwargs: (None, cp_model.UNKNOWN),
    )

    result = solve_student_assignment(_input())

    assert result.status == "failed"
    assert result.solver_outcome == "unknown"


class _ControlledSolver:
    """Small CP-SAT stand-in for deterministic orchestration timeout tests."""

    def __init__(self, status, value=0):
        self.status = status
        self.value = value
        self.solve_calls = 0

    def Solve(self, _model):
        self.solve_calls += 1
        return self.status

    def Value(self, _expression):
        return self.value


def test_later_lexicographic_timeout_returns_the_prior_valid_incumbent(monkeypatch):
    """A lower-priority timeout must not erase a higher-priority candidate."""

    model = cp_model.CpModel()
    mandatory = model.NewBoolVar("mandatory")
    primary = model.NewBoolVar("primary")
    incumbent = _ControlledSolver(cp_model.OPTIMAL, value=0)
    timed_out = _ControlledSolver(cp_model.UNKNOWN)
    solvers = iter((incumbent, timed_out))
    monkeypatch.setattr(
        student_assignment_module,
        "_new_solver",
        lambda *_args, **_kwargs: next(solvers),
    )

    solver, outcome = student_assignment_module._solve_lexicographically(
        model,
        (mandatory, primary),
        1.0,
    )

    assert solver is incumbent
    assert outcome == cp_model.UNKNOWN
    assert incumbent.solve_calls == 1
    assert timed_out.solve_calls == 1


def test_lexicographic_solver_skips_constant_objective_slots(monkeypatch):
    """An empty priority tier has no value and must not trigger a cold solve."""

    model = cp_model.CpModel()
    mandatory = model.NewBoolVar("mandatory")
    solver = _ControlledSolver(cp_model.OPTIMAL, value=0)
    monkeypatch.setattr(
        student_assignment_module,
        "_new_solver",
        lambda *_args, **_kwargs: solver,
    )

    returned_solver, outcome = student_assignment_module._solve_lexicographically(
        model,
        (0, mandatory),
        1.0,
    )

    assert returned_solver is solver
    assert outcome == cp_model.FEASIBLE
    assert solver.solve_calls == 1


def test_all_constant_objectives_still_return_a_reviewable_feasibility_result(monkeypatch):
    """A fully protected rerun has no decisions but is valid fixed context."""

    model = cp_model.CpModel()
    solver = _ControlledSolver(cp_model.OPTIMAL)
    monkeypatch.setattr(
        student_assignment_module,
        "_new_solver",
        lambda *_args, **_kwargs: solver,
    )

    returned_solver, outcome = student_assignment_module._solve_lexicographically(
        model,
        (0, 0),
        1.0,
    )

    assert returned_solver is solver
    assert outcome == cp_model.OPTIMAL
    assert solver.solve_calls == 1


def test_lexicographic_infeasibility_without_an_incumbent_remains_infeasible(monkeypatch):
    model = cp_model.CpModel()
    mandatory = model.NewBoolVar("mandatory")
    solver = _ControlledSolver(cp_model.INFEASIBLE)
    monkeypatch.setattr(
        student_assignment_module,
        "_new_solver",
        lambda *_args, **_kwargs: solver,
    )

    returned_solver, outcome = student_assignment_module._solve_lexicographically(
        model,
        (mandatory,),
        1.0,
    )

    assert returned_solver is None
    assert outcome == cp_model.INFEASIBLE
