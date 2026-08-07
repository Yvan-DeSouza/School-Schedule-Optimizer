from scheduling_engine.demand_analyzer import analyze_demand
from scheduling_engine.dto import AcademicYearDTO, CourseDTO, CourseRequestDTO, HistoricalDemandDTO, SchedulingInputDTO
from scheduling_engine.section_estimator import estimate_section_counts
import pytest


def test_demand_analysis_counts_requests_history_and_co_requests():
    calculus = CourseDTO(1, "MCV4U", "Calculus", 10, 30)
    physics = CourseDTO(2, "SPH4U", "Physics", 10, 30)
    data = SchedulingInputDTO(
        academic_year_id=10,
        academic_years=(AcademicYearDTO(8, "2024-2025"), AcademicYearDTO(9, "2025-2026"), AcademicYearDTO(10, "2026-2027")),
        courses=(calculus, physics),
        course_requests=(
            CourseRequestDTO(1, 1, "primary"), CourseRequestDTO(1, 2, "alternate"),
            CourseRequestDTO(2, 1, "primary"),
        ),
        historical_demand=(HistoricalDemandDTO(1, 10, 8, 8), HistoricalDemandDTO(1, 20, 18, 9)),
    )

    result = analyze_demand(data)
    calculus_summary, physics_summary = result.summaries
    assert (calculus_summary.primary_requests, calculus_summary.alternate_requests, calculus_summary.total_requests) == (2, 0, 2)
    assert calculus_summary.historical_conversion_ratio > 26 / 30
    assert physics_summary.lacks_historical_data
    assert result.conflict_recommendations[0].co_request_count == 1
    assert result.conflict_recommendations[0].weight == 1.0


def test_section_estimator_uses_fallback_rounding_and_capacity_warning():
    low_demand = CourseDTO(1, "ART4U", "Art", 10, 30)
    established = CourseDTO(2, "SPH4U", "Physics", 10, 25)
    data = SchedulingInputDTO(
        academic_year_id=10,
        academic_years=(AcademicYearDTO(9, "2025-2026"), AcademicYearDTO(10, "2026-2027")),
        courses=(low_demand, established),
        course_requests=tuple(CourseRequestDTO(student, 1, "primary") for student in range(1, 6)) + tuple(CourseRequestDTO(student, 2, "primary") for student in range(6, 36)),
        historical_demand=(HistoricalDemandDTO(2, 100, 80, 9),),
    )

    low, physics = estimate_section_counts(data)
    assert low.recommended_section_count == 1 and low.used_fallback_ratio
    assert any("below" in warning for warning in low.warnings)
    assert physics.predicted_enrollment == pytest.approx(24) and physics.recommended_section_count == 1


def test_demand_analysis_uses_three_year_recency_half_life_and_ignores_non_history():
    course = CourseDTO(1, "MCV4U", "Calculus", 10, 30)
    data = SchedulingInputDTO(
        academic_year_id=4,
        academic_years=(
            AcademicYearDTO(1, "2023-2024"), AcademicYearDTO(2, "2025-2026"),
            AcademicYearDTO(3, "2026-2027"), AcademicYearDTO(4, "2027-2028"),
            AcademicYearDTO(5, "2028-2029"),
        ),
        courses=(course,),
        historical_demand=(
            HistoricalDemandDTO(1, 100, 50, 1),  # age 4
            HistoricalDemandDTO(1, 100, 100, 2),  # age 2
            HistoricalDemandDTO(1, 100, 0, 3),    # age 1
            HistoricalDemandDTO(1, 100, 100, 4),  # target: excluded
            HistoricalDemandDTO(1, 100, 100, 5),  # future: excluded
            HistoricalDemandDTO(1, 0, 0, 2),      # zero requests: excluded
        ),
    )

    ratio = analyze_demand(data).summaries[0].historical_conversion_ratio
    older_weight = 2 ** (-4 / 3)
    recent_weight = 2 ** (-2 / 3)
    immediately_prior_weight = 2 ** (-1 / 3)
    assert ratio == pytest.approx(
        (50 * older_weight + 100 * recent_weight)
        / (100 * older_weight + 100 * recent_weight + 100 * immediately_prior_weight)
    )


def test_demand_analysis_rejects_malformed_academic_year_names():
    data = SchedulingInputDTO(
        academic_year_id=1,
        academic_years=(AcademicYearDTO(1, "2026/2027"),),
        courses=(CourseDTO(1, "MCV4U", "Calculus", 10, 30),),
    )
    with pytest.raises(ValueError, match="YYYY-YYYY"):
        analyze_demand(data)
