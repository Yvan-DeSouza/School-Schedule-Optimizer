"""Step 6 safety, drift, rollback, and review-contract regressions."""

import pytest

from backend.apps.common.constants import COURSE_REQUEST_TYPE_PRIMARY, SCHEDULE_BLOCK_A
from backend.apps.courses.constants import ENROLLMENT_LIFECYCLE_HISTORICAL
from backend.apps.courses.models import CourseRequest, Enrollment, Section
from backend.apps.courses.services.offerings import ensure_academic_year_offerings
from backend.apps.scheduling.codes import STUDENT_ASSIGNMENT_RERUN_CONTEXT_CHANGED
from backend.apps.scheduling.models import (
    SectionSchedule,
    StudentAssignmentApproval,
    StudentAssignmentApprovalEnrollment,
    StudentAssignmentLock,
    StudentAssignmentRun,
    TimeSlot,
)
from backend.apps.scheduling.services.student_assignment import (
    StudentAssignmentConflictError,
    approve_student_assignment_run,
    create_student_assignment_run,
    preview_student_assignment_approval,
    preview_student_assignment_unlock,
)
from backend.apps.scheduling.services.student_assignment_locks import (
    create_student_assignment_lock,
    release_student_assignment_lock,
)


def _importance():
    return {
        "section_utilization_balance": "not_important",
        "student_semester_balance": "not_important",
        "course_sequence_preferences": "not_important",
        "difficulty_balance": "not_important",
        "course_category_diversity": "not_important",
    }


def _setup_assignment_context(academic_year, course, counselor_user, students, *, section_count=2):
    offering = ensure_academic_year_offerings(academic_year, actor=counselor_user)[0]
    sections = []
    for index in range(section_count):
        section = Section.objects.create(
            course=course,
            delivery_group=offering.delivery_group,
            section_number=f"HARD-{index + 1:02d}",
            academic_year=academic_year,
            semester=1,
            capacity_min=1,
            capacity_max=1,
        )
        slot = TimeSlot.objects.create(
            academic_year=academic_year,
            semester=1,
            block=chr(ord(SCHEDULE_BLOCK_A) + index),
        )
        SectionSchedule.objects.create(section=section, timeslot=slot, room=None)
        sections.append(section)
    requests = [
        CourseRequest.objects.create(
            student=student.student_profile,
            academic_year=academic_year,
            course=course,
            request_type=COURSE_REQUEST_TYPE_PRIMARY,
            is_mandatory=True,
        )
        for student in students
    ]
    return offering, sections, requests


def _run(academic_year, counselor_user, *, staffing_mode="sections_only"):
    return create_student_assignment_run(
        academic_year=academic_year.id,
        staffing_mode=staffing_mode,
        provisional_teacher_assignment_run=None,
        soft_constraint_importance=_importance(),
        created_by=counselor_user,
    )


@pytest.mark.django_db
def test_approval_rolls_back_every_write_when_a_later_enrollment_fails(
    monkeypatch, academic_year, course, counselor_user, student_user, second_student_user,
):
    _setup_assignment_context(
        academic_year, course, counselor_user, [student_user, second_student_user],
    )
    run = _run(academic_year, counselor_user)
    assert len(run.result["assignments"]) == 2
    original_save = Enrollment.save
    saves = 0

    def fail_on_second_new_enrollment(self, *args, **kwargs):
        nonlocal saves
        if self._state.adding and self.course_offering_id:
            saves += 1
            if saves == 2:
                raise RuntimeError("simulated enrollment write failure")
        return original_save(self, *args, **kwargs)

    monkeypatch.setattr(Enrollment, "save", fail_on_second_new_enrollment)
    with pytest.raises(RuntimeError, match="simulated enrollment write failure"):
        approve_student_assignment_run(
            run, approved_by=counselor_user, reason="Exercise approval rollback.",
        )

    assert Enrollment.objects.count() == 0
    assert StudentAssignmentApproval.objects.count() == 0
    assert StudentAssignmentApprovalEnrollment.objects.count() == 0


@pytest.mark.django_db
def test_releasing_a_solve_time_lock_blocks_stale_approval(
    academic_year, course, counselor_user, student_user,
):
    _, sections, _ = _setup_assignment_context(
        academic_year, course, counselor_user, [student_user], section_count=1,
    )
    lock = create_student_assignment_lock(
        academic_year=academic_year,
        lock_type="exact_student_section",
        created_by=counselor_user,
        reason="Protect the reviewed target during this solve.",
        student=student_user.student_profile,
        section=sections[0],
        course=course,
        staffing_mode="sections_only",
    )
    run = _run(academic_year, counselor_user)
    release_student_assignment_lock(
        lock, released_by=counselor_user, release_reason="Reopen the target for review.",
    )

    with pytest.raises(StudentAssignmentConflictError) as error:
        approve_student_assignment_run(run, approved_by=counselor_user, reason="Stale lock test.")
    assert error.value.detail["code"] == STUDENT_ASSIGNMENT_RERUN_CONTEXT_CHANGED


