"""Contracts for the pure named-teacher stage; no Django fixtures are used."""

from scheduling_engine.dto import (
    TeacherAssignmentInputDTO, TeacherAssignmentSectionDTO,
    TeacherAssignmentTeacherDTO, TeacherCourseAssignmentRuleDTO,
)
from scheduling_engine.teacher_assignment import solve_teacher_assignment


def _teacher(teacher_id=1, **overrides):
    values = dict(
        id=teacher_id, eligible_course_ids=(1,), remaining_semester_1=3,
        remaining_semester_2=3, remaining_annual=6,
    )
    values.update(overrides)
    return TeacherAssignmentTeacherDTO(**values)


def _section(section_id=1, **overrides):
    values = dict(
        section_id=section_id, delivery_group_id=1, member_course_ids=(1,),
        semester=1, timeslot_id=10,
    )
    values.update(overrides)
    return TeacherAssignmentSectionDTO(**values)


def test_assigns_legal_teacher_and_keeps_timing_fixed():
    result = solve_teacher_assignment(TeacherAssignmentInputDTO(
        academic_year_id=1, sections=(_section(),), teachers=(_teacher(),),
    ))

    assert result.status == "complete"
    assert result.assignments[0].teacher_id == 1
    assert result.assignments[0].timeslot_id == 10


def test_locked_teacher_is_hard_requirement():
    result = solve_teacher_assignment(TeacherAssignmentInputDTO(
        academic_year_id=1,
        sections=(_section(locked_teacher_id=2),),
        teachers=(_teacher(), _teacher(2)),
    ))

    assert result.status == "complete"
    assert result.assignments[0].teacher_id == 2


def test_explicit_unavailability_is_hard_but_absence_is_available():
    result = solve_teacher_assignment(TeacherAssignmentInputDTO(
        academic_year_id=1, sections=(_section(),),
        teachers=(_teacher(unavailable_timeslot_ids=(10,)),),
    ))

    assert result.status == "partial"
    assert result.unassigned_section_ids == (1,)
    assert result.diagnostics[0]["code"] == "no_eligible_teacher_for_section"


def test_teacher_cannot_cover_two_sections_in_same_timeslot():
    result = solve_teacher_assignment(TeacherAssignmentInputDTO(
        academic_year_id=1, sections=(_section(1), _section(2)), teachers=(_teacher(),),
    ))

    assert result.status == "partial"
    assert len(result.assignments) == 1


def test_course_rules_enforce_exact_annual_count():
    result = solve_teacher_assignment(TeacherAssignmentInputDTO(
        academic_year_id=1,
        sections=(_section(1, timeslot_id=10), _section(2, timeslot_id=11)),
        teachers=(_teacher(), _teacher(2)),
        rules=(TeacherCourseAssignmentRuleDTO(teacher_id=1, course_id=1, minimum_sections=2, maximum_sections=2),),
    ))

    assert result.status == "complete"
    assert {row.teacher_id for row in result.assignments} == {1}


def test_requested_course_precedes_prior_year_continuity():
    result = solve_teacher_assignment(TeacherAssignmentInputDTO(
        academic_year_id=1, sections=(_section(),),
        teachers=(
            _teacher(1, preferred_course_ids=(1,)),
            _teacher(2, prior_year_course_ids=(1,)),
        ),
    ))

    assert result.status == "complete"
    assert result.assignments[0].teacher_id == 1


def test_combined_section_requires_intersection_of_eligibility():
    result = solve_teacher_assignment(TeacherAssignmentInputDTO(
        academic_year_id=1,
        sections=(_section(member_course_ids=(1, 2)),),
        teachers=(_teacher(1, eligible_course_ids=(1,)), _teacher(2, eligible_course_ids=(1, 2))),
    ))

    assert result.status == "complete"
    assert result.assignments[0].teacher_id == 2
