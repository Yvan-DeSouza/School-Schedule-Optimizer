"""Regression contracts for special student-time commitments.

These tests deliberately cover Django workflow boundaries only.  Detailed
placement choice and occupancy semantics live in the pure-engine contracts,
where they can run without an ORM or database fixture.
"""

import pytest

from backend.apps.common.constants import (
    COURSE_CATEGORY_MATH,
    COURSE_REQUEST_TYPE_PRIMARY,
    GRADE_LEVEL_12,
)
from backend.apps.courses.models import (
    Course,
    CourseOffering,
    CourseRequest,
    StudentScheduleCommitmentRequest,
)
from backend.apps.courses.services.offerings import ensure_academic_year_offerings
from backend.apps.scheduling.models import (
    OnlineSupervisionConfiguration,
    OnlineSupervisionSession,
    TimeSlot,
)
from backend.apps.scheduling.services.online_supervision import (
    approve_online_supervision_plan_run,
    create_online_supervision_plan_run,
)


@pytest.mark.django_db
def test_online_and_co_op_remain_offerings_without_normal_delivery_groups(academic_year):
    """Special delivery does not create a misleading instructional resource."""

    online = Course.objects.create(
        name="Online Calculus",
        grade_level=GRADE_LEVEL_12,
        course_code="MHF4U-ONLINE",
        category=COURSE_CATEGORY_MATH,
        delivery_kind="online",
        capacity_min=10,
        capacity_max=30,
    )
    co_op = Course.objects.create(
        name="Co-op",
        grade_level=GRADE_LEVEL_12,
        course_code="COP4X",
        category="",
        delivery_kind="co_op",
        credit_value="2.0",
        capacity_min=10,
        capacity_max=30,
    )

    ensure_academic_year_offerings(academic_year)

    assert CourseOffering.objects.get(academic_year=academic_year, course=online).delivery_group is None
    assert CourseOffering.objects.get(academic_year=academic_year, course=co_op).delivery_group is None


@pytest.mark.django_db
def test_online_supervision_plan_is_reviewed_before_sessions_exist(
    academic_year,
    counselor_user,
    student_user,
):
    """Approval creates supervision capacity, not a fake academic Section."""

    online = Course.objects.create(
        name="Online Calculus",
        grade_level=GRADE_LEVEL_12,
        course_code="MHF4U-ONLINE",
        category=COURSE_CATEGORY_MATH,
        delivery_kind="online",
        capacity_min=10,
        capacity_max=30,
    )
    OnlineSupervisionConfiguration.objects.create(
        academic_year=academic_year,
        capacity_profile=online.capacity_profile,
        updated_by=counselor_user,
    )
    CourseRequest.objects.create(
        student=student_user.student_profile,
        academic_year=academic_year,
        course=online,
        request_type=COURSE_REQUEST_TYPE_PRIMARY,
    )

    run = create_online_supervision_plan_run(
        academic_year=academic_year,
        created_by=counselor_user,
    )

    assert run.result["sessions"]
    assert not OnlineSupervisionSession.objects.filter(academic_year=academic_year).exists()

    approve_online_supervision_plan_run(
        run,
        approved_by=counselor_user,
        reason="Reserve reviewed online supervision capacity.",
    )

    session = OnlineSupervisionSession.objects.get(academic_year=academic_year)
    assert session.timeslot is None
    assert session.supervisor is None


@pytest.mark.django_db
def test_counselor_can_create_and_release_an_append_only_study_lock(
    authenticated_client,
    academic_year,
    counselor_user,
    student_user,
):
    """A Study lock restricts requested time without creating an enrollment."""

    study_request = StudentScheduleCommitmentRequest.objects.create(
        student=student_user.student_profile,
        academic_year=academic_year,
        commitment_type="study",
        request_index=1,
    )
    timeslot = TimeSlot.objects.create(
        academic_year=academic_year,
        semester=1,
        block="A",
        is_available=True,
    )
    client = authenticated_client(counselor_user)
    create = client.post(
        "/api/planning/student-special-commitment-locks/",
        {
            "academic_year": academic_year.id,
            "lock_type": "study_time",
            "lock_mode": "exact",
            "schedule_commitment_request": study_request.id,
            "timeslot": timeslot.id,
            "reason": "Keep the approved Study period in A1.",
        },
        format="json",
    )

    assert create.status_code == 201
    lock_id = create.data["id"]
    released = client.post(
        f"/api/planning/student-special-commitment-locks/{lock_id}/release/",
        {"release_reason": "Counselor reopened the student's options."},
        format="json",
    )

    assert released.status_code == 200
    assert released.data["is_active"] is False
