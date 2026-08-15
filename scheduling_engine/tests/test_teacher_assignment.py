"""Contracts for the pure named-teacher stage; no Django fixtures are used."""

from scheduling_engine.dto import (
    FixedTeacherAssignmentDTO, TeacherAssignmentInputDTO, TeacherAssignmentSectionDTO,
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


def test_online_supervision_uses_normal_workload_but_no_subject_qualification():
    result = solve_teacher_assignment(TeacherAssignmentInputDTO(
        academic_year_id=1,
        sections=(TeacherAssignmentSectionDTO(
            section_id=None, delivery_group_id=-9, member_course_ids=(), semester=1,
            timeslot_id=10, is_online_supervision=True,
            online_supervision_session_id=9,
        ),),
        teachers=(_teacher(eligible_course_ids=()),),
    ))

    assert result.status == "complete"
    assert result.assignments[0].online_supervision_session_id == 9
    assert result.assignments[0].teacher_id == 1


def test_online_supervision_cannot_share_a_teacher_with_a_normal_section_at_one_time():
    result = solve_teacher_assignment(TeacherAssignmentInputDTO(
        academic_year_id=1,
        sections=(
            _section(1, timeslot_id=10),
            TeacherAssignmentSectionDTO(
                section_id=None, delivery_group_id=-9, member_course_ids=(), semester=1,
                timeslot_id=10, is_online_supervision=True,
                online_supervision_session_id=9,
            ),
        ),
        teachers=(_teacher(),),
    ))

    assert result.status == "partial"
    assert len(result.assignments) == 1


def test_paired_half_semester_sections_share_one_teacher_workload_slot():
    """Sequential trimestre teaching is one load, despite two course identities."""

    result = solve_teacher_assignment(TeacherAssignmentInputDTO(
        academic_year_id=1,
        sections=(
            _section(1, timeslot_id=10, shared_staffing_key="half:1"),
            _section(
                2, delivery_group_id=2, member_course_ids=(2,), timeslot_id=10,
                shared_staffing_key="half:1",
            ),
        ),
        teachers=(_teacher(
            eligible_course_ids=(1, 2), remaining_semester_1=1,
            remaining_semester_2=0, remaining_annual=1,
        ),),
    ))

    assert result.status == "complete"
    assert {item.section_id for item in result.assignments} == {1, 2}
    assert {item.teacher_id for item in result.assignments} == {1}


def test_candidate_ledger_records_static_qualification_and_availability_exclusions():
    result = solve_teacher_assignment(TeacherAssignmentInputDTO(
        academic_year_id=1,
        sections=(_section(),),
        teachers=(
            _teacher(1),
            _teacher(2, eligible_course_ids=()),
            _teacher(3, unavailable_timeslot_ids=(10,)),
        ),
    ))

    ledger = result.candidate_ledger[0]
    candidates = {item["teacher_id"]: item for item in ledger.candidates}
    assert ledger.selected_teacher_id == 1
    assert candidates[1]["comparison_state"] == "selected"
    assert candidates[2]["static_rejections"][0]["code"] == (
        "teacher_assignment_qualification_unavailable"
    )
    assert candidates[3]["static_rejections"][0]["code"] == (
        "teacher_assignment_teacher_unavailable"
    )


def test_candidate_ledger_records_exact_teacher_lock_mismatch():
    result = solve_teacher_assignment(TeacherAssignmentInputDTO(
        academic_year_id=1,
        sections=(_section(locked_teacher_id=1),),
        teachers=(_teacher(1), _teacher(2)),
    ))

    candidate = next(
        item for item in result.candidate_ledger[0].candidates if item["teacher_id"] == 2
    )
    assert candidate["static_rejections"][0]["code"] == (
        "teacher_assignment_exact_teacher_locked_elsewhere"
    )


def test_candidate_ledger_marks_eligible_unselected_teacher_as_possible_in_isolation():
    result = solve_teacher_assignment(TeacherAssignmentInputDTO(
        academic_year_id=1,
        sections=(_section(),),
        teachers=(_teacher(1, preferred_course_ids=(1,)), _teacher(2)),
    ))

    candidates = {item["teacher_id"]: item for item in result.candidate_ledger[0].candidates}
    assert candidates[1]["comparison_state"] == "selected"
    assert candidates[2]["comparison_state"] == (
        "possible_in_isolation_global_comparison_not_yet_proven"
    )


def test_online_supervision_candidate_evidence_bypasses_only_qualification():
    result = solve_teacher_assignment(TeacherAssignmentInputDTO(
        academic_year_id=1,
        sections=(TeacherAssignmentSectionDTO(
            section_id=None, delivery_group_id=-9, member_course_ids=(), semester=1,
            timeslot_id=10, is_online_supervision=True,
            online_supervision_session_id=9,
        ),),
        teachers=(_teacher(eligible_course_ids=()),),
    ))

    ledger = result.candidate_ledger[0]
    assert ledger.decision_kind == "online_supervision"
    assert ledger.candidates[0]["qualification_evaluation"] == (
        "not_applicable_online_supervision"
    )


def test_online_supervision_candidate_evidence_keeps_availability_and_exact_lock_rules():
    """Online supervision omits subject qualification, not ordinary staffing rules."""

    result = solve_teacher_assignment(TeacherAssignmentInputDTO(
        academic_year_id=1,
        sections=(TeacherAssignmentSectionDTO(
            section_id=None, delivery_group_id=-9, member_course_ids=(), semester=1,
            timeslot_id=10, is_online_supervision=True,
            online_supervision_session_id=9, locked_teacher_id=1,
        ),),
        teachers=(
            _teacher(1, eligible_course_ids=(), unavailable_timeslot_ids=(10,)),
            _teacher(2, eligible_course_ids=()),
        ),
    ))

    candidates = {
        item["teacher_id"]: item for item in result.candidate_ledger[0].candidates
    }
    assert candidates[1]["qualification_evaluation"] == (
        "not_applicable_online_supervision"
    )
    assert candidates[1]["static_rejections"][0]["code"] == (
        "teacher_assignment_teacher_unavailable"
    )
    assert candidates[2]["static_rejections"][0]["code"] == (
        "teacher_assignment_exact_teacher_locked_elsewhere"
    )


def test_online_supervision_candidate_evidence_records_returned_timeslot_collision():
    """A supervisor remains subject to the final timetable collision check."""

    result = solve_teacher_assignment(TeacherAssignmentInputDTO(
        academic_year_id=1,
        sections=(
            _section(1, timeslot_id=10),
            TeacherAssignmentSectionDTO(
                section_id=None, delivery_group_id=-9, member_course_ids=(), semester=1,
                timeslot_id=10, is_online_supervision=True,
                online_supervision_session_id=9,
            ),
        ),
        teachers=(
            _teacher(1, preferred_course_ids=(1,)),
            _teacher(2),
        ),
    ))

    online_ledger = next(
        item for item in result.candidate_ledger
        if item.decision_kind == "online_supervision"
    )
    candidate = next(item for item in online_ledger.candidates if item["teacher_id"] == 1)
    assert online_ledger.selected_teacher_id == 2
    assert candidate["qualification_evaluation"] == (
        "not_applicable_online_supervision"
    )
    assert candidate["final_rejections"][0]["code"] == "teacher_timeslot_collision"
    assert candidate["comparison_state"] == "blocked_by_returned_solution"


def test_half_semester_pair_uses_one_candidate_ledger_decision():
    result = solve_teacher_assignment(TeacherAssignmentInputDTO(
        academic_year_id=1,
        sections=(
            _section(1, timeslot_id=10, shared_staffing_key="half:1"),
            _section(
                2, delivery_group_id=2, member_course_ids=(2,), timeslot_id=10,
                shared_staffing_key="half:1",
            ),
        ),
        teachers=(_teacher(eligible_course_ids=(1, 2)),),
    ))

    assert len(result.candidate_ledger) == 1
    assert result.candidate_ledger[0].decision_kind == "half_semester_pair"
    assert result.candidate_ledger[0].section_ids == (1, 2)


def test_candidate_ledger_retains_fixed_staffing_as_fixed_context_not_an_alternative():
    result = solve_teacher_assignment(TeacherAssignmentInputDTO(
        academic_year_id=1,
        sections=(_section(is_fixed=True, assigned_teacher_id=1),),
        teachers=(_teacher(1), _teacher(2)),
        fixed_assignments=(FixedTeacherAssignmentDTO(
            section_id=1, teacher_id=1, semester=1, timeslot_id=10, member_course_ids=(1,),
        ),),
    ))

    ledger = result.candidate_ledger[0]
    assert ledger.selection_state == "fixed_context"
    assert ledger.selected_teacher_id == 1
    assert ledger.candidates == ()


def test_candidate_ledger_records_returned_timeslot_collision_without_claiming_global_proof():
    result = solve_teacher_assignment(TeacherAssignmentInputDTO(
        academic_year_id=1,
        sections=(
            _section(1, member_course_ids=(1,), timeslot_id=10),
            _section(2, member_course_ids=(2,), timeslot_id=10),
        ),
        teachers=(
            _teacher(1, eligible_course_ids=(1,), preferred_course_ids=(1,)),
            _teacher(2, eligible_course_ids=(1, 2)),
        ),
    ))

    ledger = next(item for item in result.candidate_ledger if item.section_ids == (1,))
    candidate = next(item for item in ledger.candidates if item["teacher_id"] == 2)
    assert candidate["final_rejections"][0]["code"] == "teacher_timeslot_collision"
    assert candidate["comparison_state"] == "blocked_by_returned_solution"


def test_candidate_ledger_records_returned_semester_and_annual_capacity_exhaustion():
    result = solve_teacher_assignment(TeacherAssignmentInputDTO(
        academic_year_id=1,
        sections=(_section(1, timeslot_id=10), _section(2, timeslot_id=11)),
        teachers=(
            _teacher(1, preferred_course_ids=(1,), remaining_semester_1=1, remaining_annual=1),
            _teacher(2, remaining_semester_1=1, remaining_annual=1),
        ),
    ))

    teacher_one_decision = next(
        item for item in result.candidate_ledger if item.selected_teacher_id == 1
    )
    candidate = next(item for item in teacher_one_decision.candidates if item["teacher_id"] == 2)
    assert {item["code"] for item in candidate["final_rejections"]} == {
        "teacher_assignment_semester_capacity_exhausted",
        "teacher_assignment_annual_capacity_exhausted",
    }
    assert candidate["comparison_state"] == "blocked_by_returned_solution"


def test_candidate_ledger_records_course_rule_maximum_as_a_static_exclusion():
    result = solve_teacher_assignment(TeacherAssignmentInputDTO(
        academic_year_id=1,
        sections=(_section(),),
        teachers=(_teacher(1), _teacher(2)),
        rules=(TeacherCourseAssignmentRuleDTO(
            teacher_id=2, course_id=1, maximum_sections=0,
        ),),
    ))

    candidate = next(
        item for item in result.candidate_ledger[0].candidates if item["teacher_id"] == 2
    )
    assert candidate["static_rejections"][0]["code"] == (
        "teacher_assignment_course_rule_maximum_reached"
    )
