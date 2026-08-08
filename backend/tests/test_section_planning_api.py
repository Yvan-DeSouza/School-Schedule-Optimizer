"""End-to-end API contract for immutable plans and draft-section approval.

Coverage includes authorization, review/preview shape, partial and adjusted
approvals, overwrite protection, audit provenance, immutability, and rollback.
"""

import pytest
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework.test import APIClient

from backend.apps.common.constants import (
    COURSE_ALLOWED_SEMESTER_1_ONLY,
    COURSE_ALLOWED_SEMESTER_EITHER,
    COURSE_REQUEST_TYPE_PRIMARY,
    GRADE_LEVEL_10,
    SECTION_PLANNING_RUN_STATUS_INFEASIBLE,
    SEMESTER_FALL,
    SEMESTER_WINTER,
)
from backend.apps.courses.models import Course, CourseRequest, Section
from backend.apps.scheduling.models import (
    SectionPlanningApproval,
    SectionPlanningApprovalCourse,
    SectionPlanningRun,
    TeacherPlanningCapacity,
)
from backend.apps.scheduling.services.section_planning import approve_section_planning_run


def create_approvable_run(client, academic_year, course, student_user, teacher_user):
    """Create minimal flexible-grade demand/staffing and return its saved run."""

    course.grade_level = GRADE_LEVEL_10
    course.save(update_fields=["grade_level"])
    CourseRequest.objects.create(
        student=student_user.student_profile,
        academic_year=academic_year,
        course=course,
        request_type=COURSE_REQUEST_TYPE_PRIMARY,
    )
    for semester in (SEMESTER_FALL, SEMESTER_WINTER):
        TeacherPlanningCapacity.objects.create(
            teacher=teacher_user.teacher_profile,
            academic_year=academic_year,
            semester=semester,
            maximum_sections=4,
        )
    response = client.post(
        "/api/planning/section-count-runs/",
        {"academic_year": academic_year.id},
        format="json",
    )
    assert response.status_code == 201
    return SectionPlanningRun.objects.get(pk=response.data["id"])


@pytest.mark.django_db
def test_section_planning_run_is_role_protected_immutable_and_read_only(
    authenticated_client, academic_year, course, student_user, teacher_user, counselor_user,
):
    # Planning-run creation remains recommendation-only: no Section rows appear.
    course.grade_level = GRADE_LEVEL_10
    course.save(update_fields=["grade_level"])
    CourseRequest.objects.create(
        student=student_user.student_profile,
        academic_year=academic_year,
        course=course,
        request_type=COURSE_REQUEST_TYPE_PRIMARY,
    )
    TeacherPlanningCapacity.objects.create(
        teacher=teacher_user.teacher_profile,
        academic_year=academic_year,
        semester=SEMESTER_FALL,
        maximum_sections=1,
    )
    url = "/api/planning/section-count-runs/"
    assert APIClient().post(url, {"academic_year": academic_year.id}, format="json").status_code == 401
    assert authenticated_client(teacher_user).post(url, {"academic_year": academic_year.id}, format="json").status_code == 403

    before_sections = Section.objects.count()
    response = authenticated_client(counselor_user).post(url, {"academic_year": academic_year.id}, format="json")
    assert response.status_code == 201
    assert response.data["status"] == "complete"
    assert response.data["result"]["courses"][0]["warnings"] == ["below_hard_min_review_required"]
    assert Section.objects.count() == before_sections

    run = SectionPlanningRun.objects.get(pk=response.data["id"])
    assert authenticated_client(counselor_user).get(f"{url}{run.id}/").status_code == 200
    with pytest.raises(Exception):
        run.save()


