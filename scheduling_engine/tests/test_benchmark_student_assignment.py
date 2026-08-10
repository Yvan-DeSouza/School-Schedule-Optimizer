"""Deterministic shape checks for the manual target-scale benchmark fixture."""

from scheduling_engine.benchmark_student_assignment import (
    build_student_assignment_target_scale_fixture,
    summarize_student_assignment_target_scale_fixture,
)


def test_target_scale_fixture_reports_its_required_demand_and_capacity():
    summary = summarize_student_assignment_target_scale_fixture(
        build_student_assignment_target_scale_fixture(),
    )

    assert summary == {
        "student_count": 1400,
        "section_count": 300,
        "request_count": 9800,
        "required_demand": 9800,
        "mandatory_request_count": 5600,
        "primary_request_count": 9800,
        "alternate_request_count": 0,
        "nominal_seat_capacity": 10500,
        "usable_seat_capacity": 10500,
        "seat_surplus_deficit": 700,
        "requests_without_eligible_section": 0,
        "course_capacity_shortages": {},
    }
