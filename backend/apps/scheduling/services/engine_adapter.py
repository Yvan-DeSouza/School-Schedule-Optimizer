"""The sole Django boundary for loading data into the pure scheduling engine.

This module is allowed to know both ORM models and ``scheduling_engine`` DTOs.
Other scheduling services may call pure engine entrypoints using DTO snapshots
loaded here, but non-scheduling Django apps should not import the engine
directly. No engine module may import Django. Centralizing translation here
makes filtering, defaults, qualification enforcement, and snapshot semantics
reviewable in one place.

The adapter reads a coherent target-year view but does not persist solver
results.  Planning-run persistence belongs to ``section_planning.py``; later
solver write-back should likewise remain an explicit transactional service.
"""

from dataclasses import asdict, replace
from collections import defaultdict
from hashlib import sha256
import json

from django.db.models import Q

from scheduling_engine.demand_analyzer import parse_academic_year_start
from scheduling_engine.student_assignment.objective_semantics import (
    OBJECTIVE_SEMANTICS_V1,
    OBJECTIVE_SEMANTICS_V2,
    OBJECTIVE_SEMANTICS_VERSIONS,
    resolve_importance_scores,
)
from scheduling_engine.dto import (
    AcademicYearDTO, CounselorConstraintPreferenceDTO, CourseConflictDTO, CourseDTO,
    CoursePrerequisiteDTO, CourseQualificationRequirementDTO, CourseRequestDTO,
    CourseRoomRequirementDTO, HardConstraintDTO, HistoricalDemandDTO, QualificationDTO,
    RoomDTO, SchedulingInputDTO, SectionDTO, SectionLockDTO, SoftConstraintDTO,
    StudentDTO, TeacherAvailabilityDTO, TeacherCoursePreferenceDTO, TeacherCurrentCourseDTO,
    TeacherDTO, TeacherPlanningCapacityDTO, TeacherQualificationDTO, TimeSlotDTO,
    PlanningOfferingDTO,
    FixedPlacementDTO, PlacementConflictDTO, PlacementInputDTO,
    PlacementTeacherDTO, PlacementUnitDTO, OnlineSupervisionPlacementSessionDTO,
    OnlineSupervisionDemandDTO, PlacementStudentTimetableDemandDTO,
    FixedTeacherAssignmentDTO, TeacherAssignmentInputDTO,
    TeacherAssignmentSectionDTO, TeacherAssignmentTeacherDTO,
    TeacherCourseAssignmentRuleDTO,
    CourseSequencePreferenceDTO, CourseDifficultyDTO, CourseCategoryRelationshipDTO,
    FixedEnrollmentDTO,
    StudentAssignmentInputDTO, StudentAssignmentRequestDTO,
    StudentAssignmentSectionDTO, StudentAssignmentLockDTO,
    StudentAssignmentScopeDTO,
    OnlineSupervisionSessionDTO, StudentScheduleCommitmentRequestDTO,
    StudentSpecialCommitmentLockDTO, FixedStudentScheduleCommitmentDTO,
)
from scheduling_engine.constraint_compiler import compile_constraints
from scheduling_engine.section_estimator import estimate_section_counts
from scheduling_engine.section_planner import plan_section_counts

from backend.apps.common.constants import (
    COURSE_REQUEST_TYPE_PRIMARY,
    COURSE_REQUEST_TYPE_ALTERNATE,
    DELIVERY_GROUP_STATUS_ACTIVE,
    COURSE_OFFERING_STATUS_OFFERED,
    QUALIFICATION_ENFORCEMENT_REQUIRED,
    QUALIFICATION_REVIEW_VERIFIED,
    SECTION_LIFECYCLE_ACTIVE,
    STATUTORY_TEACHABLE_MIN_GRADE,
)
from backend.apps.common.models import AcademicYear, HistoricalCourseDemand, Room
from backend.apps.constraints.models import (
    CounselorConstraintPreference, CourseConflict, CourseQualificationRequirement,
    CourseRoomRequirement, HardConstraint, Qualification, SoftConstraint,
    TeacherAvailability, TeacherCoursePreference, TeacherCurrentCourse, TeacherQualification,
)
from backend.apps.control.models import SectionLock
from backend.apps.courses.models import (
    Course,
    CourseCategoryRelationship,
    StudentCourseHistoricalResult,
    CourseOffering,
    CoursePrerequisite,
    CourseSequencePreference,
    CourseRequest,
    DeliveryGroup,
    Enrollment,
    HalfSemesterCoursePair,
    HalfSemesterSectionPair,
    Section,
)
from backend.apps.courses.constants import (
    COURSE_DELIVERY_KIND_NORMAL_INSTRUCTION,
    COURSE_DURATION_FULL_SEMESTER,
    COURSE_DURATION_HALF_SEMESTER,
    ENROLLMENT_LIFECYCLE_ACTIVE,
)
from backend.apps.courses.services.difficulty import course_difficulty_facts
from backend.apps.courses.selectors import (
    active_delivery_groups_for_year,
    active_sections_for_year,
)
from backend.apps.people.models import Student, Teacher
from backend.apps.scheduling.models import (
    TeacherPlanningCapacity,
    TeacherPlanningAnnualCapacity,
    TeacherPlanningRoster,
    TeacherAssignmentRun,
    StudentAssignmentLock,
    TimeSlot,
    OnlineSupervisionSession,
    OnlineEnrollment,
    StudentScheduleCommitment,
    StudentSpecialCommitmentLock,
)
from backend.apps.courses.models import StudentScheduleCommitmentRequest


def _delivery_group_allowed_semester(group):
    """Collapse member restrictions to the legal shared-semester intersection."""

    from backend.apps.courses.services.offerings import combined_allowed_semester

    return combined_allowed_semester([
        offering.course for offering in group.offerings.all()
    ])


def _first_member_course_id(section):
    if not section.delivery_group_id:
        return section.course_id
    return section.delivery_group.offerings.order_by(
        "course__course_code", "course_id"
    ).values_list("course_id", flat=True).first()


