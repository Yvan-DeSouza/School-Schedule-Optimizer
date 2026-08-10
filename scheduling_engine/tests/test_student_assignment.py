"""Contracts for pure student-to-section assignment; no Django dependency."""

from ortools.sat.python import cp_model

import scheduling_engine.student_assignment as student_assignment_module
from scheduling_engine.dto import (
    CoursePrerequisiteDTO,
    CourseSequencePreferenceDTO,
    FixedEnrollmentDTO,
    StudentAssignmentInputDTO,
    StudentAssignmentLockDTO,
    StudentAssignmentRequestDTO,
    StudentAssignmentSectionDTO,
    StudentAssignmentScopeDTO,
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
