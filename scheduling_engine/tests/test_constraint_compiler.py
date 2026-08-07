import pytest

from scheduling_engine.constraint_compiler import compile_constraints
from scheduling_engine.dto import (
    CourseDTO, CourseQualificationRequirementDTO, CourseRoomRequirementDTO,
    QualificationDTO, RoomDTO, SchedulingInputDTO, SectionDTO, SectionLockDTO,
    TeacherAvailabilityDTO, TeacherDTO, TeacherQualificationDTO, TimeSlotDTO,
)


def complete_input(lock_teacher=True):
    return SchedulingInputDTO(
        academic_year_id=1,
        courses=(CourseDTO(1, "MCV4U", "Calculus", 10, 30),),
        sections=(SectionDTO(1, 1, 1, 1, 10, 30),),
        teachers=(TeacherDTO(1, 3, 6),),
        rooms=(RoomDTO(1, "classroom", 30),),
        timeslots=(TimeSlotDTO(1, 1, 1, "Monday", 1),),
        qualifications=(QualificationDTO(1, "Math"),),
        teacher_qualifications=(TeacherQualificationDTO(1, 1),),
        teacher_availability=(TeacherAvailabilityDTO(1, 1, True),),
        course_room_requirements=(CourseRoomRequirementDTO(1, "classroom"),),
        course_qualification_requirements=(CourseQualificationRequirementDTO(1, 1),),
        section_locks=(SectionLockDTO(1, 1 if lock_teacher else None, 1, 1),),
    )


def test_compiler_builds_solver_indexes_and_preserves_locks():
    compiled = compile_constraints(complete_input())
    assert compiled.qualified_teacher_ids_by_course[1] == frozenset({1})
    assert compiled.available_timeslot_ids_by_teacher[1] == frozenset({1})
    assert compiled.required_room_types_by_course[1] == frozenset({"classroom"})
    assert compiled.locked_sections_by_id[1].locked_room_id == 1


def test_compiler_rejects_lock_to_unqualified_teacher():
    data = complete_input(lock_teacher=True)
    data = SchedulingInputDTO(**{**data.__dict__, "teacher_qualifications": ()})
    with pytest.raises(ValueError, match="lacks a required"):
        compile_constraints(data)