@pytest.mark.django_db
def test_planning_role_can_preview_and_approve_draft_sections_with_audit_trace(
    authenticated_client,
    academic_year,
    course,
    student_user,
    teacher_user,
    counselor_user,
    unknown_user,
):
    # Full happy path plus anonymous/teacher/unknown authorization boundaries.
    counselor_client = authenticated_client(counselor_user)
    run = create_approvable_run(
        counselor_client,
        academic_year,
        course,
        student_user,
        teacher_user,
    )
    review_url = f"/api/planning/section-count-runs/{run.id}/review/"
    preview_url = f"/api/planning/section-count-runs/{run.id}/approval-preview/"
    approve_url = f"/api/planning/section-count-runs/{run.id}/approve/"
    selection = {
        "courses": [{
            "course_id": course.id,
            "semester_1_count": 2,
            "semester_2_count": 0,
        }],
        "reason": "Department head confirmed two Semester 1 sections.",
    }

    assert APIClient().post(approve_url, selection, format="json").status_code == 401
    assert authenticated_client(teacher_user).post(approve_url, selection, format="json").status_code == 403
    assert authenticated_client(unknown_user).post(approve_url, selection, format="json").status_code == 403

    review = counselor_client.get(review_url)
    assert review.status_code == 200
    assert review.data["can_approve"] is True
    assert review.data["courses"][0]["recommended_annual_count"] == 1

    preview = counselor_client.post(preview_url, selection, format="json")
    assert preview.status_code == 200
    assert preview.data["can_approve"] is True
    assert preview.data["proposed_section_count"] == 2
    assert "counselor_adjusted_section_counts" in preview.data["courses"][0]["warnings"]

    response = counselor_client.post(approve_url, selection, format="json")
    assert response.status_code == 201
    assert response.data["planning_run"] == run.id
    assert response.data["approved_by"] == counselor_user.id
    assert response.data["reason"] == selection["reason"]
    assert response.data["course_approvals"][0]["approved_semester_1_count"] == 2

    sections = list(Section.objects.order_by("section_number"))
    assert set(response.data["course_approvals"][0]["generated_section_ids"]) == {
        section.id for section in sections
    }
    assert [section.section_number for section in sections] == ["S1-01", "S1-02"]
    assert all(section.semester == SEMESTER_FALL for section in sections)
    assert all(section.teacher is None and not section.is_locked for section in sections)
    assert all(section.capacity_min == course.capacity_profile.hard_min for section in sections)
    assert all(section.capacity_max == course.capacity_profile.hard_max for section in sections)
    assert all(section.planning_approval_course.approval.planning_run_id == run.id for section in sections)

    section_response = counselor_client.get(f"/api/sections/{sections[0].id}/")
    assert section_response.data["planning_approval"] == response.data["id"]
    assert section_response.data["planning_run"] == run.id
    run_response = counselor_client.get(f"/api/planning/section-count-runs/{run.id}/")
    assert run_response.data["approvals"][0]["id"] == response.data["id"]

    duplicate = counselor_client.post(approve_url, selection, format="json")
    assert duplicate.status_code == 409
    assert duplicate.data["conflicts"][0]["code"] == "course_already_approved_from_run"
    assert Section.objects.count() == 2


@pytest.mark.django_db
def test_partial_approvals_create_only_selected_courses(
    authenticated_client,
    academic_year,
    course,
    student_user,
    teacher_user,
    counselor_user,
):
    # A run may be reviewed in disjoint batches without touching other courses.
    client = authenticated_client(counselor_user)
    course.grade_level = GRADE_LEVEL_10
    course.save(update_fields=["grade_level"])
    second_course = Course.objects.create(
        name="Data Management",
        grade_level=GRADE_LEVEL_10,
        course_code="MDM4U",
        category=course.category,
        capacity_min=10,
        capacity_max=30,
    )
    for requested_course in (course, second_course):
        CourseRequest.objects.create(
            student=student_user.student_profile,
            academic_year=academic_year,
            course=requested_course,
            request_type=COURSE_REQUEST_TYPE_PRIMARY,
        )
    for semester in (SEMESTER_FALL, SEMESTER_WINTER):
        TeacherPlanningCapacity.objects.create(
            teacher=teacher_user.teacher_profile,
            academic_year=academic_year,
            semester=semester,
            maximum_sections=4,
        )
    run_response = client.post(
        "/api/planning/section-count-runs/",
        {"academic_year": academic_year.id},
        format="json",
    )
    run_id = run_response.data["id"]
    approve_url = f"/api/planning/section-count-runs/{run_id}/approve/"

    first = client.post(approve_url, {
        "courses": [{"course_id": course.id, "semester_1_count": 1, "semester_2_count": 0}],
    }, format="json")
    assert first.status_code == 201
    assert set(Section.objects.values_list("course_id", flat=True)) == {course.id}

    second = client.post(approve_url, {
        "courses": [{"course_id": second_course.id, "semester_1_count": 0, "semester_2_count": 1}],
    }, format="json")
    assert second.status_code == 201
    assert set(Section.objects.values_list("course_id", flat=True)) == {course.id, second_course.id}
    assert SectionPlanningApproval.objects.filter(planning_run_id=run_id).count() == 2


