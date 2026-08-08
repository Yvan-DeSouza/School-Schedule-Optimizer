"""Pure planner contract: candidates, objectives, eligibility, and diagnostics."""

from scheduling_engine.dto import (
    AcademicYearDTO,
    CourseDTO,
    CourseRequestDTO,
    SchedulingInputDTO,
    StudentDTO,
    TeacherDTO,
    TeacherPlanningCapacityDTO,
    TeacherQualificationDTO,
    CourseQualificationRequirementDTO,
    QualificationDTO,
)
from scheduling_engine.section_planner import generate_section_count_candidates, plan_section_counts


def course(*, course_id=1, grade=10, hard_min=10, soft_min=18, target=24, soft_max=30, hard_max=35, priority=4):
    """Build a concise CourseDTO exposing policy fields relevant to tests."""

    return CourseDTO(
        course_id, f"C{course_id}", f"Course {course_id}", hard_min, hard_max,
        grade, "math", False, False, course_id, hard_min, soft_min, target,
        soft_max, hard_max, "either_semester", priority, priority,
    )


def input_for(courses, demand_by_course, capacities=(3, 3)):
    """Build independent students/requests and one teacher capacity witness."""

    requests = []
    students = []
    student_id = 1
    for course_id, demand in demand_by_course.items():
        for _ in range(demand):
            requests.append(CourseRequestDTO(student_id, course_id, True))
            students.append(StudentDTO(student_id, 10))
            student_id += 1
    return SchedulingInputDTO(
        academic_year_id=1,
        academic_years=(AcademicYearDTO(1, "2026-2027"),),
        courses=tuple(courses),
        course_requests=tuple(requests),
        students=tuple(students),
        teachers=(TeacherDTO(1, 3, 6),),
        teacher_planning_capacities=(
            TeacherPlanningCapacityDTO(1, 1, capacities[0]),
            TeacherPlanningCapacityDTO(1, 2, capacities[1]),
        ),
    )


def test_candidates_include_hard_feasible_range_and_under_minimum_review_option():
    # Candidate generation is testable before CP-SAT model compilation.
    candidates = generate_section_count_candidates(60, course())
    assert [item.count for item in candidates] == [0, 2, 3, 4, 5, 6]
    review = generate_section_count_candidates(5, course())
    assert [item.count for item in review] == [0, 1]
    assert review[-1].below_hard_min_review_required is True


def test_planner_prefers_smaller_classes_when_target_distance_ties():
    data = input_for((course(target=24),), {1: 60})
    result = plan_section_counts(data)
    assert result["status"] == "complete"
    assert result["courses"][0]["staffing_feasible_annual_count"] == 3


def test_planner_reoptimizes_globally_when_teacher_capacity_is_limited():
    # Tier 1 demand must win when the shared pool cannot cover every course.
    tier_one = course(course_id=1, priority=1)
    elective = course(course_id=2, priority=4)
    data = input_for((tier_one, elective), {1: 60, 2: 60}, capacities=(2, 2))
    result = plan_section_counts(data)
    by_course = {item["course_id"]: item for item in result["courses"]}
    assert result["status"] == "complete"
    assert by_course[1]["unmet_demand"] == 0
    assert result["capacity_summary"]["planned_sections"] <= 4


def test_grade_twelve_requires_existing_compiled_qualification_eligibility():
    # Senior eligibility is hard and reports why demand becomes unstaffable.
    senior_course = CourseDTO(
        1, "MCV4U", "Calculus", 10, 35, 12, "math", False, True,
        1, 10, 18, 24, 30, 35, "semester_1_only", 1, 1,
    )
    data = input_for((senior_course,), {1: 5}, capacities=(1, 0))
    data = SchedulingInputDTO(
        **{**data.__dict__, "qualifications": (QualificationDTO(1, "Senior Math"),),
           "teacher_qualifications": (TeacherQualificationDTO(1, 1),),
           "course_qualification_requirements": (CourseQualificationRequirementDTO(1, 1, True),)}
    )
    result = plan_section_counts(data)
    assert result["courses"][0]["semester_1_count"] == 1

    unqualified = SchedulingInputDTO(**{**data.__dict__, "teacher_qualifications": ()})
    result = plan_section_counts(unqualified)
    assert result["courses"][0]["staffing_feasible_annual_count"] == 0
    assert result["courses"][0]["unmet_demand"] == 5
    assert result["diagnostics"][0]["code"] == "no_eligible_teachers"
    assert result["diagnostics"][0]["eligible_teacher_count"] == 0


def test_infeasible_course_override_reports_available_candidates():
    # Counselor hard overrides are never ignored or silently rounded.
    data = input_for((course(),), {1: 5})

    result = plan_section_counts(
        data,
        course_constraints=({"course_id": 1, "exact_sections": 3},),
    )

    assert result["status"] == "infeasible"
    assert result["diagnostics"] == [{
        "code": "course_constraint_no_candidate",
        "severity": "error",
        "course_id": 1,
        "course_code": "C1",
        "priority_tier": 4,
        "message": "No section-count candidate satisfies the scenario for C1.",
        "phase": "candidate_generation",
        "requested_constraint": {"course_id": 1, "exact_sections": 3},
        "available_candidate_counts": [0, 1],
        "eligible_teacher_count": 1,
    }]


def test_infeasible_staffing_scenario_reports_capacity_shortfall():
    data = input_for(
        (course(course_id=1), course(course_id=2)),
        {1: 60, 2: 60},
        capacities=(1, 1),
    )

    result = plan_section_counts(
        data,
        course_constraints=(
            {"course_id": 1, "exact_sections": 2},
            {"course_id": 2, "exact_sections": 2},
        ),
    )

    assert result["status"] == "infeasible"
    total_shortfall = next(
        item for item in result["diagnostics"]
        if item["code"] == "total_staffing_capacity_shortfall"
    )
    assert total_shortfall["required_sections"] == 4
    assert total_shortfall["available_sections"] == 2
    assert total_shortfall["shortfall_sections"] == 2
