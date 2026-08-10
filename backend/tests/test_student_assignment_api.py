"""HTTP contract and configuration coverage for student assignment."""

import pytest

from backend.apps.common.constants import (
    COURSE_CATEGORY_MATH,
    COURSE_REQUEST_TYPE_PRIMARY,
    GRADE_LEVEL_12,
    SCHEDULE_BLOCK_A,
)
from backend.apps.courses.models import Course, CourseRequest, Section
from backend.apps.courses.services.offerings import ensure_academic_year_offerings
from backend.apps.scheduling.models import SectionSchedule, StudentAssignmentLock, TimeSlot


@pytest.mark.django_db
def test_student_roster_omits_contact_and_birth_date_fields(
    authenticated_client, counselor_user, student_user,
):
    response = authenticated_client(counselor_user).get("/api/students/")

    assert response.status_code == 200
    row = response.data["results"][0]
    assert set(row) == {
        "id", "student_number", "first_name", "last_name", "grade_level", "academic_year",
    }
    assert "email" not in row
    assert "date_of_birth" not in row
    assert authenticated_client(student_user).get("/api/students/").status_code == 403


@pytest.mark.django_db
def test_prerequisite_and_soft_sequence_configuration_reject_cycles(
    authenticated_client, counselor_user, course,
):
    second = Course.objects.create(
        name="Linear Algebra", grade_level=GRADE_LEVEL_12,
        course_code="MHF4U", category=COURSE_CATEGORY_MATH,
        capacity_min=10, capacity_max=30,
    )
    client = authenticated_client(counselor_user)

    assert client.post("/api/course-prerequisites/", {
        "course": second.id, "prerequisite": course.id,
    }, format="json").status_code == 201
    assert client.post("/api/course-prerequisites/", {
        "course": course.id, "prerequisite": second.id,
    }, format="json").status_code == 400
    assert client.post("/api/course-sequence-preferences/", {
        "earlier_course": course.id, "later_course": second.id, "is_active": True,
    }, format="json").status_code == 201
    assert client.post("/api/course-sequence-preferences/", {
        "earlier_course": second.id, "later_course": course.id, "is_active": True,
    }, format="json").status_code == 400


@pytest.mark.django_db
def test_counselor_can_create_and_review_sections_only_student_run(
    authenticated_client, academic_year, course, counselor_user, student_user, staff_user,
):
    offering = ensure_academic_year_offerings(academic_year, actor=counselor_user)[0]
    section = Section.objects.create(
        course=course, delivery_group=offering.delivery_group,
        section_number="S1-01", academic_year=academic_year, semester=1,
        capacity_min=10, capacity_max=30,
    )
    slot = TimeSlot.objects.create(academic_year=academic_year, semester=1, block=SCHEDULE_BLOCK_A)
    SectionSchedule.objects.create(section=section, timeslot=slot)
    CourseRequest.objects.create(
        student=student_user.student_profile, academic_year=academic_year,
        course=course, request_type=COURSE_REQUEST_TYPE_PRIMARY,
    )
    payload = {
        "academic_year": academic_year.id,
        "staffing_mode": "sections_only",
        "soft_constraint_importance": {
            "section_utilization_balance": "important",
            "student_semester_balance": "important",
            "course_sequence_preferences": "really_important",
            "difficulty_balance": "important",
            "course_category_diversity": "important",
        },
    }

    client = authenticated_client(counselor_user)
    response = client.post("/api/planning/student-assignment-runs/", payload, format="json")

    assert response.status_code == 201
    assert response.data["status"] == "complete"
    review = client.get(f"/api/planning/student-assignment-runs/{response.data['id']}/review/")
    assert review.status_code == 200
    assert review.data["approval_allowed"] is True
    assert "lock_costs" in review.data
    assert "seat_contention" in review.data
    assert "section_balance_facts" in review.data
    assert "soft_priorities" in review.data
    run_url = f"/api/planning/student-assignment-runs/{response.data['id']}"
    assert authenticated_client(staff_user).post(
        "/api/planning/student-assignment-runs/", payload, format="json",
    ).status_code == 403
    assert authenticated_client(staff_user).get(f"{run_url}/review/").status_code == 200
    assert authenticated_client(student_user).get(f"{run_url}/review/").status_code == 403
    assert authenticated_client(staff_user).post(
        f"{run_url}/approve/", {"reason": "Staff cannot approve."}, format="json",
    ).status_code == 403
    assert authenticated_client(student_user).post(
        f"{run_url}/what-if-unlock/", {"lock_ids": [999999]}, format="json",
    ).status_code == 403
    what_if = client.post(
        f"/api/planning/student-assignment-runs/{response.data['id']}/what-if-unlock/",
        {"lock_ids": [999999]}, format="json",
    )
    assert what_if.status_code == 400
    assert what_if.data["code"] == "student_assignment_what_if_lock_not_active"
    explanation = client.get(
        f"/api/planning/student-assignment-runs/{response.data['id']}/students/"
        f"{student_user.student_profile.id}/explanation/"
    )
    assert explanation.status_code == 200
    assert explanation.data["requests"][0]["received"] is True


