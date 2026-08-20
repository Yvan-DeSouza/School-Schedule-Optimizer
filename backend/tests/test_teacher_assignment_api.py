"""HTTP review contract for named-teacher candidate evidence."""

import pytest

from backend.apps.common.constants import (
    QUALIFICATION_DIVISION_SENIOR,
    QUALIFICATION_REVIEW_VERIFIED,
    SCHEDULE_BLOCK_A,
)
from backend.apps.constraints.models import (
    CourseQualificationRequirement,
    Qualification,
    TeacherQualification,
)
from backend.apps.courses.models import Section
from backend.apps.courses.services.offerings import ensure_academic_year_offerings
from backend.apps.scheduling.models import (
    SectionSchedule,
    TeacherPlanningAnnualCapacity,
    TeacherPlanningCapacity,
    TeacherPlanningRoster,
    TeacherPlanningRosterMember,
    TimeSlot,
    SchedulingExecution,
)


def _create_accepted_teacher_assignment_context(
    *, academic_year, course, counselor_user, teacher, is_fixed=False,
):
    """Build real accepted timing and ready-roster input for the API contract."""

    group = ensure_academic_year_offerings(academic_year, actor=counselor_user)[0].delivery_group
    slot = TimeSlot.objects.create(
        academic_year=academic_year,
        semester=1,
        block=SCHEDULE_BLOCK_A,
    )
    section = Section.objects.create(
        course=course,
        delivery_group=group,
        section_number="LEDGER-01",
        academic_year=academic_year,
        semester=1,
        capacity_min=10,
        capacity_max=30,
        teacher=teacher if is_fixed else None,
    )
    SectionSchedule.objects.create(section=section, timeslot=slot, room=None)
    roster = TeacherPlanningRoster.objects.create(academic_year=academic_year, status="ready")
    TeacherPlanningRosterMember.objects.create(
        roster=roster,
        teacher=teacher,
        added_by=counselor_user,
    )
    for semester in (1, 2):
        TeacherPlanningCapacity.objects.create(
            teacher=teacher,
            academic_year=academic_year,
            semester=semester,
            maximum_sections=3,
        )
    TeacherPlanningAnnualCapacity.objects.create(
        teacher=teacher,
        academic_year=academic_year,
        maximum_sections=6,
    )
    return section, slot


@pytest.mark.django_db
def test_teacher_assignment_review_exposes_persisted_candidate_ledger(
    authenticated_client, academic_year, course, counselor_user, teacher_user,
    run_scheduling_task_inline,
):
    """The API returns evidence saved with the immutable reviewed run."""

    qualification = Qualification.objects.create(
        code="teacher-ledger-senior",
        name="Teacher ledger senior",
        kind="teachable",
        subject_code="mathematics",
        division=QUALIFICATION_DIVISION_SENIOR,
    )
    CourseQualificationRequirement.objects.create(course=course, qualification=qualification)
    TeacherQualification.objects.create(
        teacher=teacher_user.teacher_profile,
        qualification=qualification,
        review_status=QUALIFICATION_REVIEW_VERIFIED,
    )
    section, slot = _create_accepted_teacher_assignment_context(
        academic_year=academic_year,
        course=course,
        counselor_user=counselor_user,
        teacher=teacher_user.teacher_profile,
    )

    client = authenticated_client(counselor_user)
    created = client.post(
        "/api/planning/teacher-assignment-runs/",
        {"academic_year": academic_year.id},
        format="json",
    )

    assert created.status_code == 202, created.data
    execution = SchedulingExecution.objects.get(pk=created.data["id"])
    assert execution.status == "completed", execution.error_detail
    run_id = execution.result_id
    review = client.get(f"/api/planning/teacher-assignment-runs/{run_id}/review/")
    assert review.status_code == 200, review.data
    assert review.data["candidate_ledger"] == [{
        "decision_kind": "section",
        "section_ids": [section.id],
        "online_supervision_session_id": None,
        "shared_staffing_key": None,
        "semester": 1,
        "timeslot_id": slot.id,
        "selection_state": "selected",
        "selected_teacher_id": teacher_user.teacher_profile.id,
        "candidates": [{
            "teacher_id": teacher_user.teacher_profile.id,
            "is_statically_eligible": True,
            "qualification_evaluation": "required",
            "static_rejections": [],
            "final_rejections": [],
            "is_selected": True,
            "comparison_state": "selected",
        }],
        "selection_factors": [{
            "kind": "factual_soft_evidence",
            "requested_course_match": False,
            "prior_year_course_match": False,
            "timeslot_preference": "neutral",
            "seniority": 0,
        }],
    }]
    assert review.data["review_summary"]["alternatives"][0]["key"] == (
        "teacher_assignment_candidate_ledger"
    )


@pytest.mark.django_db
def test_teacher_assignment_review_keeps_preassigned_teacher_as_fixed_context(
    authenticated_client, academic_year, course, counselor_user, teacher_user,
    run_scheduling_task_inline,
):
    """A review must not present an inherited teacher as a new recommendation."""

    qualification = Qualification.objects.create(
        code="teacher-ledger-fixed-senior",
        name="Teacher ledger fixed senior",
        kind="teachable",
        subject_code="mathematics",
        division=QUALIFICATION_DIVISION_SENIOR,
    )
    CourseQualificationRequirement.objects.create(course=course, qualification=qualification)
    TeacherQualification.objects.create(
        teacher=teacher_user.teacher_profile,
        qualification=qualification,
        review_status=QUALIFICATION_REVIEW_VERIFIED,
    )
    section, slot = _create_accepted_teacher_assignment_context(
        academic_year=academic_year,
        course=course,
        counselor_user=counselor_user,
        teacher=teacher_user.teacher_profile,
        is_fixed=True,
    )

    client = authenticated_client(counselor_user)
    created = client.post(
        "/api/planning/teacher-assignment-runs/",
        {"academic_year": academic_year.id},
        format="json",
    )

    assert created.status_code == 202, created.data
    execution = SchedulingExecution.objects.get(pk=created.data["id"])
    assert execution.status == "completed", execution.error_detail
    run_id = execution.result_id
    review = client.get(f"/api/planning/teacher-assignment-runs/{run_id}/review/")
    assert review.status_code == 200, review.data
    assert review.data["candidate_ledger"] == [{
        "decision_kind": "section",
        "section_ids": [section.id],
        "online_supervision_session_id": None,
        "shared_staffing_key": None,
        "semester": 1,
        "timeslot_id": slot.id,
        "selection_state": "fixed_context",
        "selected_teacher_id": teacher_user.teacher_profile.id,
        "candidates": [],
        "selection_factors": [{
            "kind": "fixed_context",
            "reason": "accepted_named_teacher_assignment",
        }],
    }]
