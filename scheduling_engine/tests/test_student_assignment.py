"""Contracts for pure student-to-section assignment; no Django dependency."""

from scheduling_engine.dto import (
    CoursePrerequisiteDTO,
    CourseSequencePreferenceDTO,
    FixedEnrollmentDTO,
    StudentAssignmentInputDTO,
    StudentAssignmentRequestDTO,
    StudentAssignmentSectionDTO,
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
