"""Stable solver-diagnostic contracts reserved for future student reruns."""

from scheduling_engine import diagnostics


def test_student_assignment_rerun_diagnostic_codes_are_stable():
    """Keep future review clients independent from counselor-facing prose."""

    assert {
        "STUDENT_ASSIGNMENT_NO_ACTIVE_PLACED_SECTION": diagnostics.STUDENT_ASSIGNMENT_NO_ACTIVE_PLACED_SECTION,
        "STUDENT_ASSIGNMENT_LOCKED_ENROLLMENT_BLOCKS_REQUEST": diagnostics.STUDENT_ASSIGNMENT_LOCKED_ENROLLMENT_BLOCKS_REQUEST,
        "STUDENT_ASSIGNMENT_SECTION_BELOW_TARGET_CAPACITY": diagnostics.STUDENT_ASSIGNMENT_SECTION_BELOW_TARGET_CAPACITY,
        "STUDENT_ASSIGNMENT_SECTION_OVER_TARGET_CONCENTRATION": diagnostics.STUDENT_ASSIGNMENT_SECTION_OVER_TARGET_CONCENTRATION,
        "STUDENT_ASSIGNMENT_LIMITED_SEAT_CONTENTION": diagnostics.STUDENT_ASSIGNMENT_LIMITED_SEAT_CONTENTION,
        "STUDENT_ASSIGNMENT_REQUIRES_ADDITIONAL_CAPACITY": diagnostics.STUDENT_ASSIGNMENT_REQUIRES_ADDITIONAL_CAPACITY,
        "STUDENT_ASSIGNMENT_REQUIRES_TIMESLOT_CHANGE": diagnostics.STUDENT_ASSIGNMENT_REQUIRES_TIMESLOT_CHANGE,
        "STUDENT_ASSIGNMENT_REQUIRES_LOCK_RELEASE": diagnostics.STUDENT_ASSIGNMENT_REQUIRES_LOCK_RELEASE,
        "STUDENT_ASSIGNMENT_REQUIRES_PLACED_SECTION": diagnostics.STUDENT_ASSIGNMENT_REQUIRES_PLACED_SECTION,
        "STUDENT_ASSIGNMENT_REQUIRES_PREREQUISITE_SEQUENCE_CHANGE": diagnostics.STUDENT_ASSIGNMENT_REQUIRES_PREREQUISITE_SEQUENCE_CHANGE,
    } == {
        "STUDENT_ASSIGNMENT_NO_ACTIVE_PLACED_SECTION": "student_assignment_no_active_placed_section",
        "STUDENT_ASSIGNMENT_LOCKED_ENROLLMENT_BLOCKS_REQUEST": "student_assignment_locked_enrollment_blocks_request",
        "STUDENT_ASSIGNMENT_SECTION_BELOW_TARGET_CAPACITY": "student_assignment_section_below_target_capacity",
        "STUDENT_ASSIGNMENT_SECTION_OVER_TARGET_CONCENTRATION": "student_assignment_section_over_target_concentration",
        "STUDENT_ASSIGNMENT_LIMITED_SEAT_CONTENTION": "student_assignment_limited_seat_contention",
        "STUDENT_ASSIGNMENT_REQUIRES_ADDITIONAL_CAPACITY": "student_assignment_requires_additional_capacity",
        "STUDENT_ASSIGNMENT_REQUIRES_TIMESLOT_CHANGE": "student_assignment_requires_timeslot_change",
        "STUDENT_ASSIGNMENT_REQUIRES_LOCK_RELEASE": "student_assignment_requires_lock_release",
        "STUDENT_ASSIGNMENT_REQUIRES_PLACED_SECTION": "student_assignment_requires_placed_section",
        "STUDENT_ASSIGNMENT_REQUIRES_PREREQUISITE_SEQUENCE_CHANGE": "student_assignment_requires_prerequisite_sequence_change",
    }
