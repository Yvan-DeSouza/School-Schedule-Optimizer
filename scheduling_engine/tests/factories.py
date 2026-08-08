"""Small DTO builders for pure scheduling-engine tests."""

from scheduling_engine.dto import (
    CourseDTO,
    CourseRequestDTO,
    PlanningOfferingDTO,
    SchedulingInputDTO,
    TeacherDTO,
    TeacherPlanningCapacityDTO,
)


def course_dto(course_id=1, *, code="TEST4U", tier=4, hard_min=10, target=24, hard_max=35):
    return CourseDTO(
        id=course_id,
        course_code=code,
        name=code,
        capacity_min=hard_min,
        capacity_max=hard_max,
        hard_min=hard_min,
        soft_min=max(hard_min, min(target, 18)),
        target_capacity=target,
        soft_max=max(target, 30),
        hard_max=hard_max,
        priority_tier=tier,
    )


def offering_dto(offering_id=1, *, course_ids=(1,), codes=("TEST4U",), hard_min=10, target=24, hard_max=35, is_combined=False):
    return PlanningOfferingDTO(
        id=offering_id,
        member_course_ids=tuple(course_ids),
        member_course_codes=tuple(codes),
        capacity_profile_id=1,
        hard_min=hard_min,
        soft_min=max(hard_min, min(target, 18)),
        target_capacity=target,
        soft_max=max(target, 30),
        hard_max=hard_max,
        is_combined=is_combined,
    )


def primary_request(student_id=1, course_id=1):
    return CourseRequestDTO(student_id=student_id, course_id=course_id, is_primary=True)


def teacher_dto(teacher_id=1, *, max_per_semester=3):
    return TeacherDTO(
        id=teacher_id,
        max_courses_per_semester=max_per_semester,
        max_courses_total=max_per_semester * 2,
    )


def teacher_capacity(teacher_id=1, *, semester=1, maximum=3, reserved=0):
    return TeacherPlanningCapacityDTO(
        teacher_id=teacher_id,
        semester=semester,
        maximum_sections=maximum,
        reserved_sections=reserved,
    )


def scheduling_input(**overrides):
    defaults = {"academic_year_id": 1}
    defaults.update(overrides)
    return SchedulingInputDTO(**defaults)
