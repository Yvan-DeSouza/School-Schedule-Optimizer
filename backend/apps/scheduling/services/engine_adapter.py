"""The sole Django boundary for loading data into the pure scheduling engine.

This module is allowed to know both ORM models and ``scheduling_engine`` DTOs.
No other Django module should import the engine directly, and no engine module
may import Django.  Centralizing translation here makes filtering, defaults,
qualification enforcement, and snapshot semantics reviewable in one place.

The adapter reads a coherent target-year view but does not persist solver
results.  Planning-run persistence belongs to ``section_planning.py``; later
solver write-back should likewise remain an explicit transactional service.
"""

from dataclasses import asdict

from django.db.models import Q

from scheduling_engine.demand_analyzer import parse_academic_year_start
from scheduling_engine.dto import (
    AcademicYearDTO, CounselorConstraintPreferenceDTO, CourseConflictDTO, CourseDTO,
    CoursePrerequisiteDTO, CourseQualificationRequirementDTO, CourseRequestDTO,
    CourseRoomRequirementDTO, HardConstraintDTO, HistoricalDemandDTO, QualificationDTO,
    RoomDTO, SchedulingInputDTO, SectionDTO, SectionLockDTO, SoftConstraintDTO,
    StudentDTO, TeacherAvailabilityDTO, TeacherCoursePreferenceDTO, TeacherCurrentCourseDTO,
    TeacherDTO, TeacherPlanningCapacityDTO, TeacherQualificationDTO, TimeSlotDTO,
)
from scheduling_engine.section_estimator import estimate_section_counts
from scheduling_engine.section_planner import plan_section_counts

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
from backend.apps.scheduling.models import TeacherPlanningCapacity, TimeSlot


