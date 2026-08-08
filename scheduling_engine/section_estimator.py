"""Legacy non-CP-SAT section-count heuristic retained for API compatibility."""

import math

from .demand_analyzer import analyze_demand
from .dto import SchedulingInputDTO, SectionCountRecommendationDTO


def estimate_section_counts(data: SchedulingInputDTO) -> tuple[SectionCountRecommendationDTO, ...]:
    """Return explainable, non-persisted section-count recommendations."""
    # This endpoint predates capacity profiles/staffing feasibility. New planning
    # should use section_planner.py; do not silently change legacy behavior here.
    summaries = {summary.course_id: summary for summary in analyze_demand(data).summaries}
    recommendations = []
    for course in sorted(data.courses, key=lambda item: (item.course_code, item.id)):
        summary = summaries[course.id]
        fallback = summary.historical_conversion_ratio is None
        # A neutral 1:1 ratio makes missing history explicit and predictable.
        ratio = 1.0 if fallback else summary.historical_conversion_ratio
        # Alternates remain visible in demand summaries but do not consume a
        # section unless a planning run explicitly promotes one.
        predicted = summary.primary_requests * ratio
        # Legacy recommendation sizes only against capacity_max and does not
        # check teacher eligibility or semester load.
        count = 0 if predicted == 0 else math.ceil((predicted / course.capacity_max) - 1e-12)
        warnings = []
        if fallback:
            # Warnings are human-readable because this legacy response predates
            # the structured diagnostics used by the CP-SAT planner.
            warnings.append("No usable historical demand; using current demand as the forecast.")
        if 0 < predicted < course.capacity_min:
            warnings.append("Predicted enrollment is below the course minimum capacity.")
        recommendations.append(SectionCountRecommendationDTO(
            course.id, course.course_code, summary.primary_requests, ratio, predicted,
            course.capacity_min, course.capacity_max, count, fallback, tuple(warnings),
        ))
    return tuple(recommendations)
