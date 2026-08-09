"""Transactional annual placement approval contracts."""

import pytest

from backend.apps.common.constants import (
    QUALIFICATION_DIVISION_SENIOR,
    QUALIFICATION_REVIEW_VERIFIED,
    SCHEDULE_BLOCK_A,
    SCHEDULE_BLOCK_B,
)
from backend.apps.constraints.models import (
    CourseConflictMatrix,
    CourseQualificationRequirement,
    Qualification,
    TeacherQualification,
)
from backend.apps.courses.services.offerings import ensure_academic_year_offerings
from backend.apps.scheduling.models import (
    SectionBudgetApproval,
    SectionBudgetApprovalOffering,
    SectionBudgetRun,
    SectionPlacementApprovalAssignment,
    SectionSchedule,
    TeacherPlanningCapacity,
    TeacherPlanningRoster,
    TeacherPlanningRosterMember,
    TimeSlot,
)
from backend.apps.scheduling.services.section_placement import (
    approve_section_placement_run,
    create_section_placement_run,
)


@pytest.mark.django_db
def test_annual_approval_materializes_timeslot_only_sections(
    academic_year, course, counselor_user, teacher_user,
):
    group = ensure_academic_year_offerings(academic_year, actor=counselor_user)[0].delivery_group
    qualification = Qualification.objects.create(
        code="math-senior", name="Mathematics Senior", kind="teachable",
        subject_code="mathematics", division=QUALIFICATION_DIVISION_SENIOR,
    )
    CourseQualificationRequirement.objects.create(course=course, qualification=qualification)
    TeacherQualification.objects.create(
        teacher=teacher_user.teacher_profile, qualification=qualification,
        review_status=QUALIFICATION_REVIEW_VERIFIED,
    )
    for semester, block in ((1, SCHEDULE_BLOCK_A), (1, SCHEDULE_BLOCK_B), (2, SCHEDULE_BLOCK_A), (2, SCHEDULE_BLOCK_B)):
        TimeSlot.objects.create(academic_year=academic_year, semester=semester, block=block)
    roster = TeacherPlanningRoster.objects.create(academic_year=academic_year, status="ready")
    TeacherPlanningRosterMember.objects.create(roster=roster, teacher=teacher_user.teacher_profile, added_by=counselor_user)
    for semester in (1, 2):
        TeacherPlanningCapacity.objects.create(
            teacher=teacher_user.teacher_profile, academic_year=academic_year,
            semester=semester, maximum_sections=2,
        )
    matrix = CourseConflictMatrix.objects.create(
        academic_year=academic_year, initialization_mode="fresh_current_demand", created_by=counselor_user,
    )
    budget_run = SectionBudgetRun.objects.create(
        academic_year=academic_year, created_by=counselor_user, status="complete",
        budget_type="exact", section_budget=1,
    )
    budget_approval = SectionBudgetApproval.objects.create(
        budget_run=budget_run, approved_by=counselor_user, reason="Approved annual count.",
    )
    SectionBudgetApprovalOffering.objects.create(
        approval=budget_approval, delivery_group=group,
        recommended_annual_count=1, recommended_semester_1_count=1, recommended_semester_2_count=0,
        approved_annual_count=1, approved_semester_1_count=1, approved_semester_2_count=0,
    )
    run = create_section_placement_run(
        academic_year_id=academic_year.id, input_mode="annual_total",
        budget_approval=budget_approval, created_by=counselor_user,
    )
    assert run.status == "complete"
    approval = approve_section_placement_run(
        run, approved_by=counselor_user, reason="Approved timing after reviewing feasibility.",
    )
    assignment = SectionPlacementApprovalAssignment.objects.get(approval=approval)
    schedule = SectionSchedule.objects.get(section=assignment.section)
    assert schedule.timeslot_id == assignment.timeslot_id
    assert schedule.room_id is None
    assert assignment.section.teacher_id is None
    assert assignment.section.annual_placement_approval_id == approval.id