@pytest.mark.django_db
def test_approval_refuses_existing_sections_and_invalid_current_semester(
    authenticated_client,
    academic_year,
    course,
    student_user,
    teacher_user,
    counselor_user,
):
    # Current catalog legality wins over a stale run, and existing operational
    # sections require explicit reconciliation rather than replacement.
    client = authenticated_client(counselor_user)
    run = create_approvable_run(client, academic_year, course, student_user, teacher_user)
    approve_url = f"/api/planning/section-count-runs/{run.id}/approve/"
    selection = {
        "courses": [{"course_id": course.id, "semester_1_count": 0, "semester_2_count": 1}],
    }

    course.allowed_semester = COURSE_ALLOWED_SEMESTER_1_ONLY
    course.save(update_fields=["allowed_semester"])
    invalid_semester = client.post(approve_url, selection, format="json")
    assert invalid_semester.status_code == 400
    assert invalid_semester.data["validation_errors"][0]["code"] == "course_not_allowed_in_semester_2"
    assert SectionPlanningApproval.objects.count() == 0

    course.allowed_semester = COURSE_ALLOWED_SEMESTER_EITHER
    course.save(update_fields=["allowed_semester"])
    Section.objects.create(
        course=course,
        section_number="01",
        academic_year=academic_year,
        semester=SEMESTER_FALL,
        capacity_min=10,
        capacity_max=30,
    )
    existing = client.post(approve_url, selection, format="json")
    assert existing.status_code == 409
    assert existing.data["conflicts"][0]["code"] == "existing_sections_for_course_year"
    assert SectionPlanningApproval.objects.count() == 0


@pytest.mark.django_db
def test_infeasible_run_cannot_be_reviewed_or_approved(
    authenticated_client,
    academic_year,
    counselor_user,
):
    run = SectionPlanningRun.objects.create(
        academic_year=academic_year,
        created_by=counselor_user,
        status=SECTION_PLANNING_RUN_STATUS_INFEASIBLE,
        result={"status": "infeasible", "detail": "No feasible plan."},
        input_snapshot={},
    )
    client = authenticated_client(counselor_user)

    assert client.get(f"/api/planning/section-count-runs/{run.id}/review/").status_code == 400
    assert client.post(f"/api/planning/section-count-runs/{run.id}/approve/", {}, format="json").status_code == 400
    assert SectionPlanningApproval.objects.count() == 0


@pytest.mark.django_db
def test_approval_transaction_rolls_back_if_section_creation_fails(
    monkeypatch,
    authenticated_client,
    academic_year,
    course,
    student_user,
    teacher_user,
    counselor_user,
):
    # Simulate failure after one Section insert; atomicity must remove both the
    # first draft and all approval audit rows.
    client = authenticated_client(counselor_user)
    run = create_approvable_run(client, academic_year, course, student_user, teacher_user)
    original_create = Section.objects.create
    calls = 0

    def fail_on_second_section(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated section write failure")
        return original_create(**kwargs)

    monkeypatch.setattr(Section.objects, "create", fail_on_second_section)
    with pytest.raises(RuntimeError, match="simulated section write failure"):
        approve_section_planning_run(
            run,
            approved_by=counselor_user,
            selections=[{
                "course_id": course.id,
                "semester_1_count": 2,
                "semester_2_count": 0,
            }],
        )

    assert Section.objects.count() == 0
    assert SectionPlanningApproval.objects.count() == 0
    assert SectionPlanningApprovalCourse.objects.count() == 0


@pytest.mark.django_db
def test_approval_records_are_immutable(
    authenticated_client,
    academic_year,
    course,
    student_user,
    teacher_user,
    counselor_user,
):
    client = authenticated_client(counselor_user)
    run = create_approvable_run(client, academic_year, course, student_user, teacher_user)
    response = client.post(
        f"/api/planning/section-count-runs/{run.id}/approve/",
        {},
        format="json",
    )
    assert response.status_code == 201
    approval = SectionPlanningApproval.objects.get(pk=response.data["id"])
    approved_course = approval.course_approvals.get()

    with pytest.raises(DjangoValidationError):
        approval.save()
    with pytest.raises(DjangoValidationError):
        approved_course.save()
