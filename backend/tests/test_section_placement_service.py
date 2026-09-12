"""Transactional annual placement approval contracts."""

from decimal import Decimal

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
from backend.apps.courses.models import Course, HalfSemesterCoursePair, HalfSemesterSectionPair
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
    SectionPlacementValidationError,
    approve_section_placement_run,
    create_section_placement_run,
)
from backend.apps.scheduling.codes import HALF_SEMESTER_PAIR_PLACEMENT_INVALID


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


def _annual_half_pair_context(academic_year, course, counselor_user, teacher_user):
    """Build two annual first/second-half positions through real services."""

    course.duration = "half_semester"
    course.credit_value = Decimal("0.5")
    course.save()
    second = Course.objects.create(
        name="Paired second-half course",
        grade_level=course.grade_level,
        course_code=f"{course.course_code}-HALF-SECOND",
        category=course.category,
        duration="half_semester",
        credit_value=Decimal("0.5"),
        capacity_profile=course.capacity_profile,
        priority_profile=course.priority_profile,
    )
    pair = HalfSemesterCoursePair.objects.create(
        first_course=course,
        second_course=second,
    )
    offerings = {
        item.course_id: item
        for item in ensure_academic_year_offerings(academic_year, actor=counselor_user)
    }
    first_group = offerings[course.id].delivery_group
    second_group = offerings[second.id].delivery_group
    qualification = Qualification.objects.create(
        code="paired-half", name="Paired half qualification", kind="teachable",
        subject_code="mathematics", division=QUALIFICATION_DIVISION_SENIOR,
    )
    for half_course in (course, second):
        CourseQualificationRequirement.objects.create(
            course=half_course,
            qualification=qualification,
        )
    TeacherQualification.objects.create(
        teacher=teacher_user.teacher_profile,
        qualification=qualification,
        review_status=QUALIFICATION_REVIEW_VERIFIED,
    )
    for semester in (1, 2):
        for block in (SCHEDULE_BLOCK_A, SCHEDULE_BLOCK_B):
            TimeSlot.objects.create(academic_year=academic_year, semester=semester, block=block)
        TeacherPlanningCapacity.objects.create(
            teacher=teacher_user.teacher_profile,
            academic_year=academic_year,
            semester=semester,
            maximum_sections=2,
        )
    roster = TeacherPlanningRoster.objects.create(academic_year=academic_year, status="ready")
    TeacherPlanningRosterMember.objects.create(
        roster=roster,
        teacher=teacher_user.teacher_profile,
        added_by=counselor_user,
    )
    matrix = CourseConflictMatrix.objects.create(
        academic_year=academic_year,
        initialization_mode="fresh_current_demand",
        created_by=counselor_user,
    )
    budget_run = SectionBudgetRun.objects.create(
        academic_year=academic_year,
        created_by=counselor_user,
        status="complete",
        budget_type="exact",
        section_budget=4,
    )
    budget = SectionBudgetApproval.objects.create(
        budget_run=budget_run,
        approved_by=counselor_user,
        reason="Approve paired half-course annual positions.",
    )
    for group in (first_group, second_group):
        SectionBudgetApprovalOffering.objects.create(
            approval=budget,
            delivery_group=group,
            recommended_annual_count=2,
            recommended_semester_1_count=1,
            recommended_semester_2_count=1,
            approved_annual_count=2,
            approved_semester_1_count=1,
            approved_semester_2_count=1,
        )
    return pair, create_section_placement_run(
        academic_year_id=academic_year.id,
        input_mode="annual_total",
        budget_approval=budget,
        created_by=counselor_user,
    )


@pytest.mark.django_db
def test_annual_placement_materializes_deterministic_co_timed_half_pairs(
    academic_year, course, counselor_user, teacher_user,
):
    pair, run = _annual_half_pair_context(
        academic_year, course, counselor_user, teacher_user,
    )

    assert run.status == "complete"
    shared_keys = [
        item["shared_placement_key"]
        for item in run.input_snapshot["units"]
        if item.get("shared_placement_key")
    ]
    assert sorted(shared_keys) == [
        f"half_semester_course_pair:{pair.id}:annual:1",
        f"half_semester_course_pair:{pair.id}:annual:1",
        f"half_semester_course_pair:{pair.id}:annual:2",
        f"half_semester_course_pair:{pair.id}:annual:2",
    ]

    approve_section_placement_run(
        run,
        approved_by=counselor_user,
        reason="Approve solver-derived co-timed half-course positions.",
    )
    pairs = list(HalfSemesterSectionPair.objects.filter(course_pair=pair).select_related(
        "first_section", "second_section",
    ).order_by("id"))
    assert len(pairs) == 2
    schedules = {
        item.section_id: item.timeslot_id
        for item in SectionSchedule.objects.filter(
            section_id__in=[
                section_id
                for row in pairs
                for section_id in (row.first_section_id, row.second_section_id)
            ]
        )
    }
    assert all(row.first_section.semester == row.second_section.semester for row in pairs)
    assert all(schedules[row.first_section_id] == schedules[row.second_section_id] for row in pairs)
    assert all(row.first_section.capacity_max == row.second_section.capacity_max for row in pairs)
    assert all(row.first_section.half_semester_segment == "first_half" for row in pairs)
    assert all(row.second_section.half_semester_segment == "second_half" for row in pairs)


@pytest.mark.django_db
def test_annual_placement_approval_rejects_a_split_half_pair_result(
    academic_year, course, counselor_user, teacher_user,
):
    _pair, run = _annual_half_pair_context(
        academic_year, course, counselor_user, teacher_user,
    )
    assignments = [dict(item) for item in run.result["assignments"]]
    paired = [
        item for item in assignments
        if item["unit_key"].endswith(":1") and item["unit_key"].startswith("annual:")
    ]
    assert len(paired) == 2
    target = paired[1]
    alternate = TimeSlot.objects.exclude(id=target["timeslot_id"]).filter(
        academic_year=academic_year,
        semester=target["semester"],
    ).first()
    assert alternate is not None
    target["timeslot_id"] = alternate.id
    target["block"] = alternate.block
    # The model intentionally makes reviewed runs immutable. A direct test-only
    # database update simulates a corrupted/crafted solver payload so approval
    # itself proves it refuses split physical pairs.
    type(run).objects.filter(pk=run.pk).update(
        result={**run.result, "assignments": assignments},
    )
    run.refresh_from_db()

    with pytest.raises(SectionPlacementValidationError) as error:
        approve_section_placement_run(
            run,
            approved_by=counselor_user,
            reason="Reject crafted split pair.",
        )

    assert error.value.detail["code"] == HALF_SEMESTER_PAIR_PLACEMENT_INVALID
    assert HalfSemesterSectionPair.objects.count() == 0