def load_scheduling_input(academic_year_id, *, require_ready_roster=False):
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
        for item in SectionLock.objects.filter(
            section__academic_year_id=academic_year_id,
            section__lifecycle_status=SECTION_LIFECYCLE_ACTIVE,
            locked_teacher__isnull=False,
        )
    }
    committed_by_teacher_semester = {}
    for section in active_sections_for_year(academic_year_id).only(
        "id",
        "teacher_id",
        "semester",
    ):
        teacher_id = locked_teacher_by_section.get(section.id, section.teacher_id)
        if teacher_id:
            # Existing assigned/locked sections consume one load slot.  They are
            # context, not newly planned work.
            key = (teacher_id, section.semester)
            committed_by_teacher_semester[key] = committed_by_teacher_semester.get(key, 0) + 1
    # An online supervisor is not course-qualified instruction, but it is an
    # ordinary teacher workload and availability commitment. Include accepted
    # supervisors in every upstream capacity view so no later planner can use
    # the same person for a simultaneous normal section.
    from backend.apps.scheduling.models import OnlineSupervisionSession

    for session in OnlineSupervisionSession.objects.filter(
        academic_year_id=academic_year_id,
        lifecycle_status="active",
        supervisor__isnull=False,
        timeslot__isnull=False,
    ).select_related("timeslot"):
        key = (session.supervisor_id, session.timeslot.semester)
        committed_by_teacher_semester[key] = committed_by_teacher_semester.get(key, 0) + 1
    # Explicit planning capacities override the teacher profile defaults for one
    # teacher/year/semester only.
    configured_capacities = {
        (item.teacher_id, item.semester): item
        for item in TeacherPlanningCapacity.objects.filter(academic_year_id=academic_year_id)
    }
    if require_ready_roster:
        try:
            roster = TeacherPlanningRoster.objects.prefetch_related(
                "members__teacher"
            ).get(academic_year=target_year)
        except TeacherPlanningRoster.DoesNotExist as error:
            raise ValueError(
                "Create and confirm a teacher planning roster before running staffing."
            ) from error
        if roster.status != "ready":
            raise ValueError(
                "The teacher planning roster is still draft. Confirm it before running staffing."
            )
        teachers = tuple(
            member.teacher
            for member in roster.members.select_related("teacher").order_by(
                "teacher__last_name", "teacher__first_name", "teacher_id"
            )
            if not member.teacher.is_archived
        )
        missing_capacity = [
            {"teacher_id": teacher.id, "semester": semester}
            for teacher in teachers
            for semester in (1, 2)
            if (teacher.id, semester) not in configured_capacities
        ]
        if missing_capacity:
            raise ValueError(
                "Every ready-roster teacher needs explicit Semester 1 and Semester 2 capacity rows, including zero-capacity rows."
            )
    else:
        # Legacy read/analysis callers may load the active teacher directory
        # without a staffing-readiness checkpoint. New staffing runs always set
        # require_ready_roster=True.
        teachers = tuple(Teacher.objects.filter(is_archived=False))
    teacher_ids = {teacher.id for teacher in teachers}
    context_teacher_ids = {
        teacher_id for teacher_id, _semester in committed_by_teacher_semester
    }
    unrostered_context = sorted(context_teacher_ids - teacher_ids)
    if require_ready_roster and unrostered_context:
        raise ValueError(
            "Active assigned or teacher-locked sections reference teachers outside "
            f"the ready roster: {unrostered_context}."
        )

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
                delivery_kind=course.delivery_kind,
                duration=course.duration,
                credit_value=float(course.credit_value),
            )
            for course in Course.objects.select_related("capacity_profile", "priority_profile")
        ),
        planning_offerings=tuple(
            PlanningOfferingDTO(
                group.id,
                tuple(offering.course_id for offering in group.offerings.all()),
                tuple(offering.course.course_code for offering in group.offerings.all()),
                group.capacity_profile_id,
                group.capacity_profile.hard_min,
                group.capacity_profile.soft_min,
                group.capacity_profile.target,
                group.capacity_profile.soft_max,
                group.capacity_profile.hard_max,
                _delivery_group_allowed_semester(group),
                min(
                    offering.course.priority_profile.tier
                    for offering in group.offerings.all()
                ),
                len(group.offerings.all()) > 1,
            )
            for group in active_delivery_groups_for_year(academic_year_id)
            .select_related("capacity_profile")
            .prefetch_related(
                "offerings__course__priority_profile"
            )
            .order_by("name", "id")
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
            SectionDTO(
                section.id,
                section.course_id or _first_member_course_id(section),
                section.academic_year_id,
                section.semester,
                section.capacity_min,
                section.capacity_max,
                section.teacher_id,
                section.is_locked,
                section.delivery_group_id or 0,
                tuple(
                    section.delivery_group.offerings.values_list("course_id", flat=True)
                    if section.delivery_group_id
                    else ([section.course_id] if section.course_id else [])
                ),
            )
            for section in active_sections_for_year(
                academic_year_id,
                Section.objects.select_related("delivery_group"),
            )
        ),
        students=tuple(
            StudentDTO(student.id, student.grade_level)
            for student in Student.objects.filter(Q(academic_year_id=academic_year_id) | Q(id__in=request_student_ids)).distinct()
        ),
        teachers=tuple(
            TeacherDTO(teacher.id, teacher.max_courses_per_semester, teacher.max_courses_total, teacher.seniority, teacher.is_reduced_load)
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
        teacher_qualifications=tuple(
            TeacherQualificationDTO(item.teacher_id, item.qualification_id)
            for item in TeacherQualification.objects.filter(
                teacher_id__in=teacher_ids,
                review_status=QUALIFICATION_REVIEW_VERIFIED,
            )
        ),
        teacher_preferences=tuple(
            TeacherCoursePreferenceDTO(item.teacher_id, item.course_id)
            for item in TeacherCoursePreference.objects.filter(teacher_id__in=teacher_ids)
        ),
        teacher_current_courses=tuple(
            TeacherCurrentCourseDTO(item.teacher_id, item.course_id, item.academic_year_id)
            for item in TeacherCurrentCourse.objects.filter(
                academic_year_id=academic_year_id,
                teacher_id__in=teacher_ids,
            )
        ),
        teacher_availability=tuple(
            TeacherAvailabilityDTO(item.teacher_id, item.timeslot_id, item.is_available)
            for item in TeacherAvailability.objects.filter(
                timeslot__academic_year_id=academic_year_id,
                teacher_id__in=teacher_ids,
            )
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
        # Legacy generic planning does not consume annual matrix rows. Loading
        # only pre-matrix edges prevents one course pair from appearing once per
        # year and violating the old compiler's unique-pair contract.
        course_conflicts=tuple(CourseConflictDTO(item.course_a_id, item.course_b_id, item.weight) for item in CourseConflict.objects.filter(matrix__isnull=True)),
        # Locks are scoped to the target year; unrelated historical locks must
        # never constrain the current solve.
        section_locks=tuple(
            SectionLockDTO(item.section_id, item.locked_teacher_id, item.locked_timeslot_id, item.locked_room_id)
            for item in SectionLock.objects.filter(
                section__academic_year_id=academic_year_id,
                section__lifecycle_status=SECTION_LIFECYCLE_ACTIVE,
            )
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
    normal_course_ids = {
        course.id for course in data.courses
        if course.delivery_kind == "normal_instruction"
    }
    # Section count deliberately owns normal instructional capacity only.
    # Online sessions have their distinct capacity-planning run and Co-op has
    # no local instructional section, so including either here would fabricate
    # a teacher/room demand that the policy explicitly excludes.
    data = replace(
        data,
        courses=tuple(course for course in data.courses if course.id in normal_course_ids),
        course_requests=tuple(
            request for request in data.course_requests if request.course_id in normal_course_ids
        ),
        course_prerequisites=tuple(
            edge for edge in data.course_prerequisites
            if edge.course_id in normal_course_ids and edge.prerequisite_id in normal_course_ids
        ),
        course_conflicts=tuple(
            conflict for conflict in data.course_conflicts
            if conflict.course_a_id in normal_course_ids and conflict.course_b_id in normal_course_ids
        ),
        course_qualification_requirements=tuple(
            item for item in data.course_qualification_requirements
            if item.course_id in normal_course_ids
        ),
        teacher_preferences=tuple(
            item for item in data.teacher_preferences if item.course_id in normal_course_ids
        ),
        teacher_current_courses=tuple(
            item for item in data.teacher_current_courses if item.course_id in normal_course_ids
        ),
    )
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


def placement_input_fingerprint(snapshot):
    """Return one deterministic stale-input fingerprint for placement reviews."""

    return sha256(json.dumps(snapshot, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _allowed_semester_ids(value):
    from backend.apps.courses.constants import (
        COURSE_ALLOWED_SEMESTER_1_ONLY,
        COURSE_ALLOWED_SEMESTER_2_ONLY,
    )

    if value == COURSE_ALLOWED_SEMESTER_1_ONLY:
        return (1,)
    if value == COURSE_ALLOWED_SEMESTER_2_ONLY:
        return (2,)
    return (1, 2)


def _group_allowed_semesters(group):
    values = [
        set(_allowed_semester_ids(offering.course.allowed_semester))
        for offering in group.offerings.all()
    ]
    return tuple(sorted(set.intersection(*values))) if values else ()


def load_teacher_assignment_input(*, academic_year_id):
    """Load accepted placement context into a detached named-assignment snapshot.

    The adapter subtracts only teacher assignments already fixed outside the
    decision set.  Locked-but-unassigned sections remain solver candidates, so
    their load is not accidentally removed twice before the model constrains it.
    """

    from backend.apps.control.models import SectionLock
    from backend.apps.courses.models import HalfSemesterSectionPair, Section
    from backend.apps.scheduling.models import (
        SectionSchedule, TeacherCourseAssignmentRule, TeacherPlanningAnnualCapacity,
        TeacherPlanningRoster, TeacherTimePreference, OnlineSupervisionSession,
    )

    academic_year_id = int(academic_year_id)
    base = load_scheduling_input(academic_year_id, require_ready_roster=True)
    compiled = compile_constraints(base)
    roster = TeacherPlanningRoster.objects.get(academic_year_id=academic_year_id)
    roster_teacher_ids = [teacher.id for teacher in base.teachers]
    timeslots = {item.id: item for item in base.timeslots}
    locks = {
        item.section_id: item
        for item in SectionLock.objects.filter(
            section__academic_year_id=academic_year_id,
            section__lifecycle_status=SECTION_LIFECYCLE_ACTIVE,
        ).select_related("locked_teacher")
    }
    schedules = {
        item.section_id: item
        for item in SectionSchedule.objects.filter(
            section__academic_year_id=academic_year_id,
            section__lifecycle_status=SECTION_LIFECYCLE_ACTIVE,
        ).select_related("timeslot")
    }
    sections = list(
        active_sections_for_year(academic_year_id)
        .select_related("course", "delivery_group", "teacher")
        .prefetch_related("delivery_group__offerings__course")
        .order_by("id")
    )
    paired_section_key = {}
    for pair in HalfSemesterSectionPair.objects.filter(
        first_section__academic_year_id=academic_year_id,
    ).order_by("id"):
        key = f"half_semester_pair:{pair.id}"
        paired_section_key[pair.first_section_id] = key
        paired_section_key[pair.second_section_id] = key
    section_dtos, fixed = [], []
    for section in sections:
        schedule = schedules.get(section.id)
        if schedule is None or schedule.timeslot_id is None:
            raise ValueError(
                f"Section {section.id} has no accepted semester/A-D placement and cannot enter teacher assignment."
            )
        if schedule.timeslot_id not in timeslots:
            raise ValueError(f"Section {section.id} references a timeslot outside the target year.")
        if section.delivery_group_id:
            member_course_ids = tuple(
                offering.course_id for offering in section.delivery_group.offerings.all()
            )
        elif section.course_id:
            member_course_ids = (section.course_id,)
        else:
            raise ValueError(f"Section {section.id} has no course delivery identity.")
        lock = locks.get(section.id)
        # A pre-existing named teacher is accepted fixed context.  A teacher lock
        # without Section.teacher is still a decision candidate, forced to that
        # named teacher by the pure model and written only on approval.
        is_fixed = section.teacher_id is not None
        section_dtos.append(TeacherAssignmentSectionDTO(
            section_id=section.id,
            delivery_group_id=section.delivery_group_id or -section.course_id,
            member_course_ids=member_course_ids,
            semester=section.semester,
            timeslot_id=schedule.timeslot_id,
            locked_teacher_id=lock.locked_teacher_id if lock else None,
            is_fixed=is_fixed,
            assigned_teacher_id=section.teacher_id,
            shared_staffing_key=paired_section_key.get(section.id),
        ))
        if is_fixed:
            fixed.append(FixedTeacherAssignmentDTO(
                section_id=section.id, teacher_id=section.teacher_id,
                semester=section.semester, timeslot_id=schedule.timeslot_id,
                member_course_ids=member_course_ids,
            ))

    online_sessions = list(OnlineSupervisionSession.objects.filter(
        academic_year_id=academic_year_id,
        lifecycle_status="active",
    ).select_related("timeslot", "supervisor").order_by("id"))
    for session in online_sessions:
        if session.timeslot_id is None or session.timeslot_id not in timeslots:
            raise ValueError(
                f"Online supervision session {session.id} has no accepted semester/A-D placement."
            )
        is_fixed = session.supervisor_id is not None
        section_dtos.append(TeacherAssignmentSectionDTO(
            section_id=None,
            delivery_group_id=-session.id,
            member_course_ids=(),
            semester=session.timeslot.semester,
            timeslot_id=session.timeslot_id,
            is_fixed=is_fixed,
            assigned_teacher_id=session.supervisor_id,
            is_online_supervision=True,
            online_supervision_session_id=session.id,
        ))
        if is_fixed:
            fixed.append(FixedTeacherAssignmentDTO(
                section_id=None,
                teacher_id=session.supervisor_id,
                semester=session.timeslot.semester,
                timeslot_id=session.timeslot_id,
                member_course_ids=(),
                online_supervision_session_id=session.id,
            ))

    configured_semester = {
        (item.teacher_id, item.semester): item
        for item in TeacherPlanningCapacity.objects.filter(
            academic_year_id=academic_year_id, teacher_id__in=roster_teacher_ids,
        )
    }
    # Accepted paired trimester sections retain two Section.teacher values in
    # the database but represent one recurring teaching workload. Collapse the
    # fixed-context witness here so a later staffing rerun does not subtract the
    # same teacher load twice; member courses remain unioned for course rules.
    fixed_by_shared_key = {}
    unpaired_fixed = []
    for item in fixed:
        pair_key = paired_section_key.get(item.section_id) if item.section_id is not None else None
        if pair_key is None:
            unpaired_fixed.append(item)
            continue
        previous = fixed_by_shared_key.get(pair_key)
        if previous is None:
            fixed_by_shared_key[pair_key] = item
        elif (
            previous.teacher_id != item.teacher_id
            or previous.timeslot_id != item.timeslot_id
            or previous.semester != item.semester
        ):
            raise ValueError(
                f"Accepted half-semester pair {pair_key} has inconsistent teacher or timing context."
            )
        else:
            fixed_by_shared_key[pair_key] = replace(
                previous,
                member_course_ids=tuple(sorted(set(previous.member_course_ids) | set(item.member_course_ids))),
            )
    fixed = unpaired_fixed + list(fixed_by_shared_key.values())
    configured_annual = {
        item.teacher_id: item
        for item in TeacherPlanningAnnualCapacity.objects.filter(
            academic_year_id=academic_year_id, teacher_id__in=roster_teacher_ids,
        )
    }
    missing = [
        teacher.id for teacher in base.teachers
        if teacher.id not in configured_annual
        or any((teacher.id, semester) not in configured_semester for semester in (1, 2))
    ]
    if missing:
        raise ValueError(f"Ready roster is missing annual or semester capacity for teacher IDs: {missing}.")
    fixed_load = defaultdict(int)
    for item in fixed:
        fixed_load[item.teacher_id, item.semester] += 1
        fixed_load[item.teacher_id, 0] += 1
    denied = defaultdict(set)
    for item in TeacherAvailability.objects.filter(
        teacher_id__in=roster_teacher_ids,
        timeslot__academic_year_id=academic_year_id,
        is_available=False,
    ):
        denied[item.teacher_id].add(item.timeslot_id)
    preferred_time, avoided_time = defaultdict(set), defaultdict(set)
    for item in TeacherTimePreference.objects.filter(
        academic_year_id=academic_year_id, teacher_id__in=roster_teacher_ids,
    ):
        (preferred_time if item.preference == "preferred" else avoided_time)[item.teacher_id].add(item.timeslot_id)

    target_year = AcademicYear.objects.get(pk=academic_year_id)
    target_start = parse_academic_year_start(target_year.name)
    previous_years = [
        year for year in AcademicYear.objects.exclude(pk=academic_year_id)
        if parse_academic_year_start(year.name) < target_start
    ]
    previous_year_id = max(previous_years, key=lambda year: parse_academic_year_start(year.name)).id if previous_years else None
    previous_courses = defaultdict(set)
    if previous_year_id:
        for item in TeacherCurrentCourse.objects.filter(
            teacher_id__in=roster_teacher_ids, academic_year_id=previous_year_id,
        ):
            previous_courses[item.teacher_id].add(item.course_id)

    teachers = []
    for teacher in base.teachers:
        annual = configured_annual[teacher.id]
        sem_1, sem_2 = configured_semester[teacher.id, 1], configured_semester[teacher.id, 2]
        teachers.append(TeacherAssignmentTeacherDTO(
            id=teacher.id,
            eligible_course_ids=tuple(sorted(
                course_id for course_id, teacher_ids in compiled.qualified_teacher_ids_by_course.items()
                if teacher.id in teacher_ids
            )),
            remaining_semester_1=max(0, sem_1.maximum_sections - sem_1.reserved_sections - fixed_load[teacher.id, 1]),
            remaining_semester_2=max(0, sem_2.maximum_sections - sem_2.reserved_sections - fixed_load[teacher.id, 2]),
            remaining_annual=max(0, annual.maximum_sections - annual.reserved_sections - fixed_load[teacher.id, 0]),
            unavailable_timeslot_ids=tuple(sorted(denied[teacher.id])),
            preferred_course_ids=tuple(sorted(
                item.course_id for item in TeacherCoursePreference.objects.filter(teacher_id=teacher.id)
            )),
            prior_year_course_ids=tuple(sorted(previous_courses[teacher.id])),
            preferred_timeslot_ids=tuple(sorted(preferred_time[teacher.id])),
            avoided_timeslot_ids=tuple(sorted(avoided_time[teacher.id])),
            seniority=teacher.seniority or 0,
        ))
    rules = tuple(
        TeacherCourseAssignmentRuleDTO(
            teacher_id=item.teacher_id, course_id=item.course_id,
            minimum_sections=item.minimum_sections, maximum_sections=item.maximum_sections,
        )
        for item in TeacherCourseAssignmentRule.objects.filter(
            academic_year_id=academic_year_id, teacher_id__in=roster_teacher_ids,
        ).order_by("teacher_id", "course_id")
    )
    return TeacherAssignmentInputDTO(
        academic_year_id=academic_year_id, sections=tuple(section_dtos),
        teachers=tuple(teachers), rules=rules, fixed_assignments=tuple(fixed),
    ), roster


def _has_directed_cycle(edges):
    """Return whether ``(earlier, later)`` edges form a directed cycle."""

    adjacency = defaultdict(set)
    for earlier, later in edges:
        adjacency[earlier].add(later)
    visiting, visited = set(), set()

    def visit(node):
        if node in visiting:
            return True
        if node in visited:
            return False
        visiting.add(node)
        cyclic = any(visit(next_node) for next_node in adjacency[node])
        visiting.remove(node)
        visited.add(node)
        return cyclic

    return any(visit(node) for node in list(adjacency))


def _student_assignment_staffing_context(
    *, academic_year_id, sections, online_sessions, staffing_mode, provisional_teacher_assignment_run
):
    """Validate and snapshot the selected counselor staffing-assumption mode."""

    from backend.apps.scheduling.constants import (
        STUDENT_ASSIGNMENT_STAFFING_MODE_FINAL_STAFFING,
        STUDENT_ASSIGNMENT_STAFFING_MODE_PARTIAL_STAFFING,
        STUDENT_ASSIGNMENT_STAFFING_MODE_PROVISIONAL_STAFFING,
        STUDENT_ASSIGNMENT_STAFFING_MODE_SECTIONS_ONLY,
    )

    if staffing_mode == STUDENT_ASSIGNMENT_STAFFING_MODE_SECTIONS_ONLY:
        if provisional_teacher_assignment_run is not None:
            raise ValueError("sections_only does not accept a provisional teacher-assignment run.")
        # Deliberately omit teacher facts so changes never stale Mode A.
        return {"staffing_mode": staffing_mode}
    if staffing_mode == STUDENT_ASSIGNMENT_STAFFING_MODE_PARTIAL_STAFFING:
        if provisional_teacher_assignment_run is not None:
            raise ValueError("partial_staffing does not accept a provisional teacher-assignment run.")
        return {
            "staffing_mode": staffing_mode,
            "section_teacher_ids": [
                {"section_id": section.id, "teacher_id": section.teacher_id}
                for section in sorted(sections, key=lambda item: item.id)
            ],
            "online_supervision_teacher_ids": [
                {"online_supervision_session_id": session.id, "teacher_id": session.supervisor_id}
                for session in sorted(online_sessions, key=lambda item: item.id)
            ],
        }
    if staffing_mode == STUDENT_ASSIGNMENT_STAFFING_MODE_FINAL_STAFFING:
        if provisional_teacher_assignment_run is not None:
            raise ValueError("final_staffing does not accept a provisional teacher-assignment run.")
        missing = [section.id for section in sections if section.teacher_id is None]
        missing_online = [session.id for session in online_sessions if session.supervisor_id is None]
        if missing or missing_online:
            raise ValueError(
                "final_staffing requires a final teacher on every active section and online supervision session: "
                f"sections={missing}, online_supervision_sessions={missing_online}."
            )
        return {
            "staffing_mode": staffing_mode,
            "section_teacher_ids": [
                {"section_id": section.id, "teacher_id": section.teacher_id}
                for section in sorted(sections, key=lambda item: item.id)
            ],
            "online_supervision_teacher_ids": [
                {"online_supervision_session_id": session.id, "teacher_id": session.supervisor_id}
                for session in sorted(online_sessions, key=lambda item: item.id)
            ],
        }
    if staffing_mode != STUDENT_ASSIGNMENT_STAFFING_MODE_PROVISIONAL_STAFFING:
        raise ValueError("Unknown student-assignment staffing mode.")
    if provisional_teacher_assignment_run is None:
        raise ValueError("provisional_staffing requires provisional_teacher_assignment_run.")
    if provisional_teacher_assignment_run.academic_year_id != int(academic_year_id):
        raise ValueError("The provisional teacher-assignment run belongs to another academic year.")
    if provisional_teacher_assignment_run.status != "complete" or provisional_teacher_assignment_run.result.get("status") != "complete":
        raise ValueError("The provisional teacher-assignment run must be complete.")
    if hasattr(provisional_teacher_assignment_run, "approval"):
        raise ValueError("A provisional teacher-assignment run cannot already be approved.")
    final_teacher_sections = [section.id for section in sections if section.teacher_id is not None]
    final_online_sessions = [session.id for session in online_sessions if session.supervisor_id is not None]
    if final_teacher_sections or final_online_sessions:
        raise ValueError(
            "provisional_staffing requires every active section and online supervision session to remain unstaffed: "
            f"sections={final_teacher_sections}, online_supervision_sessions={final_online_sessions}."
        )
    assignments = provisional_teacher_assignment_run.result.get("assignments", [])
    assignment_by_section = {
        int(item["section_id"]): int(item["teacher_id"])
        for item in assignments if item.get("section_id") is not None
    }
    assignment_by_online_session = {
        int(item["online_supervision_session_id"]): int(item["teacher_id"])
        for item in assignments if item.get("online_supervision_session_id") is not None
    }
    section_ids = {section.id for section in sections}
    online_session_ids = {session.id for session in online_sessions}
    if set(assignment_by_section) != section_ids or set(assignment_by_online_session) != online_session_ids:
        raise ValueError("The provisional teacher-assignment run does not cover exactly the active staffing units.")
    # Staleness of the unapproved source is determined from the same current
    # detached staffing input that created it, without treating it as final.
    teacher_data, _roster = load_teacher_assignment_input(academic_year_id=academic_year_id)
    teacher_snapshot = asdict(teacher_data)
    if placement_input_fingerprint(teacher_snapshot) != provisional_teacher_assignment_run.input_snapshot.get("fingerprint"):
        raise ValueError("The provisional teacher-assignment run is stale.")
    return {
        "staffing_mode": staffing_mode,
        "provisional_teacher_assignment_run_id": provisional_teacher_assignment_run.id,
        "provisional_teacher_assignments": [
            {"section_id": section_id, "teacher_id": assignment_by_section[section_id]}
            for section_id in sorted(assignment_by_section)
        ],
        "provisional_online_supervision_assignments": [
            {"online_supervision_session_id": session_id, "teacher_id": assignment_by_online_session[session_id]}
            for session_id in sorted(assignment_by_online_session)
        ],
        "provisional_teacher_input_fingerprint": provisional_teacher_assignment_run.input_snapshot.get("fingerprint"),
    }


def _active_assignment_backup_resolutions(*, academic_year_id, sections):
    """Return approved, active-source cancellation resolution facts by student."""

    from backend.apps.scheduling.models import PlanningRequestResolution, StaffingRequestResolution

    staffing_run_ids = {
        section.staffing_approval_offering.approval.staffing_run_id
        for section in sections
        if section.staffing_approval_offering_id
    }
    budget_approval_ids = {
        section.staffing_approval_offering.approval.staffing_run.budget_approval_id
        for section in sections
        if section.staffing_approval_offering_id
        and section.staffing_approval_offering.approval.staffing_run.budget_approval_id
    }
    resolutions = []
    if staffing_run_ids:
        resolutions.extend(StaffingRequestResolution.objects.filter(
            staffing_run_id__in=staffing_run_ids,
            staffing_run__approval__isnull=False,
        ).select_related("backup_request"))
    if budget_approval_ids:
        resolutions.extend(PlanningRequestResolution.objects.filter(
            approval_id__in=budget_approval_ids,
        ).select_related("backup_request"))
    by_student = defaultdict(list)
    for resolution in resolutions:
        by_student[resolution.student_id].append({
            "cancelled_course_ids": tuple(sorted(int(item) for item in resolution.cancelled_course_ids)),
            "backup_request_id": resolution.backup_request_id,
            "outcome": resolution.outcome,
            "unresolved_course_count": resolution.unresolved_course_count,
        })
    return by_student


def _student_assignment_request_in_scope(*, scope, request, course_offering_id, sections_by_offering):
    """Resolve request scope using the same explicit student/course/section IDs."""

    if scope.scope_type == "full":
        return True
    if request.student_id in scope.student_ids or request.course_id in scope.course_ids:
        return True
    return any(
        section.section_id in scope.section_ids
        for section in sections_by_offering.get(course_offering_id, ())
    )


def _student_assignment_enrollment_lock_ids(*, enrollment, locks):
    """Return active lock IDs that make this enrollment fixed context."""

    lock_ids = []
    enrollment_course_id = (
        enrollment.course_offering.course_id
        if enrollment.course_offering_id
        else enrollment.section.course_id
    )
    for lock in locks:
        if lock.lock_type == "whole_student_schedule" and lock.student_id == enrollment.student_id:
            lock_ids.append(lock.id)
        elif lock.lock_type == "section_roster" and lock.section_id == enrollment.section_id:
            lock_ids.append(lock.id)
        elif lock.lock_type == "course_roster" and lock.course_id == enrollment_course_id:
            lock_ids.append(lock.id)
        elif (
            lock.lock_type == "exact_student_section"
            and lock.student_id == enrollment.student_id
            and lock.course_id == enrollment_course_id
            and lock.section_id == enrollment.section_id
        ):
            lock_ids.append(lock.id)
    return tuple(sorted(lock_ids))


def load_student_assignment_input(
    *, academic_year_id, staffing_mode, provisional_teacher_assignment_run=None,
    soft_constraint_importance, scope=None, priority_request_ids=(),
    priority_request_limit=100, schedule_preservation_level="none",
    selected_lock_ids=None, objective_semantics_version=OBJECTIVE_SEMANTICS_V1,
    objective_importance_scores=None,
):
    """Load a fully detached student-assignment snapshot.

    Scope flags are resolved into the DTOs so the engine can ignore
    out-of-scope decisions without losing the audit/fingerprint facts needed
    when approval revalidates the run.  A normal run honors every active lock;
    an explicit lock selection is used only for the audited what-if path or a
    deliberately narrowed rerun input.
    """

    academic_year_id = int(academic_year_id)
    AcademicYear.objects.get(pk=academic_year_id)
    if scope is None:
        scope = StudentAssignmentScopeDTO()
    if not isinstance(scope, StudentAssignmentScopeDTO):
        raise ValueError("Student-assignment scope must be a detached StudentAssignmentScopeDTO.")
    required_importance_keys = {
        "section_utilization_balance",
        "student_semester_balance",
        "course_sequence_preferences",
        "difficulty_balance",
        "course_category_diversity",
    }
    if set(soft_constraint_importance) != required_importance_keys:
        raise ValueError("All five student-assignment soft_constraint_importance values are required.")
    if objective_semantics_version not in OBJECTIVE_SEMANTICS_VERSIONS:
        raise ValueError(
            f"Unsupported student-assignment objective semantics version: {objective_semantics_version!r}."
        )
    if objective_semantics_version == OBJECTIVE_SEMANTICS_V1 and objective_importance_scores:
        raise ValueError("Explicit 0-10 scores require objective_semantics_version='v2'.")
    resolved_importance_scores = resolve_importance_scores(
        labels={
            "section_utilization_balance": soft_constraint_importance["section_utilization_balance"],
            "student_semester_balance": soft_constraint_importance["student_semester_balance"],
            "course_sequence_preferences": soft_constraint_importance["course_sequence_preferences"],
            "difficulty_balance": soft_constraint_importance["difficulty_balance"],
            "course_category_diversity": soft_constraint_importance["course_category_diversity"],
        },
        scores=(objective_importance_scores if objective_semantics_version == OBJECTIVE_SEMANTICS_V2 else None),
    )
    sections = list(active_sections_for_year(academic_year_id).select_related(
        "course", "delivery_group__capacity_profile", "teacher",
        "staffing_approval_offering__approval__staffing_run",
    ).prefetch_related("delivery_group__offerings__course__priority_profile").order_by("id"))
    from backend.apps.scheduling.models import SectionSchedule
    schedules = {
        item.section_id: item
        for item in SectionSchedule.objects.filter(section_id__in=[section.id for section in sections]).select_related("timeslot")
    }
    section_dtos = []
    # Catalog pairs identify the academic first/second-half relationship;
    # section pairs identify the concrete two teaching sections sharing one
    # semester/block/teacher. Keeping both maps detached makes the pure engine
    # enforce the school-specific trimestre pattern without ORM knowledge.
    half_pair_by_course = {}
    for pair in HalfSemesterCoursePair.objects.filter(is_active=True).order_by("id"):
        half_pair_by_course[pair.first_course_id] = (
            pair.second_course_id,
            "first_half",
        )
        half_pair_by_course[pair.second_course_id] = (
            pair.first_course_id,
            "second_half",
        )
    half_section_pair_key = {}
    for pair in HalfSemesterSectionPair.objects.filter(
        first_section__academic_year_id=academic_year_id,
    ).order_by("id"):
        key = f"half_semester_section_pair:{pair.id}"
        half_section_pair_key[pair.first_section_id] = key
        half_section_pair_key[pair.second_section_id] = key
    for section in sections:
        schedule = schedules.get(section.id)
        if schedule is None or schedule.timeslot_id is None or schedule.timeslot.academic_year_id != academic_year_id:
            raise ValueError(f"Section {section.id} has no accepted target-year SectionSchedule.timeslot.")
        if schedule.timeslot.semester != section.semester:
            raise ValueError(f"Section {section.id} has a timeslot in a different semester.")
        if section.delivery_group_id:
            offerings = list(section.delivery_group.offerings.filter(
                academic_year_id=academic_year_id,
                status=COURSE_OFFERING_STATUS_OFFERED,
            ).select_related("course__priority_profile"))
            if not offerings:
                raise ValueError(f"Section {section.id} has no active offered delivery-group membership.")
            target_capacity = section.delivery_group.capacity_profile.target
        elif section.course_id:
            offerings = list(CourseOffering.objects.filter(
                academic_year_id=academic_year_id,
                course_id=section.course_id,
                status=COURSE_OFFERING_STATUS_OFFERED,
            ).select_related("course__priority_profile"))
            if len(offerings) != 1:
                raise ValueError(f"Legacy section {section.id} does not map unambiguously to one offered CourseOffering.")
            target_capacity = offerings[0].course.capacity_profile.target
        else:
            raise ValueError(f"Section {section.id} has no physical delivery identity.")
        section_dtos.append(StudentAssignmentSectionDTO(
            section_id=section.id,
            delivery_group_id=section.delivery_group_id or -section.course_id,
            member_course_offering_ids=tuple(item.id for item in offerings),
            member_course_ids=tuple(item.course_id for item in offerings),
            semester=section.semester,
            timeslot_id=schedule.timeslot_id,
            capacity_max=section.capacity_max,
            target_capacity=target_capacity,
            # Mode A deliberately excludes teacher identity from the detached
            # snapshot, so a later staffing edit cannot stale a sections-only
            # student run. Other modes retain their declared teacher context.
            teacher_id=section.teacher_id if staffing_mode != "sections_only" else None,
            half_semester_segment=section.half_semester_segment,
            half_semester_pair_key=half_section_pair_key.get(section.id),
        ))
    online_sessions = list(OnlineSupervisionSession.objects.filter(
        academic_year_id=academic_year_id,
        lifecycle_status="active",
    ).select_related("timeslot", "supervisor", "plan_approval_session").order_by("id"))
    online_offerings = list(CourseOffering.objects.filter(
        academic_year_id=academic_year_id,
        status=COURSE_OFFERING_STATUS_OFFERED,
        course__delivery_kind="online",
    ).select_related("course__priority_profile", "course__capacity_profile").order_by("id"))
    online_session_dtos = []
    for session in online_sessions:
        if session.timeslot_id is None or session.timeslot.academic_year_id != academic_year_id:
            raise ValueError(
                f"Online supervision session {session.id} has no accepted target-year timeslot."
            )
        # A negative engine-only section identity keeps shared online capacity
        # inside the existing detached candidate model without misrepresenting
        # the resource as a persisted instructional Section.
        engine_section_id = -session.id
        online_session_dtos.append(OnlineSupervisionSessionDTO(
            session_id=session.id,
            semester=session.timeslot.semester,
            timeslot_id=session.timeslot_id,
            capacity_max=session.capacity_max,
            target_capacity=session.target_capacity,
            supervisor_id=session.supervisor_id if staffing_mode != "sections_only" else None,
        ))
        section_dtos.append(StudentAssignmentSectionDTO(
            section_id=engine_section_id,
            delivery_group_id=engine_section_id,
            member_course_offering_ids=tuple(item.id for item in online_offerings),
            member_course_ids=tuple(item.course_id for item in online_offerings),
            semester=session.timeslot.semester,
            timeslot_id=session.timeslot_id,
            capacity_max=session.capacity_max,
            target_capacity=session.target_capacity,
            teacher_id=session.supervisor_id if staffing_mode != "sections_only" else None,
        ))
    staffing_context = _student_assignment_staffing_context(
        academic_year_id=academic_year_id,
        sections=sections,
        online_sessions=online_sessions,
        staffing_mode=staffing_mode,
        provisional_teacher_assignment_run=provisional_teacher_assignment_run,
    )
    offered_by_course = {
        item.course_id: item
        for item in CourseOffering.objects.filter(
            academic_year_id=academic_year_id,
            status=COURSE_OFFERING_STATUS_OFFERED,
        ).select_related("course__priority_profile")
    }
    active_locks = list(
        StudentAssignmentLock.objects.filter(
            academic_year_id=academic_year_id,
            is_active=True,
        ).prefetch_related("members").order_by("id")
    )
    if selected_lock_ids is not None:
        try:
            selected_lock_ids = tuple(sorted({int(lock_id) for lock_id in selected_lock_ids}))
        except (TypeError, ValueError) as error:
            raise ValueError("Selected lock IDs must be positive integer identifiers.") from error
        if any(lock_id <= 0 for lock_id in selected_lock_ids):
            raise ValueError("Selected lock IDs must be positive integer identifiers.")
        active_lock_by_id = {lock.id: lock for lock in active_locks}
        missing_lock_ids = [lock_id for lock_id in selected_lock_ids if lock_id not in active_lock_by_id]
        if missing_lock_ids:
            raise ValueError(f"Selected student-assignment locks are not active: {missing_lock_ids}.")
        active_locks = [active_lock_by_id[lock_id] for lock_id in selected_lock_ids]
    from backend.apps.scheduling.services.student_assignment_locks import (
        validate_student_assignment_lock_staffing_mode,
    )
    for lock in active_locks:
        # Existing data must obey the same final-staffing restriction as new
        # lock creation; otherwise an old teacher lock could silently affect a
        # run that explicitly says teacher identity is provisional or ignored.
        validate_student_assignment_lock_staffing_mode(
            lock_type=lock.lock_type,
            staffing_mode=staffing_mode,
        )
    lock_dtos = tuple(
        StudentAssignmentLockDTO(
            lock_id=lock.id,
            lock_type=lock.lock_type,
            student_id=lock.student_id,
            section_id=lock.section_id,
            course_id=lock.course_id,
            teacher_id=lock.teacher_id,
            member_student_ids=tuple(sorted(item.student_id for item in lock.members.all())),
            is_active=True,
        )
        for lock in active_locks
    )
    special_locks = list(StudentSpecialCommitmentLock.objects.filter(
        academic_year_id=academic_year_id,
        is_active=True,
    ).select_related("schedule_commitment_request", "course_request__course", "timeslot").order_by("id"))
    special_lock_dtos = tuple(
        StudentSpecialCommitmentLockDTO(
            lock_id=lock.id,
            lock_type=lock.lock_type,
            lock_mode=lock.lock_mode,
            schedule_commitment_request_id=lock.schedule_commitment_request_id,
            course_request_id=lock.course_request_id,
            timeslot_id=lock.timeslot_id,
            semester=lock.semester,
            co_op_block_pair=lock.co_op_block_pair,
        )
        for lock in special_locks
    )
    fixed_rows = []
    fixed_course_by_student = set()
    movable_course_by_student = set()
    section_by_id = {item.section_id: item for item in section_dtos}
    sections_by_offering = defaultdict(list)
    for section in section_dtos:
        for offering_id in section.member_course_offering_ids:
            sections_by_offering[offering_id].append(section)
    for enrollment in Enrollment.objects.filter(
        section_id__in=section_by_id,
    ).select_related("course_offering", "section__course").order_by("id"):
        section = section_by_id[enrollment.section_id]
        offering = enrollment.course_offering
        if offering is None:
            candidates = [
                offering_id for offering_id in section.member_course_offering_ids
                if len(section.member_course_offering_ids) == 1
            ]
            if len(candidates) != 1:
                raise ValueError(f"Legacy enrollment {enrollment.id} cannot be mapped unambiguously to an offering.")
            offering = CourseOffering.objects.get(pk=candidates[0])
        if offering.id not in section.member_course_offering_ids:
            raise ValueError(f"Enrollment {enrollment.id} offering does not belong to its physical section.")
        is_active = enrollment.lifecycle_status == ENROLLMENT_LIFECYCLE_ACTIVE
        in_scope = is_active and (
            scope.scope_type == "full"
            or enrollment.student_id in scope.student_ids
            or offering.course_id in scope.course_ids
            or enrollment.section_id in scope.section_ids
        )
        lock_ids = _student_assignment_enrollment_lock_ids(
            enrollment=enrollment,
            locks=active_locks,
        )
        is_locked = bool(lock_ids)
        row = FixedEnrollmentDTO(
            enrollment_id=enrollment.id,
            student_id=enrollment.student_id,
            section_id=enrollment.section_id,
            course_offering_id=offering.id,
            course_id=offering.course_id,
            semester=section.semester,
            timeslot_id=section.timeslot_id,
            is_active=is_active,
            is_locked=is_locked,
            is_historical=not is_active,
            is_in_scope=in_scope,
            lock_ids=lock_ids,
            half_semester_segment=section.half_semester_segment,
            credit_value=float(offering.course.credit_value),
        )
        fixed_rows.append(row)
        if is_active:
            if in_scope and not is_locked:
                movable_course_by_student.add((enrollment.student_id, offering.course_id))
            else:
                fixed_course_by_student.add((enrollment.student_id, offering.course_id))
    online_session_by_id = {item.session_id: item for item in online_session_dtos}
    for enrollment in OnlineEnrollment.objects.filter(
        supervision_session__academic_year_id=academic_year_id,
    ).select_related("course_offering__course", "supervision_session__timeslot").order_by("id"):
        session = online_session_by_id.get(enrollment.supervision_session_id)
        if session is None:
            if enrollment.lifecycle_status == "active":
                raise ValueError(
                    f"Active online enrollment {enrollment.id} references an inactive or unplaced supervision session."
                )
            continue
        offering = enrollment.course_offering
        is_active = enrollment.lifecycle_status == "active"
        in_scope = is_active and (
            scope.scope_type == "full"
            or enrollment.student_id in scope.student_ids
            or offering.course_id in scope.course_ids
        )
        special_lock_ids = tuple(sorted(
            lock.id for lock in special_locks
            if lock.course_request_id
            and lock.course_request.student_id == enrollment.student_id
            and lock.course_request.course_id == offering.course_id
        ))
        row = FixedEnrollmentDTO(
            enrollment_id=enrollment.id,
            student_id=enrollment.student_id,
            section_id=-enrollment.supervision_session_id,
            course_offering_id=offering.id,
            course_id=offering.course_id,
            semester=session.semester,
            timeslot_id=session.timeslot_id,
            is_active=is_active,
            is_locked=bool(special_lock_ids),
            is_historical=not is_active,
            is_in_scope=in_scope,
            lock_ids=special_lock_ids,
            half_semester_segment=half_pair_by_course.get(
                offering.course_id, (None, None)
            )[1],
            credit_value=float(offering.course.credit_value),
            delivery_kind="online",
        )
        fixed_rows.append(row)
        if is_active:
            if in_scope and not row.is_locked:
                movable_course_by_student.add((enrollment.student_id, offering.course_id))
            else:
                fixed_course_by_student.add((enrollment.student_id, offering.course_id))

    commitment_request_dtos = tuple(
        StudentScheduleCommitmentRequestDTO(
            request_id=item.id,
            student_id=item.student_id,
            commitment_type=item.commitment_type,
            is_in_scope=(
                scope.scope_type == "full" or item.student_id in scope.student_ids
            ),
        )
        for item in StudentScheduleCommitmentRequest.objects.filter(
            academic_year_id=academic_year_id
        ).order_by("id")
    )
    fixed_commitments = []
    for commitment in StudentScheduleCommitment.objects.filter(
        academic_year_id=academic_year_id,
    ).select_related("course_request__course", "course_offering").prefetch_related("occupancies").order_by("id"):
        is_active = commitment.lifecycle_status == "active"
        source_request_id = commitment.schedule_commitment_request_id
        source_course_request_id = commitment.course_request_id
        relevant_lock_ids = tuple(sorted(
            lock.id for lock in special_locks
            if (
                source_request_id is not None
                and lock.schedule_commitment_request_id == source_request_id
            ) or (
                source_course_request_id is not None
                and lock.course_request_id == source_course_request_id
            )
        ))
        fixed_commitments.append(FixedStudentScheduleCommitmentDTO(
            commitment_id=commitment.id,
            student_id=commitment.student_id,
            commitment_kind=commitment.commitment_kind,
            schedule_commitment_request_id=source_request_id,
            course_request_id=source_course_request_id,
            course_offering_id=commitment.course_offering_id,
            course_id=(
                commitment.course_request.course_id
                if commitment.course_request_id
                else None
            ),
            credit_value=float(commitment.credit_value),
            occupancy=tuple(sorted(
                (item.timeslot_id, item.half_semester_segment)
                for item in commitment.occupancies.all()
            )),
            is_active=is_active,
            is_locked=bool(relevant_lock_ids),
            is_historical=not is_active,
            is_in_scope=(
                is_active and (
                    scope.scope_type == "full" or commitment.student_id in scope.student_ids
                )
            ),
        ))
    resolutions_by_student = _active_assignment_backup_resolutions(
        academic_year_id=academic_year_id,
        sections=sections,
    )
    requests = []
    for request in CourseRequest.objects.filter(academic_year_id=academic_year_id).select_related(
        "course__priority_profile"
    ).order_by("id"):
        if (request.student_id, request.course_id) in fixed_course_by_student:
            continue
        offering = offered_by_course.get(request.course_id)
        if request.request_type == COURSE_REQUEST_TYPE_ALTERNATE:
            continue
        if offering:
            in_scope = _student_assignment_request_in_scope(
                scope=scope,
                request=request,
                course_offering_id=offering.id,
                sections_by_offering=sections_by_offering,
            )
            current_enrollment_id = None
            if (request.student_id, request.course_id) in movable_course_by_student:
                current_enrollment_id = next(
                    row.enrollment_id
                    for row in fixed_rows
                    if row.is_active
                    and row.is_in_scope
                    and not row.is_locked
                    and row.student_id == request.student_id
                    and row.course_id == request.course_id
                )
            requests.append(StudentAssignmentRequestDTO(
                request_id=request.id, student_id=request.student_id, course_id=request.course_id,
                course_offering_id=offering.id, is_primary=True,
                is_mandatory=request.is_mandatory,
                priority_tier=request.course.priority_profile.tier,
                current_enrollment_id=current_enrollment_id,
                is_in_scope=in_scope,
                delivery_kind=request.course.delivery_kind,
                duration=request.course.duration,
                credit_value=float(request.course.credit_value),
                half_semester_segment=half_pair_by_course.get(
                    request.course_id, (None, None)
                )[1],
                paired_half_course_id=half_pair_by_course.get(
                    request.course_id, (None, None)
                )[0],
            ))
            continue
        relevant = [
            item for item in resolutions_by_student[request.student_id]
            if request.course_id in item["cancelled_course_ids"]
        ]
        possibilities = {(item["outcome"], item["backup_request_id"]) for item in relevant}
        if len(possibilities) != 1:
            raise ValueError(
                f"Cancelled primary request {request.id} has no unambiguous approved active-source backup resolution."
            )
        outcome, backup_request_id = possibilities.pop()
        if outcome != "backup_promoted" or not backup_request_id:
            raise ValueError(f"Cancelled primary request {request.id} remains unresolved by approved upstream planning.")
        backup = CourseRequest.objects.select_related("course__priority_profile").get(pk=backup_request_id)
        backup_offering = offered_by_course.get(backup.course_id)
        if backup.student_id != request.student_id or backup_offering is None:
            raise ValueError(f"Approved backup for cancelled request {request.id} is no longer usable.")
        if (backup.student_id, backup.course_id) not in fixed_course_by_student:
            in_scope = _student_assignment_request_in_scope(
                scope=scope,
                request=backup,
                course_offering_id=backup_offering.id,
                sections_by_offering=sections_by_offering,
            )
            requests.append(StudentAssignmentRequestDTO(
                request_id=backup.id, student_id=backup.student_id, course_id=backup.course_id,
                course_offering_id=backup_offering.id, is_primary=False,
                is_mandatory=False, priority_tier=backup.course.priority_profile.tier,
                assignment_basis="approved_backup",
                backup_resolution_snapshot={
                    "cancelled_primary_request_id": request.id,
                    "cancelled_course_id": request.course_id,
                    "outcome": outcome,
                },
                is_in_scope=in_scope,
                delivery_kind=backup.course.delivery_kind,
                duration=backup.course.duration,
                credit_value=float(backup.course.credit_value),
                half_semester_segment=half_pair_by_course.get(
                    backup.course_id, (None, None)
                )[1],
                paired_half_course_id=half_pair_by_course.get(
                    backup.course_id, (None, None)
                )[0],
            ))
    hard_edges = list(CoursePrerequisite.objects.values_list("prerequisite_id", "course_id"))
    if _has_directed_cycle(hard_edges):
        raise ValueError("Hard CoursePrerequisite configuration contains a directed cycle.")
    soft_edges = list(CourseSequencePreference.objects.filter(is_active=True).values_list("earlier_course_id", "later_course_id"))
    if _has_directed_cycle(soft_edges):
        raise ValueError("CourseSequencePreference configuration contains a directed cycle.")
    effective_request_ids = {request.request_id for request in requests}
    priority_request_ids = tuple(sorted(int(request_id) for request_id in priority_request_ids))
    if len(priority_request_ids) != len(set(priority_request_ids)):
        raise ValueError("Priority request IDs must be unique.")
    if not set(priority_request_ids) <= effective_request_ids:
        raise ValueError("Priority request IDs must identify effective requests in this snapshot.")
    if len(priority_request_ids) > int(priority_request_limit):
        raise ValueError("Priority request IDs exceed the configured run limit.")
    relevant_course_ids = {
        item.course_id for item in requests
    } | {
        item.course_id for item in fixed_rows
        if item.is_active and not item.is_historical
    } | {
        item.course_id
        for item in fixed_commitments
        if item.is_active
        and not item.is_historical
        and item.course_id is not None
    }
    relevant_courses = list(Course.objects.filter(id__in=relevant_course_ids).order_by("id"))
    historical_rows = list(StudentCourseHistoricalResult.objects.filter(
        course_id__in=relevant_course_ids,
    ).select_related("academic_year").order_by("academic_year__name", "id"))
    student_year_keys = {(row.student_id, row.academic_year_id) for row in historical_rows}
    comparison_rows = StudentCourseHistoricalResult.objects.filter(
        student_id__in={key[0] for key in student_year_keys},
        academic_year_id__in={key[1] for key in student_year_keys},
    ).select_related("academic_year") if student_year_keys else ()
    student_year_results = defaultdict(list)
    for row in comparison_rows:
        if (row.student_id, row.academic_year_id) in student_year_keys:
            student_year_results[row.student_id, row.academic_year_id].append(row)
    rows_by_course = defaultdict(list)
    for row in historical_rows:
        rows_by_course[row.course_id].append(row)
    course_difficulties = tuple(
        CourseDifficultyDTO(**course_difficulty_facts(
            course,
            historical_results=rows_by_course[course.id],
            student_year_results=student_year_results,
        ))
        for course in relevant_courses
    )
    course_category_relationships = tuple(
        CourseCategoryRelationshipDTO(
            category_a=item.category_a,
            category_b=item.category_b,
            similarity_score=item.similarity_score,
        )
        for item in CourseCategoryRelationship.objects.order_by("category_a", "category_b", "id")
    )
    student_grades = tuple(
        (int(student_id), int(grade_level))
        for student_id, grade_level in Student.objects.filter(
            academic_year_id=academic_year_id,
        ).order_by("id").values_list("id", "grade_level")
    )
    return StudentAssignmentInputDTO(
        academic_year_id=academic_year_id,
        requests=tuple(requests), sections=tuple(section_dtos), fixed_enrollments=tuple(fixed_rows),
        hard_prerequisites=tuple(
            CoursePrerequisiteDTO(course_id=course_id, prerequisite_id=prerequisite_id)
            for prerequisite_id, course_id in hard_edges
        ),
        soft_sequence_preferences=tuple(
            CourseSequencePreferenceDTO(earlier_course_id=earlier, later_course_id=later)
            for earlier, later in soft_edges
        ),
        section_utilization_balance_importance=soft_constraint_importance["section_utilization_balance"],
        student_semester_balance_importance=soft_constraint_importance["student_semester_balance"],
        course_sequence_preferences_importance=soft_constraint_importance["course_sequence_preferences"],
        difficulty_balance_importance=soft_constraint_importance["difficulty_balance"],
        course_category_diversity_importance=soft_constraint_importance["course_category_diversity"],
        course_difficulties=course_difficulties,
        course_category_relationships=course_category_relationships,
        online_supervision_sessions=tuple(online_session_dtos),
        schedule_commitment_requests=commitment_request_dtos,
        special_commitment_locks=special_lock_dtos,
        fixed_schedule_commitments=tuple(fixed_commitments),
        timeslots=tuple(
            TimeSlotDTO(
                slot.id, slot.academic_year_id, slot.semester, slot.block, slot.is_available
            )
            for slot in TimeSlot.objects.filter(academic_year_id=academic_year_id).order_by("id")
        ),
        student_assignment_locks=lock_dtos,
        schedule_preservation_level=schedule_preservation_level,
        priority_request_ids=priority_request_ids,
        priority_request_limit=int(priority_request_limit),
        scope=scope,
        student_ids_with_alternate_requests=tuple(sorted(set(
            CourseRequest.objects.filter(
                academic_year_id=academic_year_id,
                request_type=COURSE_REQUEST_TYPE_ALTERNATE,
            ).values_list("student_id", flat=True)
        ))),
        objective_semantics_version=objective_semantics_version,
        objective_importance_scores=(
            resolved_importance_scores if objective_semantics_version == OBJECTIVE_SEMANTICS_V2 else {}
        ),
        student_grades=student_grades,
    ), staffing_context


def load_section_placement_input(*, academic_year_id, input_mode, budget_approval=None, conflict_matrix=None):
    """Build the exact ORM-to-placement DTO snapshot once for a solver run.

    The adapter separately computes remaining workload from administrative
    reservations and accepted *outside-scope* placements. This prevents a
    decision unit from being subtracted once here and again by the witness model.
    """

    from backend.apps.constraints.models import CourseConflictMatrix, TeacherAvailability
    from backend.apps.control.models import ManualOverride, SectionLock
    from backend.apps.courses.models import (
        DeliveryGroup, Enrollment, HalfSemesterSectionPair, Section,
    )
    from backend.apps.courses.selectors import active_delivery_groups_for_year, active_sections_for_year
    from backend.apps.scheduling.constants import (
        SECTION_PLACEMENT_INPUT_ANNUAL_TOTAL,
        SECTION_PLACEMENT_INPUT_FIXED_SEMESTER,
    )
    from backend.apps.scheduling.models import (
        AnnualPlacementLock, SectionBudgetApproval, SectionSchedule,
        TeacherPlanningCapacity, TeacherPlanningRoster, OnlineSupervisionSession,
    )

    if input_mode not in {SECTION_PLACEMENT_INPUT_FIXED_SEMESTER, SECTION_PLACEMENT_INPUT_ANNUAL_TOTAL}:
        raise ValueError("Placement input_mode must be fixed_semester or annual_total.")
    base = load_scheduling_input(academic_year_id, require_ready_roster=True)
    # Reuse the normalized, fail-closed qualification compiler rather than
    # reimplementing Grade 11-12 eligibility in a placement-specific adapter.
    compiled = compile_constraints(base)
    roster = TeacherPlanningRoster.objects.get(academic_year_id=academic_year_id)
    if conflict_matrix is None:
        try:
            conflict_matrix = CourseConflictMatrix.objects.get(academic_year_id=academic_year_id)
        except CourseConflictMatrix.DoesNotExist as error:
            raise ValueError("Create the current academic year's course conflict matrix before placement.") from error
    if conflict_matrix.academic_year_id != int(academic_year_id):
        raise ValueError("The selected course conflict matrix belongs to another academic year.")

    group_queryset = active_delivery_groups_for_year(academic_year_id).prefetch_related("offerings__course")
    groups = {group.id: group for group in group_queryset}
    schedule_rows = {
        item.section_id: item
        for item in SectionSchedule.objects.filter(
            section__academic_year_id=academic_year_id,
            section__lifecycle_status=SECTION_LIFECYCLE_ACTIVE,
        ).select_related("timeslot", "section")
    }
    locks = {
        item.section_id: item
        for item in SectionLock.objects.filter(
            section__academic_year_id=academic_year_id,
            section__lifecycle_status=SECTION_LIFECYCLE_ACTIVE,
        ).select_related("locked_timeslot", "locked_teacher")
    }
    paired_section_key = {}
    for pair in HalfSemesterSectionPair.objects.filter(
        first_section__academic_year_id=academic_year_id,
    ).select_related("first_section", "second_section").order_by("id"):
        key = f"half_semester_pair:{pair.id}"
        paired_section_key[pair.first_section_id] = key
        paired_section_key[pair.second_section_id] = key
    dependency_section_ids = set(Enrollment.objects.filter(
        section__academic_year_id=academic_year_id,
        section__lifecycle_status=SECTION_LIFECYCLE_ACTIVE,
        lifecycle_status="active",
    ).values_list("section_id", flat=True)) | set(ManualOverride.objects.filter(
        section__academic_year_id=academic_year_id,
        section__lifecycle_status=SECTION_LIFECYCLE_ACTIVE,
    ).values_list("section_id", flat=True))
    units, fixed = [], []
    if input_mode == SECTION_PLACEMENT_INPUT_FIXED_SEMESTER:
        sections = active_sections_for_year(academic_year_id).select_related("course", "delivery_group").prefetch_related("delivery_group__offerings__course")
        for section in sections:
            lock = locks.get(section.id)
            schedule = schedule_rows.get(section.id)
            fixed_teacher_id = lock.locked_teacher_id if lock and lock.locked_teacher_id else section.teacher_id
            if schedule:
                if schedule.timeslot_id is None:
                    raise ValueError(f"Section {section.id} has a schedule row without accepted timing.")
                fixed.append(FixedPlacementDTO(
                    section.id,
                    schedule.timeslot_id,
                    fixed_teacher_id,
                    member_course_ids=tuple(
                        section.delivery_group.offerings.values_list(
                            "course_id", flat=True,
                        ) if section.delivery_group_id else (section.course_id,)
                    ),
                    capacity_max=section.capacity_max,
                ))
                continue
            if (
                section.planning_approval_course_id is None
                and section.staffing_approval_offering_id is None
                and section.annual_placement_approval_id is None
            ):
                # A manually created unscheduled section has no solver-owned
                # replacement contract. Counselors must first give it timing or
                # create it through an approved planning workflow.
                raise ValueError(f"Section {section.id} is manual fixed context without accepted timing.")
            if section.is_locked and (lock is None or lock.locked_timeslot_id is None):
                raise ValueError(f"Section {section.id} is locked but has no usable locked timeslot.")
            if section.id in dependency_section_ids:
                raise ValueError(f"Section {section.id} has downstream dependency context without accepted timing.")
            if not section.delivery_group_id and not section.course_id:
                raise ValueError(f"Section {section.id} has no course or delivery group.")
            group = groups.get(section.delivery_group_id) if section.delivery_group_id else None
            member_course_ids = tuple(item.course_id for item in group.offerings.all()) if group else (section.course_id,)
            allowed = _group_allowed_semesters(group) if group else _allowed_semester_ids(section.course.allowed_semester)
            units.append(PlacementUnitDTO(
                key=f"section:{section.id}", section_id=section.id,
                delivery_group_id=section.delivery_group_id or -section.course_id,
                member_course_ids=member_course_ids, allowed_semesters=allowed,
                fixed_semester=section.semester,
                locked_timeslot_id=lock.locked_timeslot_id if lock else None,
                locked_teacher_id=lock.locked_teacher_id if lock else None,
                source_mode=input_mode,
                shared_staffing_key=paired_section_key.get(section.id),
                capacity_max=section.capacity_max,
            ))
    else:
        if budget_approval is None:
            raise ValueError("annual_total placement requires an approved section budget.")
        if budget_approval.budget_run.academic_year_id != int(academic_year_id):
            raise ValueError("The budget approval belongs to another academic year.")
        approval_rows = list(budget_approval.offering_approvals.select_related("delivery_group").order_by("delivery_group_id"))
        approval_group_ids = {row.delivery_group_id for row in approval_rows}
        active_sections = active_sections_for_year(academic_year_id).filter(delivery_group_id__in=approval_group_ids)
        if active_sections.exists():
            raise ValueError("Annual placement cannot overwrite delivery groups that already have active materialized sections.")
        annual_locks = {
            (item.delivery_group_id, item.annual_index): item
            for item in AnnualPlacementLock.objects.filter(
                academic_year_id=academic_year_id,
                delivery_group_id__in=approval_group_ids,
            ).select_related("locked_timeslot")
        }
        for row in approval_rows:
            group = groups.get(row.delivery_group_id)
            if group is None:
                raise ValueError("A budget approval references an inactive delivery group.")
            allowed = _group_allowed_semesters(group)
            if not allowed:
                raise ValueError(f"Delivery group {group.id} has no legal shared semester.")
            for annual_index in range(1, row.approved_annual_count + 1):
                lock = annual_locks.get((group.id, annual_index))
                units.append(PlacementUnitDTO(
                    key=f"annual:{group.id}:{annual_index}", delivery_group_id=group.id,
                    member_course_ids=tuple(item.course_id for item in group.offerings.all()),
                    allowed_semesters=allowed,
                    locked_timeslot_id=lock.locked_timeslot_id if lock else None,
                    annual_index=annual_index, source_mode=input_mode,
                    capacity_max=group.capacity_profile.hard_max,
                ))
        out_of_range = [
            f"{group_id}:{index}"
            for (group_id, index) in annual_locks
            if index > next(row.approved_annual_count for row in approval_rows if row.delivery_group_id == group_id)
        ]
        if out_of_range:
            raise ValueError(f"Annual placement lock(s) are outside approved annual counts: {', '.join(out_of_range)}.")

    # Online supervision is a shared physical resource, not a normal course
    # section.  Its approved annual slots join the same timing solve because a
    # supervisor still needs one workload-safe A-D block, while its missing
    # subject qualification is intentional rather than a data error.
    online_sessions = list(OnlineSupervisionSession.objects.filter(
        academic_year_id=academic_year_id,
        lifecycle_status="active",
    ).select_related("timeslot", "plan_approval_session").order_by("id"))
    online_placement_sessions = []
    for session in online_sessions:
        if session.plan_approval_session_id is None:
            if session.timeslot_id is None:
                raise ValueError(
                    f"Online supervision session {session.id} has no approved capacity provenance."
                )
            # A legacy accepted session is fixed context, not a newly planned
            # resource. Its approved timing is enough to reserve the generic
            # seat in the student-level diversity witness.
            allowed = (session.timeslot.semester,)
        else:
            allowed = tuple(sorted(int(value) for value in session.plan_approval_session.allowed_semesters))
        if not allowed:
            raise ValueError(f"Online supervision session {session.id} has no legal semester.")
        online_placement_sessions.append(OnlineSupervisionPlacementSessionDTO(
            session_id=session.id,
            capacity_max=session.capacity_max,
            allowed_semesters=allowed,
            fixed_timeslot_id=session.timeslot_id,
        ))
        if session.timeslot_id:
            fixed.append(FixedPlacementDTO(
                section_id=None,
                timeslot_id=session.timeslot_id,
                teacher_id=session.supervisor_id,
                online_supervision_session_id=session.id,
            ))
            continue
        units.append(PlacementUnitDTO(
            key=f"online_supervision:{session.id}",
            delivery_group_id=-session.id,
            member_course_ids=(),
            allowed_semesters=allowed,
            source_mode=input_mode,
            requires_course_qualification=False,
            online_supervision_session_id=session.id,
        ))

    # This carries primary online co-request facts into the timing stage solely
    # to prove that generic supervision seats can occupy distinct blocks for a
    # student with multiple online courses. It does not assign a student to a
    # session early or make supervision resources course-specific.
    offered_online_course_ids = set(CourseOffering.objects.filter(
        academic_year_id=academic_year_id,
        status=COURSE_OFFERING_STATUS_OFFERED,
        course__delivery_kind="online",
    ).values_list("course_id", flat=True))
    online_supervision_demands = tuple(
        OnlineSupervisionDemandDTO(
            request_id=request.id,
            student_id=request.student_id,
            course_id=request.course_id,
            allowed_semesters=_allowed_semester_ids(request.course.allowed_semester),
        )
        for request in CourseRequest.objects.filter(
            academic_year_id=academic_year_id,
            request_type=COURSE_REQUEST_TYPE_PRIMARY,
            course_id__in=offered_online_course_ids,
        ).select_related("course").order_by("id")
    )

    half_pair_by_course = {}
    for pair in HalfSemesterCoursePair.objects.filter(is_active=True).order_by("id"):
        half_pair_by_course[pair.first_course_id] = pair.second_course_id
        half_pair_by_course[pair.second_course_id] = pair.first_course_id

    # This private placement witness carries completion-defining normal demand
    # for both full- and half-semester courses. It proves that accepted timing
    # has enough block-level capacity for known pathways without creating early
    # rosters or duplicating the later special-commitment student-assignment
    # model. The engine groups a valid half-course pair as one occupied block
    # while retaining both course identities for capacity accounting.
    student_timetable_demands = tuple(
        PlacementStudentTimetableDemandDTO(
            request_id=request.id,
            student_id=request.student_id,
            course_id=request.course_id,
            allowed_semesters=_allowed_semester_ids(request.course.allowed_semester),
            duration=request.course.duration,
            paired_half_course_id=(
                half_pair_by_course.get(request.course_id)
                if request.course.duration == COURSE_DURATION_HALF_SEMESTER
                else None
            ),
        )
        for request in CourseRequest.objects.filter(
            academic_year_id=academic_year_id,
            course__delivery_kind=COURSE_DELIVERY_KIND_NORMAL_INSTRUCTION,
            course__duration__in=(
                COURSE_DURATION_FULL_SEMESTER,
                COURSE_DURATION_HALF_SEMESTER,
            ),
        ).filter(
            Q(request_type=COURSE_REQUEST_TYPE_PRIMARY) | Q(is_mandatory=True)
        ).order_by("id")
    )

    # Existing accepted schedules outside the current decision scope reserve a
    # teacher at that block and consume workload before candidates are modelled.
    decision_section_ids = {unit.section_id for unit in units if unit.section_id}
    fixed_context = [item for item in fixed if item.section_id not in decision_section_ids]
    fixed_load = {}
    for item in fixed_context:
        if item.teacher_id:
            slot = next(slot for slot in base.timeslots if slot.id == item.timeslot_id)
            fixed_load[item.teacher_id, slot.semester] = fixed_load.get((item.teacher_id, slot.semester), 0) + 1
    configured = {
        (item.teacher_id, item.semester): item
        for item in TeacherPlanningCapacity.objects.filter(academic_year_id=academic_year_id)
    }
    explicit_unavailable = defaultdict(set)
    for item in TeacherAvailability.objects.filter(
        teacher_id__in=[teacher.id for teacher in base.teachers],
        timeslot__academic_year_id=academic_year_id,
        is_available=False,
    ):
        explicit_unavailable[item.teacher_id].add(item.timeslot_id)
    teachers = []
    for teacher in base.teachers:
        sem_capacity = {}
        for semester in (1, 2):
            config = configured[teacher.id, semester]
            sem_capacity[semester] = max(0, config.maximum_sections - config.reserved_sections - fixed_load.get((teacher.id, semester), 0))
        annual = max(0, teacher.max_courses_total - sum(configured[teacher.id, semester].reserved_sections for semester in (1, 2)) - sum(fixed_load.get((teacher.id, semester), 0) for semester in (1, 2)))
        teachers.append(PlacementTeacherDTO(
            id=teacher.id,
            eligible_course_ids=tuple(sorted(course_id for course_id, teacher_ids in compiled.qualified_teacher_ids_by_course.items() if teacher.id in teacher_ids)),
            remaining_semester_1=sem_capacity[1], remaining_semester_2=sem_capacity[2], remaining_annual=annual,
            unavailable_timeslot_ids=tuple(sorted(explicit_unavailable[teacher.id])),
        ))
    conflicts = tuple(
        PlacementConflictDTO(
            item.course_a_id, item.course_b_id, float(item.weight),
            float(item.estimated_retained_co_request_count),
        )
        for item in conflict_matrix.conflicts.filter(
            course_a_id__in=[course.id for course in base.courses],
            course_b_id__in=[course.id for course in base.courses],
        )
    )
    return PlacementInputDTO(
        academic_year_id=int(academic_year_id), input_mode=input_mode,
        units=tuple(units), fixed_placements=tuple(fixed_context),
        timeslots=base.timeslots, teachers=tuple(teachers), conflicts=conflicts,
        online_supervision_sessions=tuple(online_placement_sessions),
        online_supervision_demands=online_supervision_demands,
        student_timetable_demands=student_timetable_demands,
    ), conflict_matrix, roster
