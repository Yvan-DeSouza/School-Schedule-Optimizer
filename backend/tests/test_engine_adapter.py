import pytest

from backend.apps.common.constants import (
    COURSE_CATEGORY_SCIENCE,
    COURSE_REQUEST_TYPE_PRIMARY,
    GRADE_LEVEL_12,
    QUALIFICATION_DIVISION_SENIOR,
    QUALIFICATION_SUBJECT_MATHEMATICS,
    ROOM_TYPE_CLASSROOM,
    SCHEDULE_BLOCK_A,
    SEMESTER_FALL,
)
from backend.apps.common.models import AcademicYear, HistoricalCourseDemand, Room
from backend.apps.constraints.models import (
    CounselorConstraintPreference, CourseConflict, CourseQualificationRequirement,
    CourseRoomRequirement, HardConstraint, Qualification, SoftConstraint,
    TeacherAvailability, TeacherCoursePreference, TeacherCurrentCourse, TeacherQualification,
)
from backend.apps.control.models import SectionLock
from backend.apps.courses.models import Course, CoursePrerequisite, CourseRequest, Section
from backend.apps.scheduling.models import TimeSlot
from backend.apps.scheduling.services.engine_adapter import load_scheduling_input


@pytest.mark.django_db
def test_engine_adapter_loads_target_year_data_and_constraints(
    academic_year, course, student_user, teacher_user, counselor_user,
):
    history_year = AcademicYear.objects.create(name="2025-2026")
    other_course = Course.objects.create(name="Physics", grade_level=GRADE_LEVEL_12, course_code="SPH4U", category=COURSE_CATEGORY_SCIENCE, capacity_min=10, capacity_max=30)
    HistoricalCourseDemand.objects.create(course=course, academic_year=history_year, requests=100, final_enrollment=90)
    CourseRequest.objects.create(student=student_user.student_profile, academic_year=academic_year, course=course, request_type=COURSE_REQUEST_TYPE_PRIMARY)
    section = Section.objects.create(course=course, section_number="01", academic_year=academic_year, semester=SEMESTER_FALL, capacity_min=10, capacity_max=30)
    room = Room.objects.create(name="101", room_type=ROOM_TYPE_CLASSROOM, capacity=30)
    timeslot = TimeSlot.objects.create(academic_year=academic_year, semester=SEMESTER_FALL, block=SCHEDULE_BLOCK_A)
    qualification = Qualification.objects.create(
        code="mathematics-senior",
        name="Mathematics - Senior",
        subject_code=QUALIFICATION_SUBJECT_MATHEMATICS,
        division=QUALIFICATION_DIVISION_SENIOR,
    )
    TeacherQualification.objects.create(teacher=teacher_user.teacher_profile, qualification=qualification)
    TeacherCoursePreference.objects.create(teacher=teacher_user.teacher_profile, course=course)
    TeacherCurrentCourse.objects.create(teacher=teacher_user.teacher_profile, course=course, academic_year=academic_year)
    TeacherAvailability.objects.create(teacher=teacher_user.teacher_profile, timeslot=timeslot)
    CourseRoomRequirement.objects.create(course=course, room_type=ROOM_TYPE_CLASSROOM)
    CourseQualificationRequirement.objects.create(course=course, qualification=qualification)
    CoursePrerequisite.objects.create(course=course, prerequisite=other_course)
    CourseConflict.objects.create(course_a=course, course_b=other_course, weight=1)
    SectionLock.objects.create(section=section, locked_teacher=teacher_user.teacher_profile, locked_timeslot=timeslot, locked_room=room)
    hard = HardConstraint.objects.create(name="Capacity", type="capacity")
    soft = SoftConstraint.objects.create(name="Balance", category="balance_semesters")
    CounselorConstraintPreference.objects.create(counselor=counselor_user.counselor_profile, constraint=soft, weight=2)

    data = load_scheduling_input(academic_year.id)

    assert data.academic_year_id == academic_year.id
    assert {item.id for item in data.academic_years} == {academic_year.id, history_year.id}
    assert data.course_requests[0].student_id == student_user.student_profile.id
    assert data.historical_demand[0].academic_year_id == history_year.id
    assert data.sections[0].id == section.id and data.section_locks[0].locked_room_id == room.id
    assert data.timeslots[0].block == SCHEDULE_BLOCK_A and data.teacher_availability[0].timeslot_id == timeslot.id
    assert data.courses[0].requires_statutory_qualification is True
    assert data.qualifications[0].code == "mathematics-senior"
    assert data.qualifications[0].division == QUALIFICATION_DIVISION_SENIOR
    assert data.course_qualification_requirements[0].is_required is True
    assert data.teacher_qualifications and data.teacher_preferences and data.teacher_current_courses
    assert data.course_room_requirements and data.course_qualification_requirements and data.course_prerequisites
    assert data.course_conflicts and data.hard_constraints and data.soft_constraints and data.counselor_constraint_preferences
