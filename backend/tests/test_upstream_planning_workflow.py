"""Counselor-facing offering, budget, roster, and physical-section workflow."""

import pytest

from backend.apps.common.constants import (
    BACKUP_POLICY_PROMOTE_AVAILABLE,
    COURSE_REQUEST_TYPE_ALTERNATE,
    COURSE_REQUEST_TYPE_PRIMARY,
    GRADE_LEVEL_10,
    QUALIFICATION_DIVISION_SENIOR,
    QUALIFICATION_REVIEW_PENDING,
    QUALIFICATION_REVIEW_VERIFIED,
    QUALIFICATION_SUBJECT_MATHEMATICS,
    TEACHER_ROSTER_STATUS_DRAFT,
    TEACHER_ROSTER_STATUS_READY,
)
from backend.apps.constraints.models import (
    CourseQualificationRequirement,
    Qualification,
    TeacherQualification,
)
from backend.apps.courses.models import (
    Course,
    CourseRequest,
    Section,
)
from backend.apps.courses.services.offerings import ensure_academic_year_offerings
from backend.apps.scheduling.models import (
    SectionBudgetApproval,
    StaffingPlanApproval,
    TeacherPlanningRoster,
)


@pytest.mark.django_db
def test_cancelled_course_backup_policy_is_audited_without_creating_sections(
    authenticated_client,
    academic_year,
    course,
    student_user,
    second_student_user,
    counselor_user,
):
    backup_course = Course.objects.create(
        name="Visual Arts",
        grade_level=GRADE_LEVEL_10,
        course_code="AVI2O",
        category="arts",
        capacity_min=10,
        capacity_max=35,
    )
    primary = CourseRequest.objects.create(
        student=student_user.student_profile,
        academic_year=academic_year,
        course=course,
        request_type=COURSE_REQUEST_TYPE_PRIMARY,
    )
    backup = CourseRequest.objects.create(
        student=student_user.student_profile,
        academic_year=academic_year,
        course=backup_course,
        request_type=COURSE_REQUEST_TYPE_ALTERNATE,
    )
    CourseRequest.objects.create(
        student=second_student_user.student_profile,
        academic_year=academic_year,
        course=backup_course,
        request_type=COURSE_REQUEST_TYPE_PRIMARY,
    )
    ensure_academic_year_offerings(academic_year, actor=counselor_user)
    cancelled = course.offerings.get(academic_year=academic_year)
    client = authenticated_client(counselor_user)

    response = client.post(
        f"/api/planning/course-offerings/{cancelled.id}/cancel/",
        {"reason": "Too few students to run this year."},
        format="json",
    )
    assert response.status_code == 200
    assert response.data["status"] == "cancelled"

    run_response = client.post(
        "/api/planning/section-budget-runs/",
        {
            "academic_year": academic_year.id,
            "budget_type": "ceiling",
            "section_budget": 1,
            "backup_policy": BACKUP_POLICY_PROMOTE_AVAILABLE,
        },
        format="json",
    )
    assert run_response.status_code == 201
    assert run_response.data["result"]["affected_student_count"] == 1
    resolution = run_response.data["result"]["request_resolutions"][0]
    assert resolution["outcome"] == "backup_promoted"
    assert resolution["backup_course_id"] == backup_course.id

    approval_response = client.post(
        f"/api/planning/section-budget-runs/{run_response.data['id']}/approve/",
        {"reason": "Approve the teacher-independent working budget."},
        format="json",
    )
    assert approval_response.status_code == 201
    assert SectionBudgetApproval.objects.count() == 1
    assert Section.objects.count() == 0
    # The immutable effective resolution never rewrites source requests.
    primary.refresh_from_db()
    backup.refresh_from_db()
    assert primary.request_type == COURSE_REQUEST_TYPE_PRIMARY
    assert backup.request_type == COURSE_REQUEST_TYPE_ALTERNATE


