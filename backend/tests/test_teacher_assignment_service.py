"""End-to-end transactional contracts for named teacher approval."""

import pytest

from backend.apps.common.constants import (
    QUALIFICATION_DIVISION_SENIOR,
    QUALIFICATION_REVIEW_VERIFIED,
    SCHEDULE_BLOCK_A,
)
from backend.apps.constraints.models import (
    CourseQualificationRequirement, Qualification, TeacherQualification,
)
from backend.apps.courses.models import Section
from backend.apps.courses.services.offerings import ensure_academic_year_offerings
from backend.apps.scheduling.models import (
    SectionSchedule, TeacherAssignmentApprovalAssignment,
    TeacherPlanningAnnualCapacity, TeacherPlanningCapacity, TeacherPlanningRoster,
    TeacherPlanningRosterMember, TimeSlot,
)
from backend.apps.scheduling.services.teacher_assignment import (
    approve_teacher_assignment_run, create_teacher_assignment_run,
)


@pytest.mark.django_db
def test_complete_teacher_run_writes_only_teacher_provenance(
    academic_year, course, counselor_user, teacher_user,
):
    """Approval must assign the named teacher without adding a room or enrollment."""

    group = ensure_academic_year_offerings(academic_year, actor=counselor_user)[0].delivery_group
    qualification = Qualification.objects.create(
        code="teacher-stage-senior", name="Teacher stage senior", kind="teachable",
        subject_code="mathematics", division=QUALIFICATION_DIVISION_SENIOR,
    )
    CourseQualificationRequirement.objects.create(course=course, qualification=qualification)
    TeacherQualification.objects.create(
        teacher=teacher_user.teacher_profile, qualification=qualification,
        review_status=QUALIFICATION_REVIEW_VERIFIED,
    )
    slot = TimeSlot.objects.create(academic_year=academic_year, semester=1, block=SCHEDULE_BLOCK_A)
    section = Section.objects.create(
        course=course, delivery_group=group, section_number="S1-01", academic_year=academic_year,
        semester=1, capacity_min=10, capacity_max=30,
    )
    SectionSchedule.objects.create(section=section, timeslot=slot, room=None)
    roster = TeacherPlanningRoster.objects.create(academic_year=academic_year, status="ready")
    TeacherPlanningRosterMember.objects.create(roster=roster, teacher=teacher_user.teacher_profile, added_by=counselor_user)
    for semester in (1, 2):
        TeacherPlanningCapacity.objects.create(
            teacher=teacher_user.teacher_profile, academic_year=academic_year,
            semester=semester, maximum_sections=3,
        )
    TeacherPlanningAnnualCapacity.objects.create(
        teacher=teacher_user.teacher_profile, academic_year=academic_year, maximum_sections=6,
    )

    run = create_teacher_assignment_run(academic_year_id=academic_year.id, created_by=counselor_user)

    assert run.status == "complete"
    approval = approve_teacher_assignment_run(run, approved_by=counselor_user, reason="Counselor approved teacher allocation.")
    section.refresh_from_db()
    line = TeacherAssignmentApprovalAssignment.objects.get(approval=approval)
    assert section.teacher_id == teacher_user.teacher_profile.id
    assert line.section_id == section.id
    assert SectionSchedule.objects.get(section=section).room_id is None