@pytest.mark.django_db
def test_new_relevant_lock_blocks_approval_of_an_older_run(
    academic_year, course, counselor_user, student_user,
):
    _, sections, _ = _setup_assignment_context(
        academic_year, course, counselor_user, [student_user], section_count=1,
    )
    run = _run(academic_year, counselor_user)
    create_student_assignment_lock(
        academic_year=academic_year,
        lock_type="exact_student_section",
        created_by=counselor_user,
        reason="Protect the section after the earlier run was reviewed.",
        student=student_user.student_profile,
        section=sections[0],
        course=course,
        staffing_mode="sections_only",
    )

    with pytest.raises(StudentAssignmentConflictError) as error:
        approve_student_assignment_run(run, approved_by=counselor_user, reason="New lock test.")
    assert error.value.detail["code"] == STUDENT_ASSIGNMENT_RERUN_CONTEXT_CHANGED


@pytest.mark.django_db
def test_section_and_enrollment_drift_block_approval_with_stable_codes(
    academic_year, course, counselor_user, student_user,
):
    offering, sections, _ = _setup_assignment_context(
        academic_year, course, counselor_user, [student_user], section_count=1,
    )
    run = _run(academic_year, counselor_user)
    sections[0].capacity_max = 2
    sections[0].save(update_fields=["capacity_max"])
    with pytest.raises(StudentAssignmentConflictError) as section_error:
        approve_student_assignment_run(run, approved_by=counselor_user, reason="Section drift test.")
    assert section_error.value.detail["code"] == "student_assignment_input_changed_since_run"

    # Restore a fresh candidate, then add an external active enrollment. The
    # candidate may not overwrite or silently duplicate that new operational
    # fact.
    sections[0].capacity_max = 1
    sections[0].save(update_fields=["capacity_max"])
    fresh_run = _run(academic_year, counselor_user)
    Enrollment.objects.create(
        student=student_user.student_profile,
        section=sections[0],
        course_offering=offering,
    )
    with pytest.raises(StudentAssignmentConflictError) as enrollment_error:
        approve_student_assignment_run(fresh_run, approved_by=counselor_user, reason="Enrollment drift test.")
    assert enrollment_error.value.detail["code"] == "student_assignment_input_changed_since_run"


@pytest.mark.django_db
def test_partial_staffing_teacher_change_is_stale_but_mode_a_is_not(
    academic_year, course, counselor_user, student_user, teacher_user,
):
    _, sections, _ = _setup_assignment_context(
        academic_year, course, counselor_user, [student_user], section_count=1,
    )
    run = _run(academic_year, counselor_user, staffing_mode="partial_staffing")
    sections[0].teacher = teacher_user.teacher_profile
    sections[0].save(update_fields=["teacher"])

    with pytest.raises(StudentAssignmentConflictError) as error:
        approve_student_assignment_run(run, approved_by=counselor_user, reason="Staffing drift test.")
    assert error.value.detail["code"] == "student_assignment_staffing_context_changed_since_run"


@pytest.mark.django_db
def test_what_if_unlock_is_deterministic_and_has_no_writes(
    academic_year, course, counselor_user, student_user,
):
    _, sections, _ = _setup_assignment_context(
        academic_year, course, counselor_user, [student_user], section_count=1,
    )
    lock = create_student_assignment_lock(
        academic_year=academic_year,
        lock_type="exact_student_section",
        created_by=counselor_user,
        reason="Protect the current hypothetical target.",
        student=student_user.student_profile,
        section=sections[0],
        course=course,
        staffing_mode="sections_only",
    )
    run = _run(academic_year, counselor_user)
    counts_before = (
        StudentAssignmentLock.objects.count(),
        StudentAssignmentRun.objects.count(),
        Enrollment.objects.count(),
        Section.objects.values_list("id", "lifecycle_status").count(),
    )
    first = preview_student_assignment_unlock(run, lock_ids=[lock.id])
    second = preview_student_assignment_unlock(run, lock_ids=[lock.id])

    assert first == second
    assert counts_before == (
        StudentAssignmentLock.objects.count(),
        StudentAssignmentRun.objects.count(),
        Enrollment.objects.count(),
        Section.objects.values_list("id", "lifecycle_status").count(),
    )
    lock.refresh_from_db()
    assert lock.is_active is True


@pytest.mark.django_db
def test_review_reports_changed_and_protected_categories(
    academic_year, course, counselor_user, student_user,
):
    _, sections, _ = _setup_assignment_context(
        academic_year, course, counselor_user, [student_user], section_count=2,
    )
    initial = _run(academic_year, counselor_user)
    approve_student_assignment_run(initial, approved_by=counselor_user, reason="Initial review.")
    exact = create_student_assignment_lock(
        academic_year=academic_year,
        lock_type="exact_student_section",
        created_by=counselor_user,
        reason="Move the student to the second reviewed section.",
        student=student_user.student_profile,
        section=sections[1],
        course=course,
        staffing_mode="sections_only",
    )
    rerun = _run(academic_year, counselor_user)
    review = preview_student_assignment_approval(rerun)
    assert review["changed_assignments"]
    assert review["moved_student_count"] == 1
    assert exact.is_active is True

    whole = create_student_assignment_lock(
        academic_year=academic_year,
        lock_type="whole_student_schedule",
        created_by=counselor_user,
        reason="Protect the student's complete reviewed schedule.",
        student=student_user.student_profile,
        staffing_mode="sections_only",
    )
    protected_run = _run(academic_year, counselor_user)
    protected_review = preview_student_assignment_approval(protected_run)
    assert protected_review["protected_assignments"]
    assert whole.is_active is True