@pytest.mark.django_db
def test_combined_delivery_moves_from_ready_roster_to_one_physical_section(
    authenticated_client,
    academic_year,
    student_user,
    second_student_user,
    teacher_user,
    counselor_user,
):
    dance_11 = Course.objects.create(
        name="Dance 11",
        grade_level=GRADE_LEVEL_10,
        course_code="ATC3M",
        category="arts",
        capacity_min=10,
        capacity_max=35,
    )
    dance_12 = Course.objects.create(
        name="Dance 12",
        grade_level=GRADE_LEVEL_10,
        course_code="ATC4M",
        category="arts",
        capacity_min=10,
        capacity_max=35,
    )
    for student, requested_course in (
        (student_user.student_profile, dance_11),
        (second_student_user.student_profile, dance_12),
    ):
        CourseRequest.objects.create(
            student=student,
            academic_year=academic_year,
            course=requested_course,
            request_type=COURSE_REQUEST_TYPE_PRIMARY,
        )
    client = authenticated_client(counselor_user)
    rule_response = client.post(
        "/api/planning/combination-rules/",
        {
            "name": "Senior Dance combined class",
            "capacity_profile": dance_11.capacity_profile_id,
            "is_active": True,
            "course_ids": [dance_11.id, dance_12.id],
        },
        format="json",
    )
    assert rule_response.status_code == 201
    combine_response = client.post(
        "/api/planning/combine-offerings/",
        {
            "academic_year": academic_year.id,
            "rule_id": rule_response.data["id"],
            "reason": "The two groups fit safely in one shared class.",
        },
        format="json",
    )
    assert combine_response.status_code == 201
    group_id = combine_response.data["id"]
    assert combine_response.data["is_combined"] is True

    budget_run = client.post(
        "/api/planning/section-budget-runs/",
        {
            "academic_year": academic_year.id,
            "budget_type": "exact",
            "section_budget": 1,
            "backup_policy": "ignore",
        },
        format="json",
    )
    assert budget_run.status_code == 201
    budget_approval = client.post(
        f"/api/planning/section-budget-runs/{budget_run.data['id']}/approve/",
        {"reason": "Approve one physical combined class."},
        format="json",
    )
    assert budget_approval.status_code == 201

    for semester, maximum in ((1, 1), (2, 0)):
        assert client.post(
            "/api/planning/teacher-capacities/",
            {
                "teacher": teacher_user.teacher_profile.id,
                "academic_year": academic_year.id,
                "semester": semester,
                "maximum_sections": maximum,
                "reserved_sections": 0,
            },
            format="json",
        ).status_code == 201
    roster_response = client.post(
        "/api/planning/teacher-rosters/",
        {"academic_year": academic_year.id},
        format="json",
    )
    roster_id = roster_response.data["id"]
    assert client.post(
        f"/api/planning/teacher-rosters/{roster_id}/set-members/",
        {"teacher_ids": [teacher_user.teacher_profile.id]},
        format="json",
    ).status_code == 200
    ready = client.post(
        f"/api/planning/teacher-rosters/{roster_id}/confirm/",
        {},
        format="json",
    )
    assert ready.status_code == 200
    assert ready.data["status"] == TEACHER_ROSTER_STATUS_READY

    run = client.post(
        "/api/planning/staffing-runs/",
        {
            "academic_year": academic_year.id,
            "budget_approval": budget_approval.data["id"],
            "backup_policy": "ignore",
        },
        format="json",
    )
    assert run.status_code == 201
    assert run.data["status"] == "complete"
    assert run.data["result"]["planned_sections"] == 1
    assert run.data["result"]["linked_budget_total"] == 1
    assert run.data["result"]["offerings"][0]["offering_id"] == group_id

    approval = client.post(
        f"/api/planning/staffing-runs/{run.data['id']}/approve/",
        {"reason": "Roster and combined-class feasibility reviewed."},
        format="json",
    )
    assert approval.status_code == 201
    assert StaffingPlanApproval.objects.count() == 1
    section = Section.objects.get()
    assert section.delivery_group_id == group_id
    assert section.course_id is None
    assert section.teacher_id is None and section.is_locked is False
    assert section.staffing_approval_offering_id is not None
    # Once the shared physical row exists, neither separation nor a second
    # approval may silently rewrite that operational decision.
    assert client.post(
        f"/api/planning/delivery-groups/{group_id}/separate/",
        {"reason": "Try to separate too late."},
        format="json",
    ).status_code == 409
    assert client.post(
        f"/api/planning/staffing-runs/{run.data['id']}/approve/",
        {"reason": "Attempt duplicate approval."},
        format="json",
    ).status_code == 409


