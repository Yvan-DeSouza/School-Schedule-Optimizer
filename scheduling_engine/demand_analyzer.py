"""Pure demand aggregation, historical forecasting, and co-request analysis."""

from collections import Counter, defaultdict
import re

from .dto import (
    CourseConflictRecommendationDTO, DemandAnalysisResultDTO, DemandSummaryDTO,
    SchedulingInputDTO,
)


ACADEMIC_YEAR_NAME_PATTERN = re.compile(r"^(?P<start>\d{4})-\d{4}$")
RECENCY_HALF_LIFE_YEARS = 3


def parse_academic_year_start(name: str) -> int:
    """Return the first year from the required ``YYYY-YYYY`` label format."""

    match = ACADEMIC_YEAR_NAME_PATTERN.fullmatch(name)
    if not match:
        raise ValueError(f"Academic year name must use YYYY-YYYY format: {name!r}.")
    return int(match.group("start"))


def analyze_demand(data: SchedulingInputDTO) -> DemandAnalysisResultDTO:
    """Aggregate demand and generate unordered co-request recommendations."""
    # Index the DTO identity universe before validating dependent records.
    courses = {course.id: course for course in data.courses}
    academic_years = {year.id: year for year in data.academic_years}
    if data.academic_year_id not in academic_years:
        raise ValueError(f"Target academic year {data.academic_year_id} is missing from the input.")
    target_start_year = parse_academic_year_start(academic_years[data.academic_year_id].name)
    # Counter per course preserves primary/alternate provenance while a separate
    # student set supports pairwise co-request conflict weights.
    counts = defaultdict(Counter)
    requested_by_student = defaultdict(set)
    for request in data.course_requests:
        if request.course_id not in courses:
            raise ValueError(f"Course request references unknown course {request.course_id}.")
        if not isinstance(request.is_primary, bool):
            raise ValueError("Course request is_primary must be a boolean.")
        request_bucket = "primary" if request.is_primary else "alternate"
        counts[request.course_id][request_bucket] += 1
        # Unused backups are not simultaneous demand and therefore must not
        # manufacture placement-conflict weights. A promoted backup is copied
        # into a run's effective input as a primary request before this analysis.
        if request.is_primary:
            requested_by_student[request.student_id].add(request.course_id)

    # Store recency-weighted request and enrollment numerators separately; their
    # ratio becomes the observed conversion from requests to final enrollment.
    historical = defaultdict(lambda: [0.0, 0.0])
    for record in data.historical_demand:
        if record.course_id not in courses:
            raise ValueError(f"Historical demand references unknown course {record.course_id}.")
        if record.academic_year_id not in academic_years:
            raise ValueError(f"Historical demand references unknown academic year {record.academic_year_id}.")
        history_start_year = parse_academic_year_start(academic_years[record.academic_year_id].name)
        age_in_years = target_start_year - history_start_year
        if record.requests > 0 and age_in_years > 0:
            # Exponential decay gives recent years more influence without an
            # arbitrary hard cutoff. Same/future years never train the forecast.
            weight = 2 ** (-age_in_years / RECENCY_HALF_LIFE_YEARS)
            historical[record.course_id][0] += record.requests * weight
            historical[record.course_id][1] += record.final_enrollment * weight

    summaries = []
    for course in sorted(data.courses, key=lambda item: (item.course_code, item.id)):
        # Courses with no requests still receive a summary so downstream planners
        # can make an explicit zero-offering decision.
        historical_requests, historical_enrollment = historical[course.id]
        ratio = historical_enrollment / historical_requests if historical_requests else None
        primary = counts[course.id]["primary"]
        alternate = counts[course.id]["alternate"]
        summaries.append(DemandSummaryDTO(course.id, course.course_code, primary, alternate, primary + alternate, ratio, ratio is None))

    # Count each unordered pair at most once per student. A student's primary and
    # alternate status does not duplicate the same course pair.
    pairs = Counter()
    for requested_courses in requested_by_student.values():
        ordered = sorted(requested_courses)
        # Sorting produces canonical pair keys and deterministic output.
        for index, course_a_id in enumerate(ordered):
            for course_b_id in ordered[index + 1:]:
                pairs[(course_a_id, course_b_id)] += 1
    recommendations = tuple(
        # Current implementation uses co-request count directly as weight while
        # preserving the integer evidence separately for explainability.
        CourseConflictRecommendationDTO(course_a_id, course_b_id, float(count), count)
        for (course_a_id, course_b_id), count in sorted(pairs.items())
    )
    return DemandAnalysisResultDTO(tuple(summaries), recommendations)
