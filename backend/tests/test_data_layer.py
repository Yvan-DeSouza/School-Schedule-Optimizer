import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from backend.apps.common.constants import (
    COURSE_CATEGORY_MATH,
    COURSE_REQUEST_TYPE_ALTERNATE,
    COURSE_REQUEST_TYPE_PRIMARY,
    GRADE_LEVEL_12,
    ROOM_TYPE_CLASSROOM,
    ROOM_TYPE_SCIENCE_LAB,
    SCHEDULE_BLOCK_A,
    SEMESTER_FALL,
    SEMESTER_WINTER,
)
from backend.apps.common.models import AcademicYear, HistoricalCourseDemand, Room
from backend.apps.constraints.models.base import Qualification
from backend.apps.constraints.models.course import (
    CourseQualificationRequirement,
    CourseRoomRequirement,
)
from backend.apps.constraints.models.teacher import (
    TeacherAvailability,
    TeacherCoursePreference,
    TeacherCurrentCourse,
    TeacherQualification,
)
from backend.apps.control.models import ManualOverride, SectionLock
from backend.apps.courses.models import (
    Course,
    CoursePrerequisite,
    CourseRequest,
    Enrollment,
    Section,
)
from backend.apps.people.models import Student, Teacher
from backend.apps.scheduling.models import SectionSchedule, TimeSlot


@pytest.fixture
def academic_year():
    return AcademicYear.objects.create(name="2026-2027")


@pytest.fixture
def course():
    return Course.objects.create(
        name="Calculus and Vectors",
        grade_level=GRADE_LEVEL_12,
        course_code="MCV4U",
        category=COURSE_CATEGORY_MATH,
        capacity_min=10,
        capacity_max=30,
    )


@pytest.fixture
def second_course():
    return Course.objects.create(
        name="Advanced Functions",
        grade_level=GRADE_LEVEL_12,
        course_code="MHF4U",
        category=COURSE_CATEGORY_MATH,
        capacity_min=10,
        capacity_max=30,
    )


@pytest.fixture
def teacher():
    return Teacher.objects.create(
        first_name="Ada",
        last_name="Lovelace",
        email="ada@example.com",
        department="Mathematics",
    )


@pytest.fixture
def student(academic_year):
    return Student.objects.create(
        student_number="S1001",
        email="student@example.com",
        first_name="Grace",
        last_name="Hopper",
        date_of_birth="2009-01-01",
        grade_level=GRADE_LEVEL_12,
        academic_year=academic_year,
    )


@pytest.fixture
def section(course, academic_year, teacher):
    return Section.objects.create(
        course=course,
        section_number="01",
        academic_year=academic_year,
        semester=SEMESTER_FALL,
        teacher=teacher,
        capacity_min=10,
        capacity_max=30,
    )


@pytest.fixture
def timeslot(academic_year):
    return TimeSlot.objects.create(
        block=SCHEDULE_BLOCK_A,
        academic_year=academic_year,
        semester=SEMESTER_FALL,
    )


@pytest.fixture
def room():
    return Room.objects.create(
        name="Room 201",
        room_type=ROOM_TYPE_CLASSROOM,
        capacity=30,
    )