@pytest.mark.django_db
def test_lock_endpoints_cover_all_types_release_audit_and_role_boundaries(
    authenticated_client, academic_year, course, counselor_user, staff_user,
    student_user, second_student_user, teacher_user,
):
    section = Section.objects.create(
        course=course, section_number="LOCK-01", academic_year=academic_year,
        semester=1, capacity_min=10, capacity_max=30,
    )
    client = authenticated_client(counselor_user)
    payloads = [
        {
            "lock_type": "exact_student_section",
            "student": student_user.student_profile.id,
            "section": section.id,
            "course": course.id,
        },
        {"lock_type": "whole_student_schedule", "student": student_user.student_profile.id},
        {"lock_type": "section_roster", "section": section.id},
        {"lock_type": "course_roster", "course": course.id},
        {
            "lock_type": "student_group_same_section", "course": course.id,
            "group_student_ids": [student_user.student_profile.id, second_student_user.student_profile.id],
        },
        {
            "lock_type": "student_teacher_course", "student": student_user.student_profile.id,
            "course": course.id, "teacher": teacher_user.teacher_profile.id,
            "staffing_mode": "final_staffing",
        },
    ]
    created = []
    for index, lock_payload in enumerate(payloads):
        response = client.post(
            "/api/planning/student-assignment-locks/",
            {"academic_year": academic_year.id, "reason": f"Reviewed lock {index}." , **lock_payload},
            format="json",
        )
        assert response.status_code == 201, response.data
        created.append(response.data)

    listed = client.get(
        f"/api/planning/student-assignment-locks/?academic_year={academic_year.id}"
    )
    assert listed.status_code == 200
    assert {row["lock_type"] for row in listed.data} == {
        payload["lock_type"] for payload in payloads
    }
    assert authenticated_client(staff_user).get(
        f"/api/planning/student-assignment-locks/?academic_year={academic_year.id}"
    ).status_code == 200
    assert authenticated_client(student_user).get(
        f"/api/planning/student-assignment-locks/?academic_year={academic_year.id}"
    ).status_code == 403
    assert authenticated_client(staff_user).post(
        "/api/planning/student-assignment-locks/", {
            "academic_year": academic_year.id,
            "lock_type": "whole_student_schedule",
            "student": student_user.student_profile.id,
            "reason": "Staff cannot create counselor-owned locks.",
        }, format="json",
    ).status_code == 403

    for row in created:
        assert authenticated_client(staff_user).post(
            f"/api/planning/student-assignment-locks/{row['id']}/release/",
            {"release_reason": "Staff cannot release counselor-owned locks."},
            format="json",
        ).status_code == 403
        released = client.post(
            f"/api/planning/student-assignment-locks/{row['id']}/release/",
            {"release_reason": "The reviewed lock is no longer needed."},
            format="json",
        )
        assert released.status_code == 200, released.data
        assert released.data["is_active"] is False
    assert StudentAssignmentLock.objects.filter(
        academic_year=academic_year, is_active=True,
    ).count() == 0


@pytest.mark.django_db
def test_teacher_lock_endpoint_requires_final_staffing_and_what_if_uses_stable_code(
    authenticated_client, academic_year, course, counselor_user, student_user, teacher_user,
):
    client = authenticated_client(counselor_user)
    response = client.post(
        "/api/planning/student-assignment-locks/", {
            "academic_year": academic_year.id,
            "lock_type": "student_teacher_course",
            "student": student_user.student_profile.id,
            "course": course.id,
            "teacher": teacher_user.teacher_profile.id,
            "staffing_mode": "partial_staffing",
            "reason": "This must be rejected before final staffing.",
        }, format="json",
    )
    assert response.status_code == 400
    assert response.data["code"] == "student_assignment_lock_final_staffing_required"


@pytest.mark.django_db
def test_run_creation_validates_scoped_source_and_priority_cap(
    authenticated_client, academic_year, counselor_user,
):
    client = authenticated_client(counselor_user)
    base = {
        "academic_year": academic_year.id,
        "staffing_mode": "sections_only",
        "soft_constraint_importance": {
            "section_utilization_balance": "not_important",
            "student_semester_balance": "not_important",
            "course_sequence_preferences": "not_important",
            "difficulty_balance": "not_important",
            "course_category_diversity": "not_important",
        },
    }
    scoped = client.post(
        "/api/planning/student-assignment-runs/",
        {**base, "scope_type": "scoped", "scope_student_ids": [1]}, format="json",
    )
    assert scoped.status_code == 400
    assert scoped.data["code"] == "student_assignment_rerun_scope_invalid"

    capped = client.post(
        "/api/planning/student-assignment-runs/",
        {**base, "priority_request_ids": list(range(1, 102))}, format="json",
    )
    assert capped.status_code == 400
    assert "priority_request_ids" in capped.data
