"""Physical delivery-group staffing feasibility and budget linkage."""

from scheduling_engine.dto import (
    AcademicYearDTO,
    CourseDTO,
    CourseQualificationRequirementDTO,
    CourseRequestDTO,
    CourseRoomRequirementDTO,
    PlanningOfferingDTO,
    QualificationDTO,
    SchedulingInputDTO,
    StudentDTO,
    TeacherDTO,
    TeacherPlanningCapacityDTO,
    TeacherQualificationDTO,
)
from scheduling_engine.staffing_planner import plan_staffing_counts


def course(course_id, qualification_required=True):
    return CourseDTO(
        course_id, f"DAN{course_id}", f"Dance {course_id}", 10, 35,
        11 if qualification_required else 10, "arts", False,
        qualification_required, course_id, 10, 18, 24, 30, 35,
        "either_semester", 4, 4,
    )


def requests(course_id, count, start):
    return tuple(
        CourseRequestDTO(student_id, course_id, True)
        for student_id in range(start, start + count)
    )


def students(*request_groups):
    return tuple(
        StudentDTO(item.student_id, 11)
        for group in request_groups
        for item in group
    )


def test_combined_group_uses_one_section_and_union_of_hard_requirements():
    first = requests(1, 19, 1)
    second = requests(2, 6, 100)
    data = SchedulingInputDTO(
        academic_year_id=1,
        academic_years=(AcademicYearDTO(1, "2026-2027"),),
        courses=(course(1), course(2)),
        planning_offerings=(PlanningOfferingDTO(
            99, (1, 2), ("DAN1", "DAN2"), 99,
            10, 18, 24, 30, 35, "either_semester", 4, True,
        ),),
        course_requests=first + second,
        students=students(first, second),
        teachers=(TeacherDTO(1, 1, 2), TeacherDTO(2, 1, 2)),
        teacher_planning_capacities=(
            TeacherPlanningCapacityDTO(1, 1, 1),
            TeacherPlanningCapacityDTO(1, 2, 0),
            TeacherPlanningCapacityDTO(2, 1, 1),
            TeacherPlanningCapacityDTO(2, 2, 0),
        ),
        qualifications=(QualificationDTO(1, "Dance A"), QualificationDTO(2, "Dance B")),
        teacher_qualifications=(
            TeacherQualificationDTO(1, 1),
            TeacherQualificationDTO(1, 2),
            TeacherQualificationDTO(2, 1),
        ),
        course_qualification_requirements=(
            CourseQualificationRequirementDTO(1, 1, True),
            CourseQualificationRequirementDTO(2, 2, True),
        ),
        course_room_requirements=(
            CourseRoomRequirementDTO(1, "dance_studio"),
            CourseRoomRequirementDTO(2, "accessible_space"),
        ),
    )

    result = plan_staffing_counts(data, approved_budget_counts={99: 1})

    assert result["status"] == "complete"
    assert result["planned_sections"] == 1
    assert result["offerings"][0]["eligible_teacher_count"] == 1
    assert result["offerings"][0]["required_room_types"] == [
        "accessible_space", "dance_studio",
    ]


def test_combined_group_fails_when_no_teacher_covers_every_member():
    first = requests(1, 10, 1)
    second = requests(2, 10, 100)
    base = SchedulingInputDTO(
        academic_year_id=1,
        academic_years=(AcademicYearDTO(1, "2026-2027"),),
        courses=(course(1), course(2)),
        planning_offerings=(PlanningOfferingDTO(
            99, (1, 2), ("DAN1", "DAN2"), 99,
            10, 18, 24, 30, 35, "either_semester", 4, True,
        ),),
        course_requests=first + second,
        students=students(first, second),
        teachers=(TeacherDTO(1, 1, 2), TeacherDTO(2, 1, 2)),
        teacher_planning_capacities=(
            TeacherPlanningCapacityDTO(1, 1, 1),
            TeacherPlanningCapacityDTO(1, 2, 0),
            TeacherPlanningCapacityDTO(2, 1, 1),
            TeacherPlanningCapacityDTO(2, 2, 0),
        ),
        qualifications=(QualificationDTO(1, "Dance A"), QualificationDTO(2, "Dance B")),
        teacher_qualifications=(TeacherQualificationDTO(1, 1), TeacherQualificationDTO(2, 2)),
        course_qualification_requirements=(
            CourseQualificationRequirementDTO(1, 1, True),
            CourseQualificationRequirementDTO(2, 2, True),
        ),
    )

    result = plan_staffing_counts(base, approved_budget_counts={99: 1})

    assert result["status"] == "infeasible"
    assert result["diagnostics"][0]["code"] == "no_eligible_teacher_for_delivery_group"


def test_linked_staffing_run_can_reallocate_but_preserves_budget_total():
    first = requests(1, 60, 1)
    second = requests(2, 60, 100)
    data = SchedulingInputDTO(
        academic_year_id=1,
        academic_years=(AcademicYearDTO(1, "2026-2027"),),
        courses=(course(1), course(2)),
        planning_offerings=(
            PlanningOfferingDTO(11, (1,), ("DAN1",), 1, 10, 18, 24, 30, 35),
            PlanningOfferingDTO(22, (2,), ("DAN2",), 2, 10, 18, 24, 30, 35),
        ),
        course_requests=first + second,
        students=students(first, second),
        teachers=(TeacherDTO(1, 2, 2), TeacherDTO(2, 2, 4)),
        teacher_planning_capacities=(
            TeacherPlanningCapacityDTO(1, 1, 2),
            TeacherPlanningCapacityDTO(1, 2, 0),
            TeacherPlanningCapacityDTO(2, 1, 2),
            TeacherPlanningCapacityDTO(2, 2, 2),
        ),
        qualifications=(QualificationDTO(1, "Dance A"), QualificationDTO(2, "Dance B")),
        teacher_qualifications=(TeacherQualificationDTO(1, 1), TeacherQualificationDTO(2, 2)),
        course_qualification_requirements=(
            CourseQualificationRequirementDTO(1, 1, True),
            CourseQualificationRequirementDTO(2, 2, True),
        ),
    )

    result = plan_staffing_counts(data, approved_budget_counts={11: 3, 22: 3})
    by_id = {item["offering_id"]: item for item in result["offerings"]}

    assert result["status"] == "complete"
    assert result["planned_sections"] == 6
    assert by_id[11]["annual_count"] == 2
    assert by_id[22]["annual_count"] == 4
    assert by_id[11]["budget_count_difference"] == -1
    assert by_id[22]["budget_count_difference"] == 1
