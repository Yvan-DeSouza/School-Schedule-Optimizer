"""Transactional contracts for the first student-to-section assignment stage."""

import pytest

from backend.apps.common.constants import (
    COURSE_REQUEST_TYPE_PRIMARY,
    SCHEDULE_BLOCK_A,
)
from backend.apps.courses.constants import ENROLLMENT_LIFECYCLE_ACTIVE, ENROLLMENT_LIFECYCLE_HISTORICAL
from backend.apps.courses.models import CourseRequest, Enrollment, Section
from backend.apps.courses.services.offerings import ensure_academic_year_offerings
from backend.apps.scheduling.models import (
    SectionSchedule,
    StudentAssignmentApproval,
    StudentAssignmentApprovalEnrollment,
    StudentAssignmentLock,
    TimeSlot,
)
from backend.apps.scheduling.services.student_assignment import (
    StudentAssignmentValidationError,
    approve_student_assignment_run,
    create_student_assignment_run,
)
from backend.apps.scheduling.services.student_assignment_locks import create_student_assignment_lock


@pytest.mark.django_db
def test_sections_only_run_creates_only_new_enrollment_and_provenance(
    academic_year, course, counselor_user, student_user,
):
    """Mode A ignores staffing yet approval writes an auditable exact offering."""

    offering = ensure_academic_year_offerings(academic_year, actor=counselor_user)[0]
    section = Section.objects.create(
        course=course,
        delivery_group=offering.delivery_group,
        section_number="S1-01",
        academic_year=academic_year,
        semester=1,
        capacity_min=10,
        capacity_max=30,
    )
    slot = TimeSlot.objects.create(
        academic_year=academic_year,
        semester=1,
        block=SCHEDULE_BLOCK_A,
    )
    SectionSchedule.objects.create(section=section, timeslot=slot, room=None)
    request = CourseRequest.objects.create(
        student=student_user.student_profile,
        academic_year=academic_year,
        course=course,
        is_mandatory=True,
        request_type=COURSE_REQUEST_TYPE_PRIMARY,
    )

    run = create_student_assignment_run(
        academic_year=academic_year.id,
        staffing_mode="sections_only",
        provisional_teacher_assignment_run=None,
        soft_constraint_importance={
            "section_utilization_balance": "important",
            "student_semester_balance": "important",
            "course_sequence_preferences": "important",
            "difficulty_balance": "not_important",
            "course_category_diversity": "not_important",
        },
        created_by=counselor_user,
    )

    assert run.status == "complete"
    approval = approve_student_assignment_run(
        run,
        approved_by=counselor_user,
        reason="Approve a reviewed student recommendation.",
    )
    enrollment = Enrollment.objects.get(student=student_user.student_profile)
    provenance = StudentAssignmentApprovalEnrollment.objects.get(approval=approval)
    assert enrollment.section_id == section.id
    assert enrollment.course_offering_id == offering.id
    assert provenance.course_request_id == request.id
    assert provenance.assignment_basis == "primary_request"


@pytest.mark.django_db
def test_sections_only_snapshot_does_not_stale_when_teacher_changes(
    academic_year, course, counselor_user, student_user, teacher_user,
):
    """Mode A transparently excludes teacher identity from its fingerprint."""

    offering = ensure_academic_year_offerings(academic_year, actor=counselor_user)[0]
    section = Section.objects.create(
        course=course, delivery_group=offering.delivery_group,
        section_number="S1-02", academic_year=academic_year, semester=1,
        capacity_min=10, capacity_max=30,
    )
    slot = TimeSlot.objects.create(academic_year=academic_year, semester=1, block=SCHEDULE_BLOCK_A)
    SectionSchedule.objects.create(section=section, timeslot=slot, room=None)
    CourseRequest.objects.create(
        student=student_user.student_profile, academic_year=academic_year,
        course=course, request_type=COURSE_REQUEST_TYPE_PRIMARY,
    )
    run = create_student_assignment_run(
        academic_year=academic_year.id, staffing_mode="sections_only",
        provisional_teacher_assignment_run=None,
        soft_constraint_importance={
            "section_utilization_balance": "not_important",
            "student_semester_balance": "not_important",
            "course_sequence_preferences": "not_important",
            "difficulty_balance": "not_important",
            "course_category_diversity": "not_important",
        }, created_by=counselor_user,
    )
    section.teacher = teacher_user.teacher_profile
    section.save(update_fields=["teacher"])

    approval = approve_student_assignment_run(run, approved_by=counselor_user, reason="Teacher is intentionally ignored in Mode A.")

    assert approval.student_assignment_run_id == run.id


