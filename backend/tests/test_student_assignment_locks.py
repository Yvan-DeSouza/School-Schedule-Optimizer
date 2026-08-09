"""Model, service, and action-policy contracts for student-assignment locks."""

import pytest
from django.core.exceptions import ValidationError

from backend.apps.access.action_policies import (
    StudentAssignmentLockAction,
    StudentAssignmentLockActionPolicy,
)
from backend.apps.common.exceptions import DomainValidationError
from backend.apps.scheduling.constants import (
    SECTION_LIFECYCLE_ACTIVE,
    STUDENT_ASSIGNMENT_LOCK_TYPE_EXACT_SECTION,
    STUDENT_ASSIGNMENT_LOCK_TYPE_STUDENT_GROUP,
    STUDENT_ASSIGNMENT_LOCK_TYPE_STUDENT_TEACHER,
    STUDENT_ASSIGNMENT_RUN_SCOPE_SCOPED,
    STUDENT_ASSIGNMENT_STAFFING_MODE_FINAL_STAFFING,
    STUDENT_ASSIGNMENT_STAFFING_MODE_PARTIAL_STAFFING,
)
from backend.apps.scheduling.models import (
    StudentAssignmentLock,
    StudentAssignmentRun,
)
from backend.apps.scheduling.services.student_assignment_locks import (
    create_student_assignment_lock,
    release_student_assignment_lock,
)
from backend.apps.courses.models import Section


def _section(academic_year, course):
    return Section.objects.create(
        course=course,
        academic_year=academic_year,
        semester=1,
        section_number="S1-01",
        capacity_min=10,
        capacity_max=30,
        lifecycle_status=SECTION_LIFECYCLE_ACTIVE,
    )


@pytest.mark.django_db
def test_exact_lock_has_audited_one_way_release_and_immutable_targets(
    academic_year, course, student_user, counselor_user,
):
    section = _section(academic_year, course)
    lock = create_student_assignment_lock(
        academic_year=academic_year,
        lock_type=STUDENT_ASSIGNMENT_LOCK_TYPE_EXACT_SECTION,
        created_by=counselor_user,
        reason="Keep this reviewed placement in the selected class.",
        student=student_user.student_profile,
        section=section,
        course=course,
    )

    released = release_student_assignment_lock(
        lock,
        released_by=counselor_user,
        release_reason="The counselor approved a later targeted rerun.",
    )

    assert released.is_active is False
    assert released.is_released is True
    assert released.released_by_id == counselor_user.id
    assert released.release_reason == "The counselor approved a later targeted rerun."
    with pytest.raises(ValidationError, match="Released student-assignment locks are immutable"):
        released.save()
    with pytest.raises(ValidationError, match="append-only"):
        released.delete()


@pytest.mark.django_db
def test_group_lock_requires_multiple_same_year_members(
    academic_year, course, student_user, second_student_user, counselor_user,
):
    lock = create_student_assignment_lock(
        academic_year=academic_year,
        lock_type=STUDENT_ASSIGNMENT_LOCK_TYPE_STUDENT_GROUP,
        created_by=counselor_user,
        reason="Keep this counselor-approved student group together.",
        course=course,
        group_students=[student_user.student_profile, second_student_user.student_profile],
    )

    assert list(lock.members.values_list("student_id", flat=True)) == [
        student_user.student_profile.id,
        second_student_user.student_profile.id,
    ]
    with pytest.raises(DomainValidationError, match="at least two"):
        create_student_assignment_lock(
            academic_year=academic_year,
            lock_type=STUDENT_ASSIGNMENT_LOCK_TYPE_STUDENT_GROUP,
            created_by=counselor_user,
            reason="A group must contain more than one student.",
            course=course,
            group_students=[student_user.student_profile],
        )


@pytest.mark.django_db
def test_teacher_lock_requires_final_staffing_mode(
    academic_year, course, student_user, teacher_user, counselor_user,
):
    with pytest.raises(DomainValidationError) as error:
        create_student_assignment_lock(
            academic_year=academic_year,
            lock_type=STUDENT_ASSIGNMENT_LOCK_TYPE_STUDENT_TEACHER,
            created_by=counselor_user,
            reason="Keep this student's course with the named teacher.",
            student=student_user.student_profile,
            course=course,
            teacher=teacher_user.teacher_profile,
            staffing_mode=STUDENT_ASSIGNMENT_STAFFING_MODE_PARTIAL_STAFFING,
        )
    assert error.value.detail["code"] == "student_assignment_lock_final_staffing_required"

    lock = create_student_assignment_lock(
        academic_year=academic_year,
        lock_type=STUDENT_ASSIGNMENT_LOCK_TYPE_STUDENT_TEACHER,
        created_by=counselor_user,
        reason="The final staffing context supports this teacher lock.",
        student=student_user.student_profile,
        course=course,
        teacher=teacher_user.teacher_profile,
        staffing_mode=STUDENT_ASSIGNMENT_STAFFING_MODE_FINAL_STAFFING,
    )
    assert lock.teacher_id == teacher_user.teacher_profile.id


@pytest.mark.django_db
def test_scoped_run_requires_an_accepted_source_approval(academic_year):
    run = StudentAssignmentRun(
        academic_year=academic_year,
        staffing_mode="sections_only",
        status="complete",
        scope_type=STUDENT_ASSIGNMENT_RUN_SCOPE_SCOPED,
    )
    with pytest.raises(ValidationError, match="requires a source approval"):
        run.full_clean()


@pytest.mark.django_db
def test_student_assignment_lock_policy_is_fail_closed_by_operation_and_role(
    counselor_user, staff_user, director_user, student_user, teacher_user, unknown_user,
):
    write_actions = StudentAssignmentLockActionPolicy.write_actions
    query_actions = StudentAssignmentLockActionPolicy.query_actions

    for action in write_actions:
        assert StudentAssignmentLockActionPolicy.can_execute(counselor_user, action=action)
        assert StudentAssignmentLockActionPolicy.can_execute(director_user, action=action)
        assert not StudentAssignmentLockActionPolicy.can_execute(staff_user, action=action)
        assert not StudentAssignmentLockActionPolicy.can_execute(student_user, action=action)
        assert not StudentAssignmentLockActionPolicy.can_execute(teacher_user, action=action)
        assert not StudentAssignmentLockActionPolicy.can_execute(unknown_user, action=action)

    for action in query_actions:
        for user in (counselor_user, staff_user, director_user):
            assert StudentAssignmentLockActionPolicy.can_execute(user, action=action)
        for user in (student_user, teacher_user, unknown_user):
            assert not StudentAssignmentLockActionPolicy.can_execute(user, action=action)

    assert not StudentAssignmentLockActionPolicy.can_execute(
        counselor_user,
        action="not_a_student_assignment_lock_action",
    )
