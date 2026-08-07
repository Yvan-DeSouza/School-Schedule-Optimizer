from scheduling_engine.demand_analyzer import analyze_demand
from scheduling_engine.dto import CourseDTO, CourseRequestDTO, HistoricalDemandDTO, SchedulingInputDTO
from scheduling_engine.section_estimator import estimate_section_counts


def test_demand_analysis_counts_requests_history_and_co_requests():
    calculus = CourseDTO(1, "MCV4U", "Calculus", 10, 30)
    physics = CourseDTO(2, "SPH4U", "Physics", 10, 30)
    data = SchedulingInputDTO(
        academic_year_id=10,
        courses=(calculus, physics),
        course_requests=(
            CourseRequestDTO(1, 1, "primary"), CourseRequestDTO(1, 2, "alternate"),
            CourseRequestDTO(2, 1, "primary"),
        ),
        historical_demand=(HistoricalDemandDTO(1, 10, 8), HistoricalDemandDTO(1, 20, 18)),
    )

    result = analyze_demand(data)
    calculus_summary, physics_summary = result.summaries
    assert (calculus_summary.primary_requests, calculus_summary.alternate_requests, calculus_summary.total_requests) == (2, 0, 2)
    assert calculus_summary.historical_conversion_ratio == 26 / 30
    assert physics_summary.lacks_historical_data
    assert result.conflict_recommendations[0].co_request_count == 1
    assert result.conflict_recommendations[0].weight == 1.0


def test_section_estimator_uses_fallback_rounding_and_capacity_warning():
    low_demand = CourseDTO(1, "ART4U", "Art", 10, 30)
    established = CourseDTO(2, "SPH4U", "Physics", 10, 25)
    data = SchedulingInputDTO(
        academic_year_id=10,
        courses=(low_demand, established),
        course_requests=tuple(CourseRequestDTO(student, 1, "primary") for student in range(1, 6)) + tuple(CourseRequestDTO(student, 2, "primary") for student in range(6, 36)),
        historical_demand=(HistoricalDemandDTO(2, 100, 80),),
    )

    low, physics = estimate_section_counts(data)
    assert low.recommended_section_count == 1 and low.used_fallback_ratio
    assert any("below" in warning for warning in low.warnings)
    assert physics.predicted_enrollment == 24 and physics.recommended_section_count == 1
