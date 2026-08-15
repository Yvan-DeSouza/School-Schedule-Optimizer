"""Immediate occupancy validation for immutable special-commitment locks."""

import pytest

from backend.apps.common.constants import COURSE_REQUEST_TYPE_PRIMARY, GRADE_LEVEL_12
from backend.apps.common.exceptions import DomainConflictError
from backend.apps.courses.constants import (
    COURSE_ALLOWED_SEMESTER_1_ONLY,
    COURSE_ALLOWED_SEMESTER_2_ONLY,
    COURSE_DELIVERY_KIND_CO_OP,
)
from backend.apps.courses.models import Course, CourseRequest, StudentScheduleCommitmentRequest
from backend.apps.scheduling.codes import STUDENT_SPECIAL_COMMITMENT_LOCK_INVALID_TARGET
from backend.apps.scheduling.services.student_special_commitment_locks import (
    create_student_special_commitment_lock,
)


def _mandatory_course_requests(*, academic_year, student, semester, count):
    """Create full-semester local demand whose term is fixed by catalog policy."""

    allowed_semester = (
        COURSE_ALLOWED_SEMESTER_1_ONLY
        if semester == 1
        else COURSE_ALLOWED_SEMESTER_2_ONLY
    )
    rows = []
    for index in range(count):
        course = Course.objects.create(
            name=f"Required {semester}-{index}",
            course_code=f"LCK{semester}{index:02d}",
            grade_level=GRADE_LEVEL_12,
            category="math",
            allowed_semester=allowed_semester,
            capacity_min=1,
            capacity_max=30,
        )
        rows.append(CourseRequest.objects.create(
            student=student,
            academic_year=academic_year,
            course=course,
            request_type=COURSE_REQUEST_TYPE_PRIMARY,
            is_mandatory=True,
        ))
    return rows


def _co_op_request(*, academic_year, student):
    course = Course.objects.create(
        name="Co-op",
        course_code="LCK-COOP",
        grade_level=GRADE_LEVEL_12,
        category="",
        delivery_kind=COURSE_DELIVERY_KIND_CO_OP,
        credit_value="2.0",
        capacity_min=1,
        capacity_max=30,
    )
    return CourseRequest.objects.create(
        student=student,
        academic_year=academic_year,
        course=course,
        request_type=COURSE_REQUEST_TYPE_PRIMARY,
        is_mandatory=True,
    )


@pytest.mark.django_db
def test_focus_lock_rejects_a_semester_filled_by_mandatory_local_courses(
    academic_year, counselor_user, student_user,
):
    _mandatory_course_requests(
        academic_year=academic_year,
        student=student_user.student_profile,
        semester=1,
        count=4,
    )
    focus_request = StudentScheduleCommitmentRequest.objects.create(
        student=student_user.student_profile,
        academic_year=academic_year,
        commitment_type="focus",
        request_index=1,
    )

    with pytest.raises(DomainConflictError) as error:
        create_student_special_commitment_lock(
            academic_year=academic_year,
            created_by=counselor_user,
            lock_type="focus_semester",
            lock_mode="exact",
            schedule_commitment_request=focus_request,
            semester=1,
            reason="External program timing is confirmed.",
        )

    assert error.value.detail["code"] == STUDENT_SPECIAL_COMMITMENT_LOCK_INVALID_TARGET


@pytest.mark.django_db
def test_co_op_lock_rejects_a_block_pair_in_a_fully_occupied_semester(
    academic_year, counselor_user, student_user,
):
    _mandatory_course_requests(
        academic_year=academic_year,
        student=student_user.student_profile,
        semester=1,
        count=4,
    )
    request = _co_op_request(academic_year=academic_year, student=student_user.student_profile)

    with pytest.raises(DomainConflictError) as error:
        create_student_special_commitment_lock(
            academic_year=academic_year,
            created_by=counselor_user,
            lock_type="co_op_time",
            lock_mode="exact",
            course_request=request,
            semester=1,
            co_op_block_pair="a_b",
            reason="Employer placement timing is confirmed.",
        )

    assert error.value.detail["code"] == STUDENT_SPECIAL_COMMITMENT_LOCK_INVALID_TARGET


@pytest.mark.django_db
def test_focus_lock_accepts_a_semester_without_forced_local_occupancy(
    academic_year, counselor_user, student_user,
):
    _mandatory_course_requests(
        academic_year=academic_year,
        student=student_user.student_profile,
        semester=2,
        count=4,
    )
    focus_request = StudentScheduleCommitmentRequest.objects.create(
        student=student_user.student_profile,
        academic_year=academic_year,
        commitment_type="focus",
        request_index=1,
    )

    lock = create_student_special_commitment_lock(
        academic_year=academic_year,
        created_by=counselor_user,
        lock_type="focus_semester",
        lock_mode="exact",
        schedule_commitment_request=focus_request,
        semester=1,
        reason="External program timing is confirmed.",
    )

    assert lock.is_active


@pytest.mark.django_db
def test_co_op_lock_accepts_a_pair_when_local_term_has_remaining_blocks(
    academic_year, counselor_user, student_user,
):
    _mandatory_course_requests(
        academic_year=academic_year,
        student=student_user.student_profile,
        semester=1,
        count=2,
    )
    request = _co_op_request(academic_year=academic_year, student=student_user.student_profile)

    lock = create_student_special_commitment_lock(
        academic_year=academic_year,
        created_by=counselor_user,
        lock_type="co_op_time",
        lock_mode="exact",
        course_request=request,
        semester=1,
        co_op_block_pair="a_b",
        reason="Employer placement timing is confirmed.",
    )

    assert lock.is_active
