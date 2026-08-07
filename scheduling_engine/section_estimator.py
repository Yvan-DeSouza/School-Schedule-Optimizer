import math

from .demand_analyzer import analyze_demand
from .dto import SchedulingInputDTO, SectionCountRecommendationDTO


def estimate_section_counts(data: SchedulingInputDTO) -> tuple[SectionCountRecommendationDTO, ...]:
    """Return explainable, non-persisted section-count recommendations."""
    summaries = {summary.course_id: summary for summary in analyze_demand(data).summaries}
    recommendations = []
    for course in sorted(data.courses, key=lambda item: (item.course_code, item.id)):
        summary = summaries[course.id]
        fallback = summary.historical_conversion_ratio is None
        ratio = 1.0 if fallback else summary.historical_conversion_ratio
        predicted = summary.total_requests * ratio
        count = 0 if predicted == 0 else math.ceil(predicted / course.capacity_max)
        warnings = []
        if fallback:
            warnings.append("No usable historical demand; using current demand as the forecast.")
        if 0 < predicted < course.capacity_min:
            warnings.append("Predicted enrollment is below the course minimum capacity.")
        recommendations.append(SectionCountRecommendationDTO(
            course.id, course.course_code, summary.total_requests, ratio, predicted,
            course.capacity_min, course.capacity_max, count, fallback, tuple(warnings),
        ))
    return tuple(recommendations)
