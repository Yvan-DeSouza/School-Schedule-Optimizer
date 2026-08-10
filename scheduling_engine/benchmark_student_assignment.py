"""Reproducible target-scale fixture for manual student-assignment benchmarking.

This is intentionally not a normal test: a timing assertion would be unstable
across developer machines. Run it before claiming the first release is ready
for the SDD target scale, and record elapsed time plus fulfillment counts.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from time import perf_counter

from .dto import (
    StudentAssignmentInputDTO,
    StudentAssignmentRequestDTO,
    StudentAssignmentSectionDTO,
)
from .student_assignment import solve_student_assignment


def build_student_assignment_target_scale_fixture(
    *, student_count=1400, section_count=300,
) -> StudentAssignmentInputDTO:
    """Build deterministic ~1,400-student/~300-section detached input facts."""

    course_count = 50
    sections_per_course = section_count // course_count
    if section_count % course_count or sections_per_course < 1:
        raise ValueError("section_count must be a positive multiple of 50.")
    sections = []
    for course_id in range(1, course_count + 1):
        for index in range(sections_per_course):
            timeslot_id = 1 + ((course_id + index) % 8)
            sections.append(StudentAssignmentSectionDTO(
                section_id=(course_id - 1) * sections_per_course + index + 1,
                delivery_group_id=course_id,
                member_course_offering_ids=(1000 + course_id,),
                member_course_ids=(course_id,),
                semester=1 if timeslot_id <= 4 else 2,
                timeslot_id=timeslot_id,
                capacity_max=35,
                target_capacity=28,
            ))
    requests = []
    request_id = 1
    # Seven primary requests per student approximate a full timetable while
    # retaining enough aggregate capacity for quality measurements.
    for student_id in range(1, student_count + 1):
        for offset in range(7):
            course_id = 1 + ((student_id * 7 + offset) % course_count)
            requests.append(StudentAssignmentRequestDTO(
                request_id=request_id,
                student_id=student_id,
                course_id=course_id,
                course_offering_id=1000 + course_id,
                is_primary=True,
                is_mandatory=offset < 4,
                priority_tier=1 if offset < 4 else 4,
            ))
            request_id += 1
    return StudentAssignmentInputDTO(
        academic_year_id=1,
        requests=tuple(requests),
        sections=tuple(sections),
        fixed_enrollments=(),
        hard_prerequisites=(),
        soft_sequence_preferences=(),
        section_utilization_balance_importance="important",
        student_semester_balance_importance="important",
        course_sequence_preferences_importance="not_important",
        time_limit_seconds=30.0,
    )


def summarize_student_assignment_target_scale_fixture(data: StudentAssignmentInputDTO):
    """Return the benchmark's non-sensitive feasibility facts before solving.

    The target-scale fixture is intentionally synthetic, but a benchmark result
    is only interpretable alongside its required demand and usable capacity.
    This summary reports those facts without changing the fixture or relaxing
    any scheduling rule.
    """

    required_requests = tuple(
        request for request in data.requests if request.is_mandatory or request.is_primary
    )
    sections_by_offering = defaultdict(list)
    for section in data.sections:
        for offering_id in section.member_course_offering_ids:
            sections_by_offering[offering_id].append(section)

    usable_sections = tuple(
        section
        for section in data.sections
        if section.semester in {1, 2} and section.timeslot_id > 0 and section.capacity_max >= 0
    )
    required_by_course = Counter(request.course_id for request in required_requests)
    capacity_by_course = Counter()
    for section in usable_sections:
        # This benchmark deliberately uses one offering/course per physical
        # section, so its per-course capacity is not double-counted shared
        # combined-delivery capacity.
        for course_id in section.member_course_ids:
            capacity_by_course[course_id] += section.capacity_max

    course_capacity_shortages = {
        course_id: required_by_course[course_id] - capacity_by_course[course_id]
        for course_id in sorted(required_by_course)
        if required_by_course[course_id] > capacity_by_course[course_id]
    }
    return {
        "student_count": len({request.student_id for request in data.requests}),
        "section_count": len(data.sections),
        "request_count": len(data.requests),
        "required_demand": len(required_requests),
        "mandatory_request_count": sum(request.is_mandatory for request in data.requests),
        "primary_request_count": sum(request.is_primary for request in data.requests),
        "alternate_request_count": sum(not request.is_primary for request in data.requests),
        "nominal_seat_capacity": sum(section.capacity_max for section in data.sections),
        "usable_seat_capacity": sum(section.capacity_max for section in usable_sections),
        "seat_surplus_deficit": sum(section.capacity_max for section in usable_sections) - len(required_requests),
        "requests_without_eligible_section": sum(
            not sections_by_offering[request.course_offering_id]
            for request in required_requests
        ),
        "course_capacity_shortages": course_capacity_shortages,
    }


def run_target_scale_benchmark():
    """Return elapsed seconds and non-sensitive result-quality measures."""

    data = build_student_assignment_target_scale_fixture()
    started = perf_counter()
    result = solve_student_assignment(data)
    return {
        "elapsed_seconds": round(perf_counter() - started, 3),
        "status": result.status,
        "solver_outcome": result.solver_outcome,
        "assignment_count": len(result.assignments),
        "unmet_request_count": len(result.unmet_requests),
        "objective_components": dict(result.objective_components),
        **summarize_student_assignment_target_scale_fixture(data),
    }


if __name__ == "__main__":
    summary = run_target_scale_benchmark()
    for key, value in summary.items():
        print(f"{key}: {value}")