@pytest.mark.django_db
def test_duplicate_course_request_is_rejected(student, course, academic_year):
    CourseRequest.objects.create(
        student=student,
        course=course,
        academic_year=academic_year,
        request_type=COURSE_REQUEST_TYPE_PRIMARY,
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        CourseRequest.objects.create(
            student=student,
            course=course,
            academic_year=academic_year,
            request_type=COURSE_REQUEST_TYPE_ALTERNATE,
        )


@pytest.mark.django_db
def test_duplicate_enrollment_is_rejected(student, section):
    Enrollment.objects.create(student=student, section=section)

    with pytest.raises(IntegrityError), transaction.atomic():
        Enrollment.objects.create(student=student, section=section)


@pytest.mark.django_db
def test_duplicate_section_per_course_year_is_rejected(course, academic_year):
    Section.objects.create(
        course=course,
        section_number="01",
        academic_year=academic_year,
        semester=SEMESTER_FALL,
        capacity_min=10,
        capacity_max=30,
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        Section.objects.create(
            course=course,
            section_number="01",
            academic_year=academic_year,
            semester=SEMESTER_WINTER,
            capacity_min=10,
            capacity_max=30,
        )


@pytest.mark.django_db
def test_duplicate_teacher_qualification_is_rejected(teacher):
    qualification = Qualification.objects.create(name="Mathematics")
    TeacherQualification.objects.create(teacher=teacher, qualification=qualification)

    with pytest.raises(IntegrityError), transaction.atomic():
        TeacherQualification.objects.create(teacher=teacher, qualification=qualification)


@pytest.mark.django_db
def test_duplicate_teacher_availability_is_rejected(teacher, timeslot):
    TeacherAvailability.objects.create(teacher=teacher, timeslot=timeslot)

    with pytest.raises(IntegrityError), transaction.atomic():
        TeacherAvailability.objects.create(teacher=teacher, timeslot=timeslot)


@pytest.mark.django_db
def test_duplicate_teacher_course_preference_is_rejected(teacher, course):
    TeacherCoursePreference.objects.create(teacher=teacher, course=course)

    with pytest.raises(IntegrityError), transaction.atomic():
        TeacherCoursePreference.objects.create(teacher=teacher, course=course)


@pytest.mark.django_db
def test_duplicate_teacher_current_course_is_rejected(teacher, course, academic_year):
    TeacherCurrentCourse.objects.create(
        teacher=teacher,
        course=course,
        academic_year=academic_year,
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        TeacherCurrentCourse.objects.create(
            teacher=teacher,
            course=course,
            academic_year=academic_year,
        )


@pytest.mark.django_db
def test_duplicate_course_prerequisite_is_rejected(course, second_course):
    CoursePrerequisite.objects.create(course=course, prerequisite=second_course)

    with pytest.raises(IntegrityError), transaction.atomic():
        CoursePrerequisite.objects.create(course=course, prerequisite=second_course)


@pytest.mark.django_db
def test_duplicate_historical_course_demand_is_rejected(course, academic_year):
    HistoricalCourseDemand.objects.create(
        course=course,
        academic_year=academic_year,
        requests=70,
        final_enrollment=50,
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        HistoricalCourseDemand.objects.create(
            course=course,
            academic_year=academic_year,
            requests=72,
            final_enrollment=51,
        )


@pytest.mark.django_db
def test_duplicate_course_room_requirement_is_rejected(course):
    CourseRoomRequirement.objects.create(course=course, room_type=ROOM_TYPE_SCIENCE_LAB)

    with pytest.raises(IntegrityError), transaction.atomic():
        CourseRoomRequirement.objects.create(course=course, room_type=ROOM_TYPE_SCIENCE_LAB)


@pytest.mark.django_db
def test_duplicate_course_qualification_requirement_is_rejected(course):
    qualification = Qualification.objects.create(name="Mathematics")
    CourseQualificationRequirement.objects.create(
        course=course,
        qualification=qualification,
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        CourseQualificationRequirement.objects.create(
            course=course,
            qualification=qualification,
        )


@pytest.mark.django_db
def test_minimum_capacity_validators_are_enforced():
    course = Course(
        name="Invalid Capacity Course",
        grade_level=GRADE_LEVEL_12,
        course_code="BAD4U",
        category=COURSE_CATEGORY_MATH,
        capacity_min=0,
        capacity_max=30,
    )
    room = Room(name="Invalid Room", room_type=ROOM_TYPE_CLASSROOM, capacity=0)

    with pytest.raises(ValidationError):
        course.full_clean()

    with pytest.raises(ValidationError):
        room.full_clean()


@pytest.mark.django_db
def test_set_null_relationships_preserve_schedule_records(section, timeslot, room):
    schedule = SectionSchedule.objects.create(
        section=section,
        timeslot=timeslot,
        room=room,
    )
    lock = SectionLock.objects.create(
        section=section,
        locked_teacher=section.teacher,
        locked_timeslot=timeslot,
        locked_room=room,
    )

    section.teacher.delete()
    timeslot.delete()
    room.delete()

    schedule.refresh_from_db()
    lock.refresh_from_db()
    section.refresh_from_db()

    assert section.teacher is None
    assert schedule.timeslot is None
    assert schedule.room is None
    assert lock.locked_teacher is None
    assert lock.locked_timeslot is None
    assert lock.locked_room is None


@pytest.mark.django_db
def test_cascade_relationships_remove_dependent_records(student, section, academic_year, course):
    Enrollment.objects.create(student=student, section=section)
    CourseRequest.objects.create(
        student=student,
        course=course,
        academic_year=academic_year,
        request_type=COURSE_REQUEST_TYPE_PRIMARY,
    )
    SectionSchedule.objects.create(section=section)
    SectionLock.objects.create(section=section)
    ManualOverride.objects.create(section=section, action="move_section")

    student.delete()

    assert Enrollment.objects.count() == 0
    assert CourseRequest.objects.count() == 0

    section.delete()

    assert SectionSchedule.objects.count() == 0
    assert SectionLock.objects.count() == 0
    assert ManualOverride.objects.count() == 0
