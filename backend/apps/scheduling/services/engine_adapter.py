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

from dataclasses import asdict
from collections import defaultdict
from hashlib import sha256
import json

from django.db.models import Q

from scheduling_engine.demand_analyzer import parse_academic_year_start
from scheduling_engine.dto import (
    AcademicYearDTO, CounselorConstraintPreferenceDTO, CourseConflictDTO, CourseDTO,
    CoursePrerequisiteDTO, CourseQualificationRequirementDTO, CourseRequestDTO,
    CourseRoomRequirementDTO, HardConstraintDTO, HistoricalDemandDTO, QualificationDTO,
    RoomDTO, SchedulingInputDTO, SectionDTO, SectionLockDTO, SoftConstraintDTO,
    StudentDTO, TeacherAvailabilityDTO, TeacherCoursePreferenceDTO, TeacherCurrentCourseDTO,
    TeacherDTO, TeacherPlanningCapacityDTO, TeacherQualificationDTO, TimeSlotDTO,
    PlanningOfferingDTO,
    FixedPlacementDTO, PlacementConflictDTO, PlacementInputDTO,
    PlacementTeacherDTO, PlacementUnitDTO,
    FixedTeacherAssignmentDTO, TeacherAssignmentInputDTO,
    TeacherAssignmentSectionDTO, TeacherAssignmentTeacherDTO,
    TeacherCourseAssignmentRuleDTO,
)
from scheduling_engine.constraint_compiler import compile_constraints
from scheduling_engine.section_estimator import estimate_section_counts
from scheduling_engine.section_planner import plan_section_counts

from backend.apps.common.constants import (
    COURSE_REQUEST_TYPE_PRIMARY,
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
    CoursePrerequisite,
    CourseRequest,
    DeliveryGroup,
    Section,
)
from backend.apps.courses.selectors import (
    active_delivery_groups_for_year,
    active_sections_for_year,
)
from backend.apps.people.models import Student, Teacher
from backend.apps.scheduling.models import (
    TeacherPlanningCapacity,
    TeacherPlanningAnnualCapacity,
    TeacherPlanningRoster,
    TimeSlot,
)


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
    from backend.apps.courses.models import Section
    from backend.apps.scheduling.models import (
        SectionSchedule, TeacherCourseAssignmentRule, TeacherPlanningAnnualCapacity,
        TeacherPlanningRoster, TeacherTimePreference,
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
        ))
        if is_fixed:
            fixed.append(FixedTeacherAssignmentDTO(
                section_id=section.id, teacher_id=section.teacher_id,
                semester=section.semester, timeslot_id=schedule.timeslot_id,
                member_course_ids=member_course_ids,
            ))

    configured_semester = {
        (item.teacher_id, item.semester): item
        for item in TeacherPlanningCapacity.objects.filter(
            academic_year_id=academic_year_id, teacher_id__in=roster_teacher_ids,
        )
    }
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


def load_section_placement_input(*, academic_year_id, input_mode, budget_approval=None, conflict_matrix=None):
    """Build the exact ORM-to-placement DTO snapshot once for a solver run.

    The adapter separately computes remaining workload from administrative
    reservations and accepted *outside-scope* placements. This prevents a
    decision unit from being subtracted once here and again by the witness model.
    """

    from backend.apps.constraints.models import CourseConflictMatrix, TeacherAvailability
    from backend.apps.control.models import ManualOverride, SectionLock
    from backend.apps.courses.models import DeliveryGroup, Enrollment, Section
    from backend.apps.courses.selectors import active_delivery_groups_for_year, active_sections_for_year
    from backend.apps.scheduling.constants import (
        SECTION_PLACEMENT_INPUT_ANNUAL_TOTAL,
        SECTION_PLACEMENT_INPUT_FIXED_SEMESTER,
    )
    from backend.apps.scheduling.models import (
        AnnualPlacementLock, SectionBudgetApproval, SectionSchedule,
        TeacherPlanningCapacity, TeacherPlanningRoster,
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
    dependency_section_ids = set(Enrollment.objects.filter(
        section__academic_year_id=academic_year_id,
        section__lifecycle_status=SECTION_LIFECYCLE_ACTIVE,
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
                fixed.append(FixedPlacementDTO(section.id, schedule.timeslot_id, fixed_teacher_id))
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
                ))
        out_of_range = [
            f"{group_id}:{index}"
            for (group_id, index) in annual_locks
            if index > next(row.approved_annual_count for row in approval_rows if row.delivery_group_id == group_id)
        ]
        if out_of_range:
            raise ValueError(f"Annual placement lock(s) are outside approved annual counts: {', '.join(out_of_range)}.")

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
    ), conflict_matrix, roster
