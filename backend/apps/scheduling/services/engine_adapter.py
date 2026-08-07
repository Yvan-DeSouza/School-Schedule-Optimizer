"""The sole Django boundary for loading data into the pure scheduling engine."""

from django.db.models import Q

from scheduling_engine.demand_analyzer import parse_academic_year_start
from scheduling_engine.dto import (
    AcademicYearDTO, CounselorConstraintPreferenceDTO, CourseConflictDTO, CourseDTO,
    CoursePrerequisiteDTO, CourseQualificationRequirementDTO, CourseRequestDTO,
    CourseRoomRequirementDTO, HardConstraintDTO, HistoricalDemandDTO, QualificationDTO,
    RoomDTO, SchedulingInputDTO, SectionDTO, SectionLockDTO, SoftConstraintDTO,
    StudentDTO, TeacherAvailabilityDTO, TeacherCoursePreferenceDTO, TeacherCurrentCourseDTO,
    TeacherDTO, TeacherQualificationDTO, TimeSlotDTO,
)
from scheduling_engine.section_estimator import estimate_section_counts

from backend.apps.common.constants import (
    COURSE_REQUEST_TYPE_PRIMARY,
    QUALIFICATION_ENFORCEMENT_REQUIRED,
    STATUTORY_TEACHABLE_MIN_GRADE,
)
from backend.apps.common.models import AcademicYear, HistoricalCourseDemand, Room
from backend.apps.constraints.models import (
    CounselorConstraintPreference, CourseConflict, CourseQualificationRequirement,
    CourseRoomRequirement, HardConstraint, Qualification, SoftConstraint,
    TeacherAvailability, TeacherCoursePreference, TeacherCurrentCourse, TeacherQualification,
)
from backend.apps.control.models import SectionLock
from backend.apps.courses.models import Course, CoursePrerequisite, CourseRequest, Section
from backend.apps.people.models import Student, Teacher
from backend.apps.scheduling.models import TimeSlot


def load_scheduling_input(academic_year_id):
    """Load one planning year's ORM data into framework-independent DTOs."""
    target_year = AcademicYear.objects.get(pk=academic_year_id)
    target_start_year = parse_academic_year_start(target_year.name)
    academic_years = tuple(
        AcademicYearDTO(id=year.id, name=year.name)
        for year in AcademicYear.objects.order_by("name")
    )
    historical_year_ids = [
        year.id
        for year in AcademicYear.objects.exclude(pk=target_year.pk)
        if parse_academic_year_start(year.name) < target_start_year
    ]
    request_student_ids = CourseRequest.objects.filter(academic_year_id=academic_year_id).values_list("student_id", flat=True)

    return SchedulingInputDTO(
        academic_year_id=academic_year_id,
        academic_years=academic_years,
        courses=tuple(
            CourseDTO(
                course.id,
                course.course_code,
                course.name,
                course.capacity_min,
                course.capacity_max,
                course.grade_level,
                course.category,
                course.is_online,
                course.grade_level >= STATUTORY_TEACHABLE_MIN_GRADE,
            )
            for course in Course.objects.all()
        ),
        course_requests=tuple(
            CourseRequestDTO(
                request.student_id,
                request.course_id,
                request.request_type == COURSE_REQUEST_TYPE_PRIMARY,
                request.is_mandatory,
            )
            for request in CourseRequest.objects.filter(academic_year_id=academic_year_id)
        ),
        historical_demand=tuple(
            HistoricalDemandDTO(record.course_id, record.requests, record.final_enrollment, record.academic_year_id)
            for record in HistoricalCourseDemand.objects.filter(academic_year_id__in=historical_year_ids)
        ),
        sections=tuple(
            SectionDTO(section.id, section.course_id, section.academic_year_id, section.semester, section.capacity_min, section.capacity_max, section.teacher_id, section.is_locked)
            for section in Section.objects.filter(academic_year_id=academic_year_id)
        ),
        students=tuple(
            StudentDTO(student.id, student.grade_level)
            for student in Student.objects.filter(Q(academic_year_id=academic_year_id) | Q(id__in=request_student_ids)).distinct()
        ),
        teachers=tuple(
            TeacherDTO(teacher.id, teacher.max_courses_per_semester, teacher.max_courses_total, teacher.seniority, teacher.reduced_load)
            for teacher in Teacher.objects.all()
        ),
        rooms=tuple(RoomDTO(room.id, room.room_type, room.capacity, room.is_specialized) for room in Room.objects.all()),
        timeslots=tuple(
            TimeSlotDTO(slot.id, slot.academic_year_id, slot.semester, slot.block, slot.is_available)
            for slot in TimeSlot.objects.filter(academic_year_id=academic_year_id)
        ),
        qualifications=tuple(
            QualificationDTO(
                item.id,
                item.name,
                item.code,
                item.kind,
                item.subject_code,
                item.division,
            )
            for item in Qualification.objects.all()
        ),
        teacher_qualifications=tuple(TeacherQualificationDTO(item.teacher_id, item.qualification_id) for item in TeacherQualification.objects.all()),
        teacher_preferences=tuple(TeacherCoursePreferenceDTO(item.teacher_id, item.course_id) for item in TeacherCoursePreference.objects.all()),
        teacher_current_courses=tuple(
            TeacherCurrentCourseDTO(item.teacher_id, item.course_id, item.academic_year_id)
            for item in TeacherCurrentCourse.objects.filter(academic_year_id=academic_year_id)
        ),
        teacher_availability=tuple(
            TeacherAvailabilityDTO(item.teacher_id, item.timeslot_id, item.is_available)
            for item in TeacherAvailability.objects.filter(timeslot__academic_year_id=academic_year_id)
        ),
        course_room_requirements=tuple(CourseRoomRequirementDTO(item.course_id, item.room_type) for item in CourseRoomRequirement.objects.all()),
        course_qualification_requirements=tuple(
            CourseQualificationRequirementDTO(
                item.course_id,
                item.qualification_id,
                item.enforcement == QUALIFICATION_ENFORCEMENT_REQUIRED,
            )
            for item in CourseQualificationRequirement.objects.all()
        ),
        course_prerequisites=tuple(CoursePrerequisiteDTO(item.course_id, item.prerequisite_id) for item in CoursePrerequisite.objects.all()),
        course_conflicts=tuple(CourseConflictDTO(item.course_a_id, item.course_b_id, item.weight) for item in CourseConflict.objects.all()),
        section_locks=tuple(
            SectionLockDTO(item.section_id, item.locked_teacher_id, item.locked_timeslot_id, item.locked_room_id)
            for item in SectionLock.objects.filter(section__academic_year_id=academic_year_id)
        ),
        hard_constraints=tuple(HardConstraintDTO(item.id, item.name, item.type, item.priority) for item in HardConstraint.objects.all()),
        soft_constraints=tuple(SoftConstraintDTO(item.id, item.name, item.category, item.default_weight) for item in SoftConstraint.objects.all()),
        counselor_constraint_preferences=tuple(
            CounselorConstraintPreferenceDTO(item.counselor_id, item.constraint_id, item.weight)
            for item in CounselorConstraintPreference.objects.all()
        ),
    )


def get_section_count_recommendations(academic_year_id):
    return estimate_section_counts(load_scheduling_input(academic_year_id))
