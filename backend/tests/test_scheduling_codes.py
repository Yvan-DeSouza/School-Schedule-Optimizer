"""Stable scheduling-workflow codes reserved for student reruns and locks."""

from backend.apps.scheduling import codes


def test_student_assignment_rerun_workflow_codes_are_stable():
    """Keep future API consumers from depending on mutable error prose."""

    assert {
        "STUDENT_ASSIGNMENT_LOCK_INVALID_TARGET": codes.STUDENT_ASSIGNMENT_LOCK_INVALID_TARGET,
        "STUDENT_ASSIGNMENT_LOCK_FINAL_STAFFING_REQUIRED": codes.STUDENT_ASSIGNMENT_LOCK_FINAL_STAFFING_REQUIRED,
        "STUDENT_ASSIGNMENT_RERUN_SCOPE_INVALID": codes.STUDENT_ASSIGNMENT_RERUN_SCOPE_INVALID,
        "STUDENT_ASSIGNMENT_RERUN_CONTEXT_CHANGED": codes.STUDENT_ASSIGNMENT_RERUN_CONTEXT_CHANGED,
        "STUDENT_ASSIGNMENT_WHAT_IF_LOCK_NOT_ACTIVE": codes.STUDENT_ASSIGNMENT_WHAT_IF_LOCK_NOT_ACTIVE,
        "STUDENT_ASSIGNMENT_SECTION_CANCELLATION_REQUIRES_RERUN": codes.STUDENT_ASSIGNMENT_SECTION_CANCELLATION_REQUIRES_RERUN,
    } == {
        "STUDENT_ASSIGNMENT_LOCK_INVALID_TARGET": "student_assignment_lock_invalid_target",
        "STUDENT_ASSIGNMENT_LOCK_FINAL_STAFFING_REQUIRED": "student_assignment_lock_final_staffing_required",
        "STUDENT_ASSIGNMENT_RERUN_SCOPE_INVALID": "student_assignment_rerun_scope_invalid",
        "STUDENT_ASSIGNMENT_RERUN_CONTEXT_CHANGED": "student_assignment_rerun_context_changed",
        "STUDENT_ASSIGNMENT_WHAT_IF_LOCK_NOT_ACTIVE": "student_assignment_what_if_lock_not_active",
        "STUDENT_ASSIGNMENT_SECTION_CANCELLATION_REQUIRES_RERUN": "student_assignment_section_cancellation_requires_rerun",
    }
