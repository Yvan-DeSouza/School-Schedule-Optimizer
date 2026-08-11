"""Explainable metadata and historical course-difficulty estimates."""

from collections import defaultdict
from decimal import Decimal

from backend.apps.common.school_values import GRADE_LEVEL_7, GRADE_LEVEL_12


COURSE_DIFFICULTY_CALCULATION_VERSION = "metadata_and_relative_history_v2"
DESIGNATION_ADJUSTMENTS = {"U": 10, "M": 6, "C": 0, "E": -8, "D": 4, "P": -3, "W": 0, "O": -6, "L": -10}
CATEGORY_ADJUSTMENTS = {"math": 3, "science": 3, "language": 1, "humanities": 1, "technology": 0, "business": 0, "arts": -1}


def _clamp(value):
    return max(0, min(100, int(round(value))))


def _designation(course):
    value = (course.course_code or "").strip().upper()
    return value[-1] if value and value[-1].isalpha() else ""


def metadata_course_difficulty(course):
    """Return a small, explainable metadata estimate when marks are unavailable."""

    grade = min(max(int(course.grade_level), GRADE_LEVEL_7), GRADE_LEVEL_12)
    grade_score = 20 + round((grade - GRADE_LEVEL_7) * 60 / (GRADE_LEVEL_12 - GRADE_LEVEL_7))
    designation = _designation(course)
    return _clamp(grade_score + DESIGNATION_ADJUSTMENTS.get(designation, 0) + CATEGORY_ADJUSTMENTS.get(course.category, 0))


def course_difficulty_facts(course, *, historical_results=(), student_year_results=None):
    """Return deterministic facts for a catalog course.

    Each result is compared to that student's same-year leave-one-course-out
    average. Recent years use a 0.70 decay per newer observed academic year;
    confidence reaches one at twelve weighted observations, keeping sparse
    history close to the metadata estimate.
    """

    metadata = metadata_course_difficulty(course)
    student_year_results = student_year_results or {}
    rows = list(historical_results)
    ordered_years = sorted({row.academic_year.name for row in rows})
    year_weight = {year: 0.70 ** (len(ordered_years) - index - 1) for index, year in enumerate(ordered_years)}
    weighted_marks = weighted_relative = total_weight = Decimal("0")
    usable = 0
    for row in rows:
        peers = [item for item in student_year_results.get((row.student_id, row.academic_year_id), ()) if item.course_id != course.id]
        if not peers:
            continue
        baseline = sum(Decimal(item.final_mark) for item in peers) / len(peers)
        weight = Decimal(str(year_weight[row.academic_year.name]))
        weighted_marks += Decimal(row.final_mark) * weight
        weighted_relative += (Decimal(row.final_mark) - baseline) * weight
        total_weight += weight
        usable += 1
    if total_weight:
        course_average = weighted_marks / total_weight
        relative_signal = weighted_relative / total_weight
        historical = _clamp(50 + (Decimal("75") - course_average) * Decimal("0.5") - relative_signal * Decimal("2"))
        confidence = min(1.0, float(total_weight / Decimal("12")))
        calculated = _clamp(metadata * (1 - confidence) + historical * confidence)
        source = "historical_and_metadata"
    else:
        course_average = relative_signal = None
        confidence = 0.0
        calculated = metadata
        source = "metadata"
    override = course.manual_difficulty_override
    return {
        "course_id": course.id, "category": course.category,
        "calculated_difficulty": calculated, "manual_difficulty_override": override,
        "effective_difficulty": calculated if override is None else int(override),
        "calculation_version": COURSE_DIFFICULTY_CALCULATION_VERSION,
        "source": "manual_override" if override is not None else source,
        "metadata_difficulty": metadata, "designation": _designation(course),
        "historical_observation_count": usable, "historical_year_count": len(ordered_years),
        "historical_confidence": confidence,
        "weighted_course_average": None if course_average is None else float(course_average),
        "relative_performance_signal": None if relative_signal is None else float(relative_signal),
    }
