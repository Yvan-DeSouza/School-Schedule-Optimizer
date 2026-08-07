from collections import Counter, defaultdict

from .dto import (
    CourseConflictRecommendationDTO, DemandAnalysisResultDTO, DemandSummaryDTO,
    SchedulingInputDTO,
)


def analyze_demand(data: SchedulingInputDTO) -> DemandAnalysisResultDTO:
    """Aggregate demand and generate unordered co-request recommendations."""
    courses = {course.id: course for course in data.courses}
    counts = defaultdict(Counter)
    requested_by_student = defaultdict(set)
    for request in data.course_requests:
        if request.course_id not in courses:
            raise ValueError(f"Course request references unknown course {request.course_id}.")
        if request.request_type not in {"primary", "alternate"}:
            raise ValueError(f"Unsupported request type: {request.request_type}.")
        counts[request.course_id][request.request_type] += 1
        requested_by_student[request.student_id].add(request.course_id)

    historical = defaultdict(lambda: [0, 0])
    for record in data.historical_demand:
        if record.course_id not in courses:
            raise ValueError(f"Historical demand references unknown course {record.course_id}.")
        if record.requests > 0:
            historical[record.course_id][0] += record.requests
            historical[record.course_id][1] += record.final_enrollment

    summaries = []
    for course in sorted(data.courses, key=lambda item: (item.course_code, item.id)):
        historical_requests, historical_enrollment = historical[course.id]
        ratio = historical_enrollment / historical_requests if historical_requests else None
        primary = counts[course.id]["primary"]
        alternate = counts[course.id]["alternate"]
        summaries.append(DemandSummaryDTO(course.id, course.course_code, primary, alternate, primary + alternate, ratio, ratio is None))

    pairs = Counter()
    for requested_courses in requested_by_student.values():
        ordered = sorted(requested_courses)
        for index, course_a_id in enumerate(ordered):
            for course_b_id in ordered[index + 1:]:
                pairs[(course_a_id, course_b_id)] += 1
    recommendations = tuple(
        CourseConflictRecommendationDTO(course_a_id, course_b_id, float(count), count)
        for (course_a_id, course_b_id), count in sorted(pairs.items())
    )
    return DemandAnalysisResultDTO(tuple(summaries), recommendations)