@pytest.mark.django_db
def test_qualification_review_and_teacher_changes_invalidate_ready_roster(
    authenticated_client,
    academic_year,
    course,
    teacher_user,
    counselor_user,
):
    qualification = Qualification.objects.create(
        code="mathematics-senior",
        name="Mathematics - Senior",
        subject_code=QUALIFICATION_SUBJECT_MATHEMATICS,
        division=QUALIFICATION_DIVISION_SENIOR,
    )
    CourseQualificationRequirement.objects.create(course=course, qualification=qualification)
    counselor = authenticated_client(counselor_user)
    for semester in (1, 2):
        counselor.post(
            "/api/planning/teacher-capacities/",
            {
                "teacher": teacher_user.teacher_profile.id,
                "academic_year": academic_year.id,
                "semester": semester,
                "maximum_sections": 1,
                "reserved_sections": 0,
            },
            format="json",
        )
    roster = counselor.post(
        "/api/planning/teacher-rosters/",
        {"academic_year": academic_year.id},
        format="json",
    )
    roster_id = roster.data["id"]
    counselor.post(
        f"/api/planning/teacher-rosters/{roster_id}/set-members/",
        {"teacher_ids": [teacher_user.teacher_profile.id]},
        format="json",
    )
    counselor.post(
        f"/api/planning/teacher-rosters/{roster_id}/confirm/",
        {},
        format="json",
    )
    assert TeacherPlanningRoster.objects.get(pk=roster_id).status == TEACHER_ROSTER_STATUS_READY

    submission = authenticated_client(teacher_user).post(
        f"/api/teachers/{teacher_user.teacher_profile.id}/qualifications/",
        {"qualification": qualification.id, "source_system": "manual"},
        format="json",
    )
    assert submission.status_code == 201
    assert submission.data["review_status"] == QUALIFICATION_REVIEW_PENDING
    assert TeacherPlanningRoster.objects.get(pk=roster_id).status == TEACHER_ROSTER_STATUS_DRAFT

    reviewed = counselor.post(
        (
            f"/api/teachers/{teacher_user.teacher_profile.id}/qualifications/"
            f"{submission.data['id']}/verify/"
        ),
        {"reason": "Credential checked against the official record."},
        format="json",
    )
    assert reviewed.status_code == 200
    assert reviewed.data["review_status"] == QUALIFICATION_REVIEW_VERIFIED
    assert TeacherQualification.objects.get(pk=submission.data["id"]).reviewed_by == counselor_user

    # A roster can be confirmed again after reviewing the changed evidence, and
    # archiving its teacher immediately invalidates that confirmation.
    assert counselor.post(
        f"/api/planning/teacher-rosters/{roster_id}/confirm/",
        {},
        format="json",
    ).data["status"] == TEACHER_ROSTER_STATUS_READY
    archived = counselor.post(
        f"/api/teachers/{teacher_user.teacher_profile.id}/archive/",
        {"reason": "Teacher is not returning next year."},
        format="json",
    )
    assert archived.status_code == 200
    assert archived.data["is_archived"] is True
    assert TeacherPlanningRoster.objects.get(pk=roster_id).status == TEACHER_ROSTER_STATUS_DRAFT
    assert counselor.delete(
        f"/api/teachers/{teacher_user.teacher_profile.id}/"
    ).status_code == 400
