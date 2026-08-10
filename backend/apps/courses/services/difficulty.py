"""Transparent catalog difficulty estimates for student-assignment snapshots.

The project deliberately has no transcript, mark, or historical-result model.
Until that source exists, difficulty is a bounded grade-level estimate rather
than a claim about measured student performance. The solver receives only the
effective snapshot value; this service keeps the assumption explicit and
replaceable when verified historical evidence becomes available.
"""

from backend.apps.common.school_values import GRADE_LEVEL_7, GRADE_LEVEL_12


COURSE_DIFFICULTY_MINIMUM = 0
COURSE_DIFFICULTY_MAXIMUM = 100
COURSE_DIFFICULTY_CALCULATION_VERSION = "grade_level_baseline_v1"


def calculate_course_difficulty(course):
    """Return the automatic 20--80 grade-level baseline for one catalog course."""

    bounded_grade = min(max(int(course.grade_level), GRADE_LEVEL_7), GRADE_LEVEL_12)
    span = GRADE_LEVEL_12 - GRADE_LEVEL_7
    return 20 + round((bounded_grade - GRADE_LEVEL_7) * 60 / span)


def course_difficulty_facts(course):
    """Return explainable automatic, override, and effective scheduling values."""

    calculated_difficulty = calculate_course_difficulty(course)
    manual_override = course.manual_difficulty_override
    effective_difficulty = calculated_difficulty if manual_override is None else int(manual_override)
    return {
        "course_id": course.id,
        "category": course.category,
        "calculated_difficulty": calculated_difficulty,
        "manual_difficulty_override": manual_override,
        "effective_difficulty": effective_difficulty,
        "calculation_version": COURSE_DIFFICULTY_CALCULATION_VERSION,
        "source": "manual_override" if manual_override is not None else "grade_level_baseline",
    }
