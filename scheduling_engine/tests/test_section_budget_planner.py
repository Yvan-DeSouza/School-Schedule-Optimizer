"""Teacher-independent budget allocation and backup-demand behavior."""

from scheduling_engine.dto import (
    AcademicYearDTO,
    CourseDTO,
    CourseRequestDTO,
    PlanningOfferingDTO,
    SchedulingInputDTO,
)
from scheduling_engine.section_budget_planner import (
    plan_section_budget,
    plan_section_budget_with_backups,
)


def course(course_id):
    return CourseDTO(
        course_id,
        f"C{course_id}",
        f"Course {course_id}",
        10,
        35,
        10,
        "arts",
        False,
        False,
        course_id,
        10,
        18,
        24,
        30,
        35,
        "either_semester",
        4,
        4,
    )


def offering(offering_id, *course_ids, combined=False):
    return PlanningOfferingDTO(
        offering_id,
        tuple(course_ids),
        tuple(f"C{course_id}" for course_id in course_ids),
        offering_id,
        10,
        18,
        24,
        30,
        35,
        "either_semester",
        4,
        combined,
    )


def input_data(courses, offerings, requests):
    return SchedulingInputDTO(
        academic_year_id=1,
        academic_years=(AcademicYearDTO(1, "2026-2027"),),
        courses=tuple(courses),
        planning_offerings=tuple(offerings),
        course_requests=tuple(requests),
    )


def primary_requests(course_id, count, start=1):
    return [
        CourseRequestDTO(student_id, course_id, True)
        for student_id in range(start, start + count)
    ]


def test_exact_budget_allocates_physical_sections_without_zeroing_demand():
    data = input_data(
        (course(1), course(2)),
        (offering(11, 1), offering(22, 2)),
        primary_requests(1, 60) + primary_requests(2, 60, 100),
    )

    result = plan_section_budget(data, section_budget=2, budget_type="exact")

    assert result["status"] == "complete"
    assert result["used_sections"] == 2
    assert {item["annual_count"] for item in result["offerings"]} == {1}
    assert all(item["unmet_demand"] == 25 for item in result["offerings"])


def test_ceiling_budget_does_not_invent_unnecessary_sections():
    data = input_data(
        (course(1),),
        (offering(11, 1),),
        primary_requests(1, 60),
    )

    result = plan_section_budget(data, section_budget=10, budget_type="ceiling")

    assert result["status"] == "complete"
    # Two 35-seat sections serve all 60 students.  Because two and three are
    # equally distant from the 24-seat target, a ceiling run keeps the smaller
    # physical footprint instead of consuming an unnecessary third section.
    assert result["used_sections"] == 2
    assert result["unused_sections"] == 8


def test_positive_demand_cannot_be_silently_given_zero_sections():
    data = input_data(
        (course(1),),
        (offering(11, 1),),
        primary_requests(1, 5),
    )

    result = plan_section_budget(data, section_budget=0, budget_type="exact")

    assert result["status"] == "infeasible"
    assert result["diagnostics"][0]["minimum_meaningful_sections"] == 1


def test_exact_budget_rejects_more_sections_than_demand_can_meaningfully_use():
    data = input_data(
        (course(1),),
        (offering(11, 1),),
        primary_requests(1, 5),
    )

    result = plan_section_budget(data, section_budget=2, budget_type="exact")

    assert result["status"] == "infeasible"
    assert result["diagnostics"][0]["maximum_meaningful_sections"] == 1


def test_combined_offering_is_one_section_and_rejects_pooled_overcapacity():
    valid = input_data(
        (course(1), course(2)),
        (offering(99, 1, 2, combined=True),),
        primary_requests(1, 19) + primary_requests(2, 6, 100),
    )
    invalid = input_data(
        (course(1), course(2)),
        (offering(99, 1, 2, combined=True),),
        primary_requests(1, 30) + primary_requests(2, 10, 100),
    )

    assert plan_section_budget(valid, section_budget=1, budget_type="exact")["used_sections"] == 1
    assert plan_section_budget(invalid, section_budget=1, budget_type="exact")["diagnostics"][0]["code"] == "combined_offering_over_capacity"


def test_available_backup_is_promoted_once_without_rewriting_raw_requests():
    requests = [
        CourseRequestDTO(1, 1, True),
        CourseRequestDTO(1, 2, False),
        CourseRequestDTO(2, 2, True),
    ]
    data = input_data(
        (course(1), course(2)),
        (offering(22, 2),),
        requests,
    )

    result = plan_section_budget_with_backups(
        data,
        section_budget=1,
        budget_type="ceiling",
        cancelled_course_ids=[1],
        backup_policy="promote_available",
    )

    assert result["status"] == "complete"
    assert result["offerings"][0]["predicted_enrollment"] == 2
    assert result["request_resolutions"] == [{
        "student_id": 1,
        "cancelled_course_ids": [1],
        "backup_course_id": 2,
        "outcome": "backup_promoted",
        "unresolved_course_count": 0,
    }]
    assert data.course_requests == tuple(requests)


def test_one_backup_fills_only_one_of_multiple_cancelled_primary_gaps():
    requests = [
        CourseRequestDTO(1, 1, True),
        CourseRequestDTO(1, 2, True),
        CourseRequestDTO(1, 3, False),
        CourseRequestDTO(2, 3, True),
    ]
    data = input_data(
        (course(1), course(2), course(3)),
        (offering(33, 3),),
        requests,
    )

    result = plan_section_budget_with_backups(
        data,
        section_budget=1,
        budget_type="ceiling",
        cancelled_course_ids=[1, 2],
        backup_policy="promote_available",
    )

    resolution = result["request_resolutions"][0]
    assert resolution["outcome"] == "backup_promoted"
    assert resolution["unresolved_course_count"] == 1
    assert result["offerings"][0]["predicted_enrollment"] == 2