@pytest.mark.django_db
def test_exact_locked_target_retires_prior_active_enrollment_with_provenance(
    academic_year, course, counselor_user, student_user,
):
    """A reviewed move preserves the old row instead of rewriting or deleting it."""

    offering = ensure_academic_year_offerings(academic_year, actor=counselor_user)[0]
    first_section = Section.objects.create(
        course=course, delivery_group=offering.delivery_group,
        section_number="S1-03", academic_year=academic_year, semester=1,
        capacity_min=10, capacity_max=30,
    )
    second_section = Section.objects.create(
        course=course, delivery_group=offering.delivery_group,
        section_number="S1-04", academic_year=academic_year, semester=1,
        capacity_min=10, capacity_max=30,
    )
    first_slot = TimeSlot.objects.create(academic_year=academic_year, semester=1, block=SCHEDULE_BLOCK_A)
    second_slot = TimeSlot.objects.create(academic_year=academic_year, semester=1, block="B")
    SectionSchedule.objects.create(section=first_section, timeslot=first_slot, room=None)
    SectionSchedule.objects.create(section=second_section, timeslot=second_slot, room=None)
    CourseRequest.objects.create(
        student=student_user.student_profile,
        academic_year=academic_year,
        course=course,
        request_type=COURSE_REQUEST_TYPE_PRIMARY,
        is_mandatory=True,
    )

    first_run = create_student_assignment_run(
        academic_year=academic_year.id,
        staffing_mode="sections_only",
        provisional_teacher_assignment_run=None,
        soft_constraint_importance={
            "section_utilization_balance": "not_important",
            "student_semester_balance": "not_important",
            "course_sequence_preferences": "not_important",
            "difficulty_balance": "not_important",
            "course_category_diversity": "not_important",
        },
        created_by=counselor_user,
    )
    approve_student_assignment_run(
        first_run,
        approved_by=counselor_user,
        reason="Approve the initial student placement.",
    )
    previous = Enrollment.objects.get(student=student_user.student_profile)

    lock = create_student_assignment_lock(
        academic_year=academic_year,
        lock_type="exact_student_section",
        created_by=counselor_user,
        reason="Keep this student's reviewed replacement in the selected section.",
        student=student_user.student_profile,
        section=second_section,
        course=course,
        staffing_mode="sections_only",
    )
    assert StudentAssignmentLock.objects.get(pk=lock.pk).is_active

    rerun = create_student_assignment_run(
        academic_year=academic_year.id,
        staffing_mode="sections_only",
        provisional_teacher_assignment_run=None,
        soft_constraint_importance={
            "section_utilization_balance": "not_important",
            "student_semester_balance": "not_important",
            "course_sequence_preferences": "not_important",
            "difficulty_balance": "not_important",
            "course_category_diversity": "not_important",
        },
        created_by=counselor_user,
    )
    approval = approve_student_assignment_run(
        rerun,
        approved_by=counselor_user,
        reason="Approve the locked student reassignment.",
    )

    previous.refresh_from_db()
    current = Enrollment.objects.get(
        student=student_user.student_profile,
        lifecycle_status=ENROLLMENT_LIFECYCLE_ACTIVE,
    )
    provenance = StudentAssignmentApprovalEnrollment.objects.get(
        approval=approval,
        enrollment=current,
    )
    assert previous.lifecycle_status == ENROLLMENT_LIFECYCLE_HISTORICAL
    assert current.section_id == second_section.id
    assert provenance.superseded_enrollment_id == previous.id
    assert StudentAssignmentApproval.objects.filter(pk=approval.pk).exists()


@pytest.mark.django_db
def test_scoped_run_without_an_accepted_source_fails_closed():
    with pytest.raises(StudentAssignmentValidationError) as error:
        create_student_assignment_run(
            academic_year=1,
            staffing_mode="sections_only",
            provisional_teacher_assignment_run=None,
            soft_constraint_importance={
                "section_utilization_balance": "not_important",
                "student_semester_balance": "not_important",
                "course_sequence_preferences": "not_important",
                "difficulty_balance": "not_important",
                "course_category_diversity": "not_important",
            },
            created_by=None,
            scope_type="scoped",
            scope_student_ids=(1,),
        )

    assert error.value.detail["code"] == "student_assignment_rerun_scope_invalid"
