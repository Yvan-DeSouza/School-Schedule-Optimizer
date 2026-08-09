"""Shared planning-core scenario contract tests."""

from scheduling_engine.dto import SchedulingInputDTO, TeacherDTO
from scheduling_engine.planning_core import remaining_teacher_capacities


def test_is_excluded_adjustment_zeroes_teacher_capacity():
    data = SchedulingInputDTO(
        academic_year_id=1,
        teachers=(TeacherDTO(id=1, max_courses_per_semester=3, max_courses_total=6),),
    )

    capacities = remaining_teacher_capacities(
        data,
        [{"teacher_id": 1, "semester": 1, "is_excluded": True}],
    )

    assert capacities[(1, 1)] == 0
    assert capacities[(1, 2)] == 3