def load_scheduling_input(academic_year_id):
    """Load one planning year's ORM data into framework-independent DTOs.

    Querysets are deliberately evaluated into tuples before returning.  The
    engine therefore receives a detached immutable snapshot rather than lazy ORM
    queries whose values could change during a solve.
    """

    academic_year_id = int(academic_year_id)
    # Fetching the target object early gives callers a normal DoesNotExist error
    # and supplies the name required for chronological history filtering.
    target_year = AcademicYear.objects.get(pk=academic_year_id)
    target_start_year = parse_academic_year_start(target_year.name)
    # Include all year identities because historical records refer to them by ID.
    academic_years = tuple(
        AcademicYearDTO(id=year.id, name=year.name)
        for year in AcademicYear.objects.order_by("name")
    )
    # Never use future or same-year outcomes to forecast the target year.  String
    # parsing is centralized in the pure demand module for consistent ordering.
    historical_year_ids = [
        year.id
        for year in AcademicYear.objects.exclude(pk=target_year.pk)
        if parse_academic_year_start(year.name) < target_start_year
    ]
    # Some imported request rows may reference students whose profile year is not
    # yet updated.  Include both target-year students and every request owner.
    request_student_ids = CourseRequest.objects.filter(academic_year_id=academic_year_id).values_list("student_id", flat=True)

    # A locked teacher takes precedence over Section.teacher when calculating
    # already-committed capacity, matching the manual-override contract.
    locked_teacher_by_section = {
        item.section_id: item.locked_teacher_id
        for item in SectionLock.objects.filter(section__academic_year_id=academic_year_id, locked_teacher__isnull=False)
    }
    committed_by_teacher_semester = {}
    for section in Section.objects.filter(academic_year_id=academic_year_id).only("id", "teacher_id", "semester"):
        teacher_id = locked_teacher_by_section.get(section.id, section.teacher_id)
        if teacher_id:
            # Existing assigned/locked sections consume one load slot.  They are
            # context, not newly planned work.
            key = (teacher_id, section.semester)
            committed_by_teacher_semester[key] = committed_by_teacher_semester.get(key, 0) + 1
    # Explicit planning capacities override the teacher profile defaults for one
    # teacher/year/semester only.
    configured_capacities = {
        (item.teacher_id, item.semester): item
        for item in TeacherPlanningCapacity.objects.filter(academic_year_id=academic_year_id)
    }
    # All teachers remain in the DTO even when current capacity is zero so
    # eligibility and what-if adjustments can report stable teacher IDs.
    teachers = tuple(Teacher.objects.all())

    return SchedulingInputDTO(
        academic_year_id=academic_year_id,
        academic_years=academic_years,
        # CourseDTO carries legacy min/max for the old endpoint and the normalized
        # five-point policy for the CP-SAT planner.  New planning uses the latter.
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
                # Legal qualification enforcement is derived from the canonical
                # grade threshold once at the boundary.
                course.grade_level >= STATUTORY_TEACHABLE_MIN_GRADE,
                course.capacity_profile_id,
                course.capacity_profile.hard_min,
                course.capacity_profile.soft_min,
                course.capacity_profile.target,
                course.capacity_profile.soft_max,
                course.capacity_profile.hard_max,
                course.allowed_semester,
                course.priority_profile.tier,
                course.priority_profile_id,
            )
            for course in Course.objects.select_related("capacity_profile", "priority_profile")
        ),
        # Translate canonical request-type strings into an engine-neutral bool;
        # mandatory remains provenance and never determines priority tiers.
        course_requests=tuple(
            CourseRequestDTO(
                request.student_id,
                request.course_id,
                request.request_type == COURSE_REQUEST_TYPE_PRIMARY,
                request.is_mandatory,
            )
            for request in CourseRequest.objects.filter(academic_year_id=academic_year_id)
        ),
        # Only strictly earlier years selected above contribute conversion data.
        historical_demand=tuple(
            HistoricalDemandDTO(record.course_id, record.requests, record.final_enrollment, record.academic_year_id)
            for record in HistoricalCourseDemand.objects.filter(academic_year_id__in=historical_year_ids)
        ),
        # Existing sections are loaded for capacity/lock context.  Planning runs
        # never turn these ORM instances into decision variables directly.
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
            for teacher in teachers
        ),
        # Produce an explicit two-semester record for every teacher.  This keeps
        # fallback logic out of the engine and snapshots the exact effective
        # maximum/reserved values used by the run.
        teacher_planning_capacities=tuple(
            TeacherPlanningCapacityDTO(
                teacher.id,
                semester,
                configured_capacities.get((teacher.id, semester)).maximum_sections if (teacher.id, semester) in configured_capacities else teacher.max_courses_per_semester,
                # Administrator reservations and already committed sections both
                # reduce the remaining planning capacity.
                (configured_capacities.get((teacher.id, semester)).reserved_sections if (teacher.id, semester) in configured_capacities else 0)
                + committed_by_teacher_semester.get((teacher.id, semester), 0),
            )
            for teacher in teachers for semester in (1, 2)
        ),
        rooms=tuple(RoomDTO(room.id, room.room_type, room.capacity, room.is_specialized) for room in Room.objects.all()),
        timeslots=tuple(
            TimeSlotDTO(slot.id, slot.academic_year_id, slot.semester, slot.block, slot.is_available)
            for slot in TimeSlot.objects.filter(academic_year_id=academic_year_id)
        ),
        # Pass normalized credential fields only.  Aspen raw text and provenance
        # are intentionally irrelevant to solver matching.
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
                # Convert the canonical enforcement choice into a pure Boolean
                # required/preferred distinction.
                item.enforcement == QUALIFICATION_ENFORCEMENT_REQUIRED,
            )
            for item in CourseQualificationRequirement.objects.all()
        ),
        course_prerequisites=tuple(CoursePrerequisiteDTO(item.course_id, item.prerequisite_id) for item in CoursePrerequisite.objects.all()),
        course_conflicts=tuple(CourseConflictDTO(item.course_a_id, item.course_b_id, item.weight) for item in CourseConflict.objects.all()),
        # Locks are scoped to the target year; unrelated historical locks must
        # never constrain the current solve.
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
    """Serve the legacy heuristic endpoint through the same DTO boundary."""

    return estimate_section_counts(load_scheduling_input(academic_year_id))


def get_section_count_plan(academic_year_id, *, course_constraints=(), teacher_capacity_adjustments=()):
    """Load ORM data then invoke the pure CP-SAT planner."""
    # Keep this convenience function for callers that do not need audit input.
    result, _ = get_section_count_plan_with_snapshot(
        academic_year_id,
        course_constraints=course_constraints,
        teacher_capacity_adjustments=teacher_capacity_adjustments,
    )
    return result


def get_section_count_plan_with_snapshot(academic_year_id, *, course_constraints=(), teacher_capacity_adjustments=()):
    """Run the engine and snapshot precisely the same immutable input."""
    # Load exactly once: a second load after solving could observe edits and make
    # the audit snapshot disagree with the result it claims to explain.
    data = load_scheduling_input(academic_year_id)
    return plan_section_counts(
        data,
        course_constraints=course_constraints,
        teacher_capacity_adjustments=teacher_capacity_adjustments,
    ), asdict(data)


def get_section_planning_snapshot(academic_year_id):
    """Return a JSON-ready immutable engine input snapshot for an audit run."""
    # ``asdict`` recursively removes dataclass instances so JSONField can store
    # the snapshot without any engine-specific encoder.
    return asdict(load_scheduling_input(academic_year_id))
