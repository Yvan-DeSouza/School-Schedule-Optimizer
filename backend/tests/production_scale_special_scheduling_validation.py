"""Explicitly-invoked production-scale scheduling validation.

This file intentionally does not use the normal ``test_*.py`` filename
pattern.  It creates and approves a real 1,400-student planning year through
the normal reviewed services, so it belongs in release-validation runs rather
than every fast developer test command.  Invoke it explicitly with:

``pytest -q backend/tests/production_scale_special_scheduling_validation.py``

All rows are created in pytest's isolated database.  The harness prepares
catalogue and source-request data directly, but it never manufactures final
sections, placements, staffing, enrollments, or commitment records; those are
created only by the production approval services under test.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal
from itertools import permutations
import os
import subprocess
import sys
from time import perf_counter

from django.contrib.auth.models import User
import pytest

from backend.apps.common.constants import (
    COURSE_REQUEST_TYPE_PRIMARY,
    GRADE_LEVEL_12,
    QUALIFICATION_DIVISION_SENIOR,
    QUALIFICATION_REVIEW_VERIFIED,
)
from backend.apps.common.models import AcademicYear
from backend.apps.constraints.conflict_matrix import create_course_conflict_matrix
from backend.apps.constraints.models import (
    CourseQualificationRequirement,
    Qualification,
    TeacherAvailability,
    TeacherQualification,
)
from backend.apps.courses.constants import (
    COURSE_ALLOWED_SEMESTER_1_ONLY,
    COURSE_ALLOWED_SEMESTER_2_ONLY,
    COURSE_ALLOWED_SEMESTER_EITHER,
    COURSE_DELIVERY_KIND_ONLINE,
    COURSE_DELIVERY_KIND_CO_OP,
    COURSE_DURATION_FULL_SEMESTER,
)
from backend.apps.courses.models import (
    Course,
    CourseRequest,
    Enrollment,
    HalfSemesterCoursePair,
    HalfSemesterSectionPair,
    Section,
    StudentScheduleCommitmentRequest,
)
from backend.apps.courses.services.offerings import ensure_academic_year_offerings
from backend.apps.people.models import Student, Teacher
from backend.apps.scheduling.constants import (
    CO_OP_BLOCK_PAIR_A_B,
    CO_OP_BLOCK_PAIR_C_D,
    STUDENT_ASSIGNMENT_RUN_SCOPE_SCOPED,
    STUDENT_ASSIGNMENT_STAFFING_MODE_FINAL_STAFFING,
)
from backend.apps.scheduling.models import (
    CapacityProfile,
    CoursePriorityProfile,
    OnlineEnrollment,
    OnlineSupervisionConfiguration,
    OnlineSupervisionSession,
    SectionSchedule,
    StudentAssignmentApprovalEnrollment,
    StudentScheduleCommitment,
    StudentScheduleCommitmentOccupancy,
    TeacherPlanningAnnualCapacity,
    TeacherPlanningCapacity,
    TeacherPlanningRoster,
    TimeSlot,
)
from backend.apps.scheduling.services.online_supervision import (
    approve_online_supervision_plan_run,
    create_online_supervision_plan_run,
)
from backend.apps.scheduling.services.section_budget_planning import (
    approve_section_budget_run,
    create_section_budget_run,
)
from backend.apps.scheduling.services.section_placement import (
    approve_section_placement_run,
    create_section_placement_run,
)
from backend.apps.scheduling.services.staffing_configuration import (
    confirm_roster_ready,
    set_roster_members,
)
from backend.apps.scheduling.services.staffing_planning import (
    approve_staffing_plan_run,
    create_staffing_plan_run,
)
from backend.apps.scheduling.services.student_assignment import (
    approve_student_assignment_run,
    create_student_assignment_run,
    preview_student_assignment_approval,
)
from backend.apps.scheduling.services.student_assignment_locks import (
    create_student_assignment_lock,
    release_student_assignment_lock,
)
from backend.apps.scheduling.services.student_special_commitment_locks import (
    create_student_special_commitment_lock,
    release_student_special_commitment_lock,
)
from backend.apps.scheduling.services.teacher_assignment import (
    approve_teacher_assignment_run,
    create_teacher_assignment_run,
)


STUDENT_COUNT = 1400
RANDOM_SEED = 20260812
NORMAL_COURSE_COUNT_PER_SEMESTER = 20
TEACHER_COUNT = 60
SUPERVISOR_COUNT = 5
QUALIFICATION_GROUP_COUNT = 11

SOFT_IMPORTANCE = {
    "section_utilization_balance": "important",
    "student_semester_balance": "important",
    "course_sequence_preferences": "important",
    "difficulty_balance": "important",
    "course_category_diversity": "important",
}


@dataclass(frozen=True)
class SimulationSummary:
    """Comparable, opaque-ID-free facts from one isolated planning year."""

    student_count: int
    request_count: int
    normal_section_count: int
    online_session_count: int
    pipeline_status: str
    stopped_at: str | None
    placement_solver_outcome: str | None
    enrollment_count: int
    online_enrollment_count: int
    commitment_count: int
    review_code_counts: tuple[tuple[str, int], ...]
    student_assignment_status: str
    student_assignment_solver_outcome: str
    student_assignment_assignment_count: int
    student_assignment_unmet_count: int
    student_assignment_optimization_facts: dict
    schedule_fingerprint: tuple[tuple[str, str, int, str, str], ...]
    stage_seconds: tuple[tuple[str, float], ...]


def _elapsed(stage_seconds, name, action):
    """Record wall-clock service time without changing production behavior."""

    started = perf_counter()
    value = action()
    stage_seconds[name] = round(perf_counter() - started, 3)
    # This explicit long-running validation is normally invoked with ``-s``.
    # Progress facts make a timeout diagnosable without changing solver limits.
    print(f"[production-scale] {name}: {stage_seconds[name]:.3f}s", flush=True)
    return value


def _normal_course_selection(courses, *, index, semester, count):
    """Use realistic pathway cohorts instead of an artificial complete graph.

    Real students select related course bundles.  Giving every student a
    pseudo-random choice from every catalogue course made the annual conflict
    matrix a dense all-course graph that does not resemble the school's
    pathway-based demand and overwhelms the legitimate placement objective.
    Each four-course cohort still has strong meaningful co-request conflicts.
    """

    cohort_size = 4
    cohort_count = len(courses) // cohort_size
    cohort_index = (index * (3 if semester == 1 else 2) + RANDOM_SEED) % cohort_count
    start = cohort_index * cohort_size
    rotation = (index + semester) % cohort_size
    cohort = courses[start:start + cohort_size]
    return tuple(cohort[(rotation + step) % cohort_size] for step in range(count))


def _bulk_students(*, academic_year, prefix):
    """Create identities efficiently; scheduling services only receive real model rows."""

    users = [
        User(username=f"{prefix}-student-{index:04d}", password="!")
        for index in range(STUDENT_COUNT)
    ]
    User.objects.bulk_create(users, batch_size=500)
    return Student.objects.bulk_create([
        Student(
            user=user,
            student_number=f"{prefix.upper()}-{index:04d}",
            email=f"{prefix}-student-{index:04d}@example.test",
            first_name="Scale",
            last_name=f"Student {index:04d}",
            date_of_birth="2008-01-01",
            grade_level=GRADE_LEVEL_12,
            academic_year=academic_year,
        )
        for index, user in enumerate(users)
    ], batch_size=500)


def _bulk_teachers(*, prefix, qualifications):
    """Make a constrained but ample senior-teacher roster with real credentials."""

    users = [
        User(username=f"{prefix}-teacher-{index:03d}", password="!")
        for index in range(TEACHER_COUNT)
    ]
    User.objects.bulk_create(users, batch_size=200)
    teachers = Teacher.objects.bulk_create([
        Teacher(
            user=user,
            first_name="Scale",
            last_name=f"Teacher {index:03d}",
            email=f"{prefix}-teacher-{index:03d}@example.test",
            department="Production-scale validation",
            max_courses_per_semester=4,
            max_courses_total=8,
        )
        for index, user in enumerate(users)
    ], batch_size=200)
    # Each qualification group has five eligible teachers. The first five
    # teachers remain intentionally unqualified supervisors, proving that
    # online supervision consumes workload without pretending to be subject
    # teaching. Narrower qualification groups reduce artificial witness
    # symmetry while remaining a realistic senior-school staffing constraint.
    qualifications_by_teacher = []
    for index, teacher in enumerate(teachers):
        if index < SUPERVISOR_COUNT:
            continue
        qualification = qualifications[(index - SUPERVISOR_COUNT) // 5]
        qualifications_by_teacher.append(TeacherQualification(
            teacher=teacher,
            qualification=qualification,
            review_status=QUALIFICATION_REVIEW_VERIFIED,
        ))
    TeacherQualification.objects.bulk_create(qualifications_by_teacher, batch_size=500)
    return teachers


def _create_catalogue(*, academic_year, prefix, profile, priority):
    """Create the small, real-school catalogue needed by the source requests."""

    categories = ("math", "science", "language", "technology", "arts")
    qualifications = [
        Qualification.objects.create(
            code=f"{prefix}-qualification-{index}",
            name=f"{prefix} senior qualification group {index}",
            kind="teachable",
            subject_code=categories[index % len(categories)],
            division=QUALIFICATION_DIVISION_SENIOR,
        )
        for index in range(QUALIFICATION_GROUP_COUNT)
    ]
    qualification_by_course = {}
    semester_courses = {1: [], 2: []}
    normal_courses = []
    for semester in (1, 2):
        for index in range(NORMAL_COURSE_COUNT_PER_SEMESTER):
            category = categories[index % len(categories)]
            course = Course.objects.create(
                course_code=f"{prefix.upper()}-{semester}{index:02d}",
                name=f"Scale {category.title()} {semester}-{index:02d}",
                grade_level=GRADE_LEVEL_12,
                category=category,
                capacity_profile=profile,
                priority_profile=priority,
                capacity_min=profile.hard_min,
                capacity_max=profile.hard_max,
                allowed_semester=f"semester_{semester}_only",
                manual_difficulty_override=40 + (index % 5) * 10,
            )
            semester_courses[semester].append(course)
            normal_courses.append(course)
            qualification_by_course[course.id] = qualifications[index % 10]
    half_first = Course.objects.create(
        course_code=f"{prefix.upper()}-HALF-1",
        name="Scale first-half trimester",
        grade_level=GRADE_LEVEL_12,
        category="arts",
        capacity_profile=profile,
        priority_profile=priority,
        capacity_min=profile.hard_min,
        capacity_max=profile.hard_max,
        allowed_semester="semester_1_only",
        duration="half_semester",
        credit_value=Decimal("0.5"),
        manual_difficulty_override=55,
    )
    half_second = Course.objects.create(
        course_code=f"{prefix.upper()}-HALF-2",
        name="Scale second-half trimestre",
        grade_level=GRADE_LEVEL_12,
        category="technology",
        capacity_profile=profile,
        priority_profile=priority,
        capacity_min=profile.hard_min,
        capacity_max=profile.hard_max,
        allowed_semester="semester_1_only",
        duration="half_semester",
        credit_value=Decimal("0.5"),
        manual_difficulty_override=70,
    )
    HalfSemesterCoursePair.objects.create(first_course=half_first, second_course=half_second)
    qualification_by_course[half_first.id] = qualifications[10]
    qualification_by_course[half_second.id] = qualifications[10]
    online_courses = [
        Course.objects.create(
            course_code=f"{prefix.upper()}-ONLINE-{index}",
            name=f"Scale online {category.title()}",
            grade_level=GRADE_LEVEL_12,
            category=category,
            capacity_profile=profile,
            priority_profile=priority,
            capacity_min=profile.hard_min,
            capacity_max=profile.hard_max,
            delivery_kind="online",
            manual_difficulty_override=45 + index * 8,
        )
        for index, category in enumerate(categories[:4], start=1)
    ]
    online_half = Course.objects.create(
        course_code=f"{prefix.upper()}-ONLINE-HALF",
        name="Scale online first-half trimester",
        grade_level=GRADE_LEVEL_12,
        category="arts",
        capacity_profile=profile,
        priority_profile=priority,
        capacity_min=profile.hard_min,
        capacity_max=profile.hard_max,
        delivery_kind="online",
        duration="half_semester",
        credit_value=Decimal("0.5"),
        allowed_semester="semester_2_only",
        manual_difficulty_override=65,
    )
    co_op = Course.objects.create(
        course_code=f"{prefix.upper()}-COOP",
        name="Scale Co-op",
        grade_level=GRADE_LEVEL_12,
        category="",
        capacity_profile=profile,
        priority_profile=priority,
        capacity_min=profile.hard_min,
        capacity_max=profile.hard_max,
        delivery_kind="co_op",
        credit_value=Decimal("2.0"),
        manual_difficulty_override=50,
    )
    CourseQualificationRequirement.objects.bulk_create([
        CourseQualificationRequirement(course=course, qualification=qualification_by_course[course.id])
        for course in (*normal_courses, half_first, half_second)
    ])
    return {
        "semester_courses": semester_courses,
        "normal_courses": normal_courses,
        "half_first": half_first,
        "half_second": half_second,
        "online_courses": online_courses,
        "online_half": online_half,
        "co_op": co_op,
        "qualifications": qualifications,
    }


def _create_requests(*, academic_year, students, catalogue):
    """Create varied but model-valid source demand, never final assignments.

    The ranges deliberately skew toward ordinary schedules while exercising
    every accepted special-commitment path.  All students have a realistic
    eight-block annual demand once Co-op, Focus, Study, and sequential halves
    are considered; unpaired normal half-courses remain intentionally reviewable.
    """

    course_requests = []
    commitment_requests = []
    by_profile = Counter()
    special_request_ids = defaultdict(dict)

    def add_course_requests(student, courses):
        for course in courses:
            course_requests.append(CourseRequest(
                student=student,
                academic_year=academic_year,
                course=course,
                request_type=COURSE_REQUEST_TYPE_PRIMARY,
                is_mandatory=True,
            ))

    for index, student in enumerate(students):
        sem_one = list(_normal_course_selection(
            catalogue["semester_courses"][1], index=index, semester=1, count=4,
        ))
        sem_two = list(_normal_course_selection(
            catalogue["semester_courses"][2], index=index, semester=2, count=4,
        ))
        normal_eight = sem_one + sem_two
        if index < 850:
            by_profile["normal"] += 1
            add_course_requests(student, normal_eight)
        elif index < 960:
            by_profile["one_online"] += 1
            add_course_requests(student, normal_eight[:-1] + [catalogue["online_courses"][index % 4]])
        elif index < 1040:
            by_profile["two_online"] += 1
            add_course_requests(student, normal_eight[:-2] + [
                catalogue["online_courses"][index % 4],
                catalogue["online_courses"][(index + 1) % 4],
            ])
        elif index < 1090:
            by_profile["study_online"] += 1
            # Three fixed-semester courses in each term leave one genuine
            # candidate block for the requested Study and one online course.
            add_course_requests(student, sem_one[:3] + sem_two[:3] + [catalogue["online_courses"][index % 4]])
            commitment_requests.append(StudentScheduleCommitmentRequest(
                student=student, academic_year=academic_year, commitment_type="study", request_index=1,
            ))
        elif index < 1120:
            by_profile["two_study"] += 1
            add_course_requests(student, sem_one[:3] + sem_two[:3])
            commitment_requests.extend([
                StudentScheduleCommitmentRequest(
                    student=student, academic_year=academic_year, commitment_type="study", request_index=1,
                ),
                StudentScheduleCommitmentRequest(
                    student=student, academic_year=academic_year, commitment_type="study", request_index=2,
                ),
            ])
        elif index < 1160:
            by_profile["focus"] += 1
            focus_semester = 1 if index % 2 == 0 else 2
            local_courses = sem_two if focus_semester == 1 else sem_one
            add_course_requests(student, local_courses)
            commitment_requests.append(StudentScheduleCommitmentRequest(
                student=student, academic_year=academic_year, commitment_type="focus", request_index=1,
            ))
            special_request_ids[student.id]["focus_semester"] = focus_semester
        elif index < 1180:
            by_profile["focus_online"] += 1
            focus_semester = 1 if index % 2 == 0 else 2
            local_courses = (sem_two if focus_semester == 1 else sem_one)[:3]
            add_course_requests(student, local_courses + [catalogue["online_courses"][index % 4]])
            commitment_requests.append(StudentScheduleCommitmentRequest(
                student=student, academic_year=academic_year, commitment_type="focus", request_index=1,
            ))
            special_request_ids[student.id]["focus_semester"] = focus_semester
        elif index < 1250:
            by_profile["co_op"] += 1
            add_course_requests(student, normal_eight[:-2] + [catalogue["co_op"]])
        elif index < 1290:
            by_profile["co_op_online"] += 1
            add_course_requests(student, normal_eight[:-3] + [catalogue["co_op"], catalogue["online_courses"][index % 4]])
        elif index < 1340:
            by_profile["paired_half"] += 1
            # The paired half-courses share one Semester 1 block sequentially,
            # so this student has three simultaneous S1 courses, not five.
            add_course_requests(student, sem_one[:3] + sem_two + [catalogue["half_first"], catalogue["half_second"]])
        elif index < 1365:
            by_profile["unpaired_half"] += 1
            # Keep this an intentionally unpaired but physically valid
            # request: three full-semester Semester-1 courses leave one
            # Semester-1 block available for the single half-semester course,
            # while all four Semester-2 courses remain requested.  The missing
            # partner is still surfaced for counselor review; it must not make
            # the mandatory timetable itself impossible.
            add_course_requests(student, sem_one[:3] + sem_two + [
                catalogue["half_first"] if index % 2 == 0 else catalogue["half_second"],
            ])
        elif index < 1385:
            by_profile["online_half"] += 1
            add_course_requests(student, normal_eight[:-1] + [catalogue["online_half"]])
        else:
            by_profile["co_op_two_online_study"] += 1
            add_course_requests(student, normal_eight[:3] + [
                catalogue["co_op"],
                catalogue["online_courses"][index % 4],
                catalogue["online_courses"][(index + 1) % 4],
            ])
            commitment_requests.append(StudentScheduleCommitmentRequest(
                student=student, academic_year=academic_year, commitment_type="study", request_index=1,
            ))

    CourseRequest.objects.bulk_create(course_requests, batch_size=1000)
    StudentScheduleCommitmentRequest.objects.bulk_create(commitment_requests, batch_size=500)
    return by_profile, special_request_ids


def _prepare_staffing(*, academic_year, teachers, timeslots, counselor_user):
    """Use the real ready-roster contract with finite slot-based capacity."""

    for teacher in teachers:
        for semester in (1, 2):
            TeacherPlanningCapacity.objects.create(
                teacher=teacher,
                academic_year=academic_year,
                semester=semester,
                maximum_sections=4,
            )
        TeacherPlanningAnnualCapacity.objects.create(
            teacher=teacher,
            academic_year=academic_year,
            maximum_sections=8,
        )
    unavailable_pairs = []
    denied_slots = (
        timeslots[(1, "A")], timeslots[(1, "B")],
        timeslots[(2, "A")], timeslots[(2, "B")],
    )
    for index, teacher in enumerate(teachers):
        # Availability is default-on in production. One explicit recurring
        # denial per teacher gives this scale scenario real availability facts
        # without making the staffing certificate artificially under-supplied.
        unavailable_pairs.append(TeacherAvailability(
            teacher=teacher,
            timeslot=denied_slots[index % len(denied_slots)],
            is_available=False,
        ))
    TeacherAvailability.objects.bulk_create(unavailable_pairs, batch_size=200)
    roster = TeacherPlanningRoster.objects.create(academic_year=academic_year)
    set_roster_members(roster, teacher_ids=[teacher.id for teacher in teachers], actor=counselor_user)
    confirm_roster_ready(roster, actor=counselor_user)
    return roster


def _create_timeslots(*, academic_year):
    return {
        (semester, block): TimeSlot.objects.create(
            academic_year=academic_year,
            semester=semester,
            block=block,
        )
        for semester in (1, 2)
        for block in ("A", "B", "C", "D")
    }


def _special_request_rows(*, academic_year):
    course_requests = {
        (row.student_id, row.course.delivery_kind): []
        for row in CourseRequest.objects.filter(academic_year=academic_year).select_related("course")
    }
    for row in CourseRequest.objects.filter(academic_year=academic_year).select_related("course").order_by("id"):
        course_requests[row.student_id, row.course.delivery_kind].append(row)
    commitment_requests = defaultdict(list)
    for row in StudentScheduleCommitmentRequest.objects.filter(academic_year=academic_year).order_by("id"):
        commitment_requests[row.student_id].append(row)
    return course_requests, commitment_requests


def _co_op_lock_target(*, course_request, academic_year):
    """Choose one exact fixture target outside a student's forced local term.

    The production-scale fixture must exercise a real hard Co-op lock, but it
    must not manufacture a counselor decision that is already impossible: four
    full-semester local requests restricted to one term occupy A-D before
    Co-op's paired A+B/C+D commitment can be considered.
    """

    forced_request_counts = {
        semester: CourseRequest.objects.filter(
            student=course_request.student,
            academic_year=academic_year,
            is_mandatory=True,
            course__duration=COURSE_DURATION_FULL_SEMESTER,
            course__allowed_semester=(
                COURSE_ALLOWED_SEMESTER_1_ONLY
                if semester == 1
                else COURSE_ALLOWED_SEMESTER_2_ONLY
            ),
        ).exclude(course__delivery_kind=COURSE_DELIVERY_KIND_CO_OP).count()
        for semester in (1, 2)
    }
    semester = min(forced_request_counts, key=lambda item: (forced_request_counts[item], item))
    assert forced_request_counts[semester] < 4
    return semester, CO_OP_BLOCK_PAIR_A_B if semester == 2 else CO_OP_BLOCK_PAIR_C_D


def _target_leaves_local_full_courses(*, student, academic_year, target_timeslot):
    """Return whether placed local full courses can avoid one locked block.

    Exact Study and online-supervision locks reserve a student block without
    selecting ordinary sections.  The fixture must therefore choose targets
    that leave an actual block-level matching for the student's local,
    semester-restricted full-semester requests.  This is a test-data validity
    check against accepted placement, not a second scheduling algorithm.
    """

    local_requests = tuple(CourseRequest.objects.filter(
        student=student,
        academic_year=academic_year,
        is_mandatory=True,
        course__duration=COURSE_DURATION_FULL_SEMESTER,
    ).exclude(
        course__delivery_kind__in=(COURSE_DELIVERY_KIND_CO_OP, COURSE_DELIVERY_KIND_ONLINE),
    ).select_related("course").order_by("id"))
    semester_course_ids = [
        request.course_id
        for request in local_requests
        if request.course.allowed_semester in {
            None,
            COURSE_ALLOWED_SEMESTER_EITHER,
            (
                COURSE_ALLOWED_SEMESTER_1_ONLY
                if target_timeslot.semester == 1
                else COURSE_ALLOWED_SEMESTER_2_ONLY
            ),
        }
    ]
    if len(semester_course_ids) > 3:
        return False
    if not semester_course_ids:
        return True

    blocks_by_course = defaultdict(set)
    for section in Section.objects.filter(
        academic_year=academic_year,
        lifecycle_status="active",
        course_id__in=semester_course_ids,
    ).select_related("sectionschedule__timeslot"):
        schedule = getattr(section, "sectionschedule", None)
        if schedule is not None and schedule.timeslot_id:
            if schedule.timeslot.semester == target_timeslot.semester:
                blocks_by_course[section.course_id].add(schedule.timeslot.block)

    allowed_blocks = tuple(
        block for block in ("A", "B", "C", "D")
        if block != target_timeslot.block
    )
    return any(
        all(
            block in blocks_by_course[course_id]
            for course_id, block in zip(semester_course_ids, block_order)
        )
        for block_order in permutations(allowed_blocks, len(semester_course_ids))
    )


def _create_special_locks(*, academic_year, timeslots, counselor_user, focus_semesters):
    """Create exact and exclusion locks only after real session placement exists."""

    by_delivery, commitments = _special_request_rows(academic_year=academic_year)
    online_slots = list(OnlineSupervisionSession.objects.filter(
        academic_year=academic_year,
        lifecycle_status="active",
        timeslot__isnull=False,
    ).order_by("id").values_list("timeslot_id", flat=True))
    assert len(set(online_slots)) >= 2, "Scale fixture must create distinct online supervision blocks."
    ordered_timeslots = tuple(timeslots.values())
    timeslots_by_id = {slot.id: slot for slot in timeslots.values()}
    locks = []
    for student_id, rows in sorted(commitments.items()):
        for row in rows:
            if row.commitment_type == "study":
                # Keep the lock sample on the two-Study profile.  The
                # Study+Online profile remains in the demand population, but
                # coupling an exact Study block to its shared supervision
                # choice makes the scale fixture unnecessarily sensitive to a
                # valid upstream placement permutation.
                if CourseRequest.objects.filter(
                    student=row.student,
                    academic_year=academic_year,
                    is_mandatory=True,
                    course__delivery_kind=COURSE_DELIVERY_KIND_ONLINE,
                ).exists():
                    continue
                if len(locks) >= 4:
                    continue
                lock_mode = "exact" if len(locks) % 2 == 0 else "exclude"
                target_timeslot_id = ordered_timeslots[len(locks) % len(ordered_timeslots)].id
                if lock_mode == "exact":
                    legal_study_slots = [
                        slot.id
                        for slot in timeslots.values()
                        if _target_leaves_local_full_courses(
                            student=row.student,
                            academic_year=academic_year,
                            target_timeslot=slot,
                        )
                    ]
                    assert legal_study_slots, (
                        f"No legal exact Study-time slot exists for student {row.student_id}."
                    )
                    target_timeslot_id = legal_study_slots[(len(locks) // 2) % len(legal_study_slots)]
                locks.append(create_student_special_commitment_lock(
                    academic_year=academic_year,
                    created_by=counselor_user,
                    lock_type="study_time",
                    lock_mode=lock_mode,
                    schedule_commitment_request=row,
                    timeslot_id=target_timeslot_id,
                    reason="Exercise reviewed Study-time restriction at production scale.",
                ))
            elif row.commitment_type == "focus":
                # Keep a distinct set of Focus locks even though Study rows
                # sort earlier in this scale population.
                if sum(lock.lock_type == "focus_semester" for lock in locks) >= 12:
                    continue
                focus_semester = focus_semesters[row.student_id]["focus_semester"]
                # Excluding the opposite semester is the equivalent hard
                # restriction to an exact focus-semester lock.  Keep both lock
                # modes in the fixture while preserving the student's known
                # external-program semester.
                is_exact = len(locks) % 2 == 0
                locks.append(create_student_special_commitment_lock(
                    academic_year=academic_year,
                    created_by=counselor_user,
                    lock_type="focus_semester",
                    lock_mode="exact" if is_exact else "exclude",
                    schedule_commitment_request=row,
                    semester=focus_semester if is_exact else 3 - focus_semester,
                    reason="Exercise reviewed Focus-semester restriction at production scale.",
                ))
    online_rows = [row for (_student_id, delivery), rows in by_delivery.items() if delivery == "online" for row in rows]
    co_op_rows = [row for (_student_id, delivery), rows in by_delivery.items() if delivery == "co_op" for row in rows]
    for index, row in enumerate(online_rows[:12]):
        if index < 6:
            # An exact supervision-time lock is a hard student decision. The
            # first locked requests are intentionally one-online profiles,
            # whose four mandatory Semester-1 courses leave supervision only
            # in Semester 2. Select an already-placed session in a semester
            # with a real free block instead of manufacturing an impossible
            # counselor lock by taking an arbitrary session ID.
            legal_online_slots = [
                slot_id for slot_id in online_slots
                if _target_leaves_local_full_courses(
                    student=row.student,
                    academic_year=academic_year,
                    target_timeslot=timeslots_by_id[slot_id],
                )
            ]
            assert legal_online_slots, (
                f"No legal exact online supervision slot exists for student {row.student_id}."
            )
            target_timeslot_id = legal_online_slots[index % len(legal_online_slots)]
        else:
            target_timeslot_id = online_slots[index % len(online_slots)]
        locks.append(create_student_special_commitment_lock(
            academic_year=academic_year,
            created_by=counselor_user,
            lock_type="online_supervision_time",
            lock_mode="exact" if index < 6 else "exclude",
            course_request=row,
            timeslot_id=target_timeslot_id,
            reason="Exercise reviewed online-supervision time restriction at production scale.",
        ))
    for index, row in enumerate(co_op_rows[:12]):
        semester, co_op_block_pair = _co_op_lock_target(
            course_request=row,
            academic_year=academic_year,
        )
        is_exact = index < 6
        locks.append(create_student_special_commitment_lock(
            academic_year=academic_year,
            created_by=counselor_user,
            lock_type="co_op_time",
            lock_mode="exact" if is_exact else "exclude",
            course_request=row,
            semester=semester if is_exact else 3 - semester,
            co_op_block_pair=co_op_block_pair,
            reason="Exercise reviewed Co-op time restriction at production scale.",
        ))
    return locks


def _occupancy_rows(*, academic_year):
    """Yield every active approved student-time fact in one common half-slot shape."""

    schedules = dict(SectionSchedule.objects.filter(
        section__academic_year=academic_year,
    ).values_list("section_id", "timeslot_id"))
    for enrollment in Enrollment.objects.filter(
        section__academic_year=academic_year,
        lifecycle_status="active",
    ).select_related("section"):
        segments = (enrollment.section.half_semester_segment,) if enrollment.section.half_semester_segment else (
            "first_half", "second_half",
        )
        for segment in segments:
            yield enrollment.student_id, schedules[enrollment.section_id], segment, "normal"
    for enrollment in OnlineEnrollment.objects.filter(
        supervision_session__academic_year=academic_year,
        lifecycle_status="active",
    ):
        for segment in ("first_half", "second_half"):
            yield enrollment.student_id, enrollment.supervision_session.timeslot_id, segment, "online"
    for occupancy in StudentScheduleCommitmentOccupancy.objects.filter(
        commitment__academic_year=academic_year,
        commitment__lifecycle_status="active",
    ).select_related("commitment"):
        yield occupancy.commitment.student_id, occupancy.timeslot_id, occupancy.half_semester_segment, occupancy.commitment.commitment_kind


def _assert_persisted_invariants(*, academic_year, review):
    """Check approved facts, rather than merely trusting a complete solver status."""

    occupancy = defaultdict(list)
    for student_id, timeslot_id, segment, source in _occupancy_rows(academic_year=academic_year):
        occupancy[student_id].append((timeslot_id, segment, source))
    collisions = {
        student_id: values
        for student_id, values in occupancy.items()
        if len({(timeslot_id, segment) for timeslot_id, segment, _source in values}) != len(values)
    }
    assert not collisions, f"Student-time collisions found: {dict(list(collisions.items())[:3])}"

    assert not Section.objects.filter(
        academic_year=academic_year,
        course__delivery_kind__in=("online", "co_op"),
    ).exists()
    assert not Enrollment.objects.filter(
        section__academic_year=academic_year,
        course_offering__course__delivery_kind__in=("online", "co_op"),
        lifecycle_status="active",
    ).exists()
    assert not StudentScheduleCommitment.objects.filter(
        academic_year=academic_year,
        commitment_kind__in=("study", "focus", "co_op"),
        lifecycle_status="active",
        course_offering__delivery_group__isnull=False,
    ).exists()

    for session in OnlineSupervisionSession.objects.filter(academic_year=academic_year):
        assert session.supervisor_id is not None
        assert session.timeslot_id is not None
        assert session.online_enrollments.filter(lifecycle_status="active").count() <= session.capacity_max
    assert not OnlineEnrollment.objects.filter(
        lifecycle_status="active",
    ).exclude(course_offering__course__delivery_kind="online").exists()

    occupied_by_student = defaultdict(set)
    for student_id, timeslot_id, segment, _source in _occupancy_rows(academic_year=academic_year):
        occupied_by_student[student_id].add((timeslot_id, segment))
    for commitment in StudentScheduleCommitment.objects.filter(
        academic_year=academic_year,
        commitment_kind="focus",
        lifecycle_status="active",
    ):
        slots = StudentScheduleCommitmentOccupancy.objects.filter(commitment=commitment)
        assert slots.count() == 8
    for commitment in StudentScheduleCommitment.objects.filter(
        academic_year=academic_year,
        commitment_kind="co_op",
        lifecycle_status="active",
    ):
        values = set(commitment.occupancies.values_list("timeslot__semester", "timeslot__block"))
        assert values in ({(1, "A"), (1, "B")}, {(1, "C"), (1, "D")}, {(2, "A"), (2, "B")}, {(2, "C"), (2, "D")})
    for pair in HalfSemesterSectionPair.objects.filter(first_section__academic_year=academic_year).select_related("first_section", "second_section"):
        assert pair.first_section.teacher_id == pair.second_section.teacher_id
        assert SectionSchedule.objects.get(section=pair.first_section).timeslot_id == SectionSchedule.objects.get(section=pair.second_section).timeslot_id

    review_codes = Counter(item["code"] for item in review["special_commitment_review_items"])
    assert review_codes["student_assignment_half_semester_unallocated_opposite_half"] > 0
    assert review_codes["student_assignment_online_half_semester_unused_supervision_half"] > 0
    return review_codes


def _schedule_fingerprint(*, academic_year):
    """Compare independent runs by source identity, never database primary keys."""

    rows = []
    for enrollment in Enrollment.objects.filter(
        section__academic_year=academic_year,
        lifecycle_status="active",
    ).select_related("student", "course_offering__course", "section__sectionschedule__timeslot"):
        slot = enrollment.section.sectionschedule.timeslot
        rows.append((
            enrollment.student.student_number.split("-")[-1],
            enrollment.course_offering.course.course_code.split("-")[-1],
            slot.semester,
            slot.block,
            enrollment.section.half_semester_segment or "full_semester",
        ))
    for enrollment in OnlineEnrollment.objects.filter(
        supervision_session__academic_year=academic_year,
        lifecycle_status="active",
    ).select_related("student", "course_offering__course", "supervision_session__timeslot"):
        slot = enrollment.supervision_session.timeslot
        rows.append((
            enrollment.student.student_number.split("-")[-1],
            enrollment.course_offering.course.course_code.split("-")[-1],
            slot.semester,
            slot.block,
            "online_supervision",
        ))
    return tuple(sorted(rows))


def _run_controlled_reruns(*, academic_year, approval, counselor_user):
    """Exercise history and lock semantics against approved operational state."""

    def _rerun_step(name, action):
        """Expose post-approval rerun progress without changing workflow."""

        started = perf_counter()
        print(f"[production-scale] {name}: starting", flush=True)
        value = action()
        print(
            f"[production-scale] {name}: {perf_counter() - started:.3f}s",
            flush=True,
        )
        return value

    # The prior implementation materialized every related field for every
    # active enrollment before selecting one target.  At production scale that
    # made the validation harness spend many minutes transferring objects that
    # the rerun only needs to identify by primary key.  Selecting the first
    # equivalent target in SQL preserves the exact ordering/selection rule and
    # leaves the actual target fully loaded for the lock workflow below.
    target_id = Enrollment.objects.filter(
        section__academic_year=academic_year,
        lifecycle_status="active",
        section__teacher__isnull=False,
    ).order_by("id").values_list("id", flat=True).first()
    assert target_id is not None, "The approved schedule must contain a named-teacher enrollment."
    target = Enrollment.objects.select_related(
        "student", "course_offering__course", "section__teacher"
    ).get(id=target_id)
    alternatives = list(Section.objects.filter(
        academic_year=academic_year,
        delivery_group=target.section.delivery_group,
        lifecycle_status="active",
    ).exclude(id=target.section_id).select_related("teacher", "sectionschedule").order_by("id"))
    assert alternatives, "Each high-demand normal offering needs an alternate section for rerun validation."
    alternate = next((row for row in alternatives if row.teacher_id == target.section.teacher_id), alternatives[0])

    exact_lock = create_student_assignment_lock(
        academic_year=academic_year,
        lock_type="exact_student_section",
        created_by=counselor_user,
        reason="Move one reviewed student to an alternate valid physical section.",
        student=target.student,
        section=alternate,
        course=target.course_offering.course,
        staffing_mode=STUDENT_ASSIGNMENT_STAFFING_MODE_FINAL_STAFFING,
    )
    rerun = _rerun_step("rerun_exact_lock", lambda: create_student_assignment_run(
        academic_year=academic_year,
        staffing_mode=STUDENT_ASSIGNMENT_STAFFING_MODE_FINAL_STAFFING,
        soft_constraint_importance=SOFT_IMPORTANCE,
        created_by=counselor_user,
        scope_type=STUDENT_ASSIGNMENT_RUN_SCOPE_SCOPED,
        source_approval=approval,
        scope_student_ids=(target.student_id,),
        schedule_preservation_level="strong",
    ))
    rerun_approval = _rerun_step("rerun_exact_lock_approval", lambda: approve_student_assignment_run(
        rerun,
        approved_by=counselor_user,
        reason="Approve one controlled, locked enrollment replacement.",
    ))
    assert rerun.status == "complete", rerun.result
    target.refresh_from_db()
    assert target.lifecycle_status == "historical"
    replacement = StudentAssignmentApprovalEnrollment.objects.get(
        approval=rerun_approval,
        superseded_enrollment=target,
    ).enrollment
    assert replacement.section_id == alternate.id

    whole_schedule_lock = create_student_assignment_lock(
        academic_year=academic_year,
        lock_type="whole_student_schedule",
        created_by=counselor_user,
        reason="Protect the reviewed replacement schedule during a later scoped run.",
        student=target.student,
        staffing_mode=STUDENT_ASSIGNMENT_STAFFING_MODE_FINAL_STAFFING,
    )
    teacher_lock = create_student_assignment_lock(
        academic_year=academic_year,
        lock_type="student_teacher_course",
        created_by=counselor_user,
        reason="Keep the approved course with its current final teacher.",
        student=target.student,
        course=replacement.course_offering.course,
        teacher=replacement.section.teacher,
        staffing_mode=STUDENT_ASSIGNMENT_STAFFING_MODE_FINAL_STAFFING,
    )
    protected_run = _rerun_step("rerun_whole_schedule_lock", lambda: create_student_assignment_run(
        academic_year=academic_year,
        staffing_mode=STUDENT_ASSIGNMENT_STAFFING_MODE_FINAL_STAFFING,
        soft_constraint_importance=SOFT_IMPORTANCE,
        created_by=counselor_user,
        scope_type=STUDENT_ASSIGNMENT_RUN_SCOPE_SCOPED,
        source_approval=rerun_approval,
        scope_student_ids=(target.student_id,),
    ))
    assert protected_run.status == "complete", protected_run.result
    assert any(
        item["reason_code"] == "student_assignment_locked_enrollment_blocks_request"
        for item in preview_student_assignment_approval(protected_run)["protected_assignments"]
    )
    release_student_assignment_lock(
        whole_schedule_lock,
        released_by=counselor_user,
        release_reason="Reopen this one schedule after controlled validation.",
    )
    # The exact lock remains active.  Releasing a lock is append-only and a
    # fresh run proves its current selection rather than approving stale work.
    released_run = _rerun_step("rerun_released_lock", lambda: create_student_assignment_run(
        academic_year=academic_year,
        staffing_mode=STUDENT_ASSIGNMENT_STAFFING_MODE_FINAL_STAFFING,
        soft_constraint_importance=SOFT_IMPORTANCE,
        created_by=counselor_user,
        scope_type=STUDENT_ASSIGNMENT_RUN_SCOPE_SCOPED,
        source_approval=rerun_approval,
        scope_student_ids=(target.student_id,),
    ))
    assert released_run.status == "complete", released_run.result
    return {
        "scoped_runs": 3,
        "historical_enrollments": Enrollment.objects.filter(
            section__academic_year=academic_year,
            lifecycle_status="historical",
        ).count(),
        "active_locks": [exact_lock.id, teacher_lock.id],
    }


def run_production_scale_special_scheduling_validation(*, counselor_user, prefix, include_reruns):
    """Create one 1,400-student complete pipeline scenario and validate it."""

    stage_seconds = {}
    # Demand forecasting deliberately accepts only the real ``YYYY-YYYY``
    # school-year label.  Two valid years make the repeatability replay
    # independent without weakening that production input contract.
    academic_year = AcademicYear.objects.create(
        name="2035-2036" if prefix == "scale-a" else "2036-2037"
    )
    profile = CapacityProfile.objects.create(
        name=f"{prefix} normal capacity",
        hard_min=1,
        soft_min=24,
        target=35,
        soft_max=38,
        hard_max=40,
    )
    online_profile = CapacityProfile.objects.create(
        name=f"{prefix} online supervision capacity",
        hard_min=1,
        soft_min=30,
        target=35,
        soft_max=38,
        hard_max=40,
    )
    priority = CoursePriorityProfile.objects.create(name=f"{prefix} core priority", tier=1)
    catalogue = _create_catalogue(
        academic_year=academic_year,
        prefix=prefix,
        profile=profile,
        priority=priority,
    )
    teachers = _bulk_teachers(prefix=prefix, qualifications=catalogue["qualifications"])
    students = _bulk_students(academic_year=academic_year, prefix=prefix)
    profile_counts, focus_semesters = _create_requests(
        academic_year=academic_year,
        students=students,
        catalogue=catalogue,
    )
    timeslots = _create_timeslots(academic_year=academic_year)
    _prepare_staffing(
        academic_year=academic_year,
        teachers=teachers,
        timeslots=timeslots,
        counselor_user=counselor_user,
    )
    ensure_academic_year_offerings(academic_year, actor=counselor_user)
    OnlineSupervisionConfiguration.objects.create(
        academic_year=academic_year,
        capacity_profile=online_profile,
        updated_by=counselor_user,
    )

    online_run = _elapsed(stage_seconds, "online_supervision_planning", lambda: create_online_supervision_plan_run(
        academic_year=academic_year, created_by=counselor_user,
    ))
    assert online_run.status == "complete", online_run.result
    _elapsed(stage_seconds, "online_supervision_approval", lambda: approve_online_supervision_plan_run(
        online_run, approved_by=counselor_user, reason="Approve scale shared supervision capacity.",
    ))
    budget_run = _elapsed(stage_seconds, "section_budget", lambda: create_section_budget_run(
        academic_year=academic_year,
        created_by=counselor_user,
        budget_type="ceiling",
        section_budget=400,
        backup_policy="ignore",
        backup_overrides=(),
        offering_constraints=(),
    ))
    assert budget_run.status == "complete", budget_run.result
    budget_approval = _elapsed(stage_seconds, "section_budget_approval", lambda: approve_section_budget_run(
        budget_run, approved_by=counselor_user, reason="Approve demand-based scale section counts.",
    ))
    staffing_run = _elapsed(stage_seconds, "staffing_planning", lambda: create_staffing_plan_run(
        academic_year=academic_year,
        created_by=counselor_user,
        budget_approval=budget_approval,
        backup_policy="ignore",
        backup_overrides=(),
        offering_constraints=(),
        teacher_capacity_adjustments=(),
    ))
    assert staffing_run.status == "complete", staffing_run.result
    _elapsed(stage_seconds, "staffing_approval", lambda: approve_staffing_plan_run(
        staffing_run, approved_by=counselor_user, reason="Approve staff-feasible scale normal sections.",
    ))
    print(
        "[production-scale] prepared "
        f"students={STUDENT_COUNT} requests={CourseRequest.objects.filter(academic_year=academic_year).count()} "
        f"normal_sections={Section.objects.filter(academic_year=academic_year, lifecycle_status='active').count()} "
        f"online_sessions={OnlineSupervisionSession.objects.filter(academic_year=academic_year, lifecycle_status='active').count()}",
        flush=True,
    )
    create_course_conflict_matrix(
        academic_year=academic_year,
        initialization_mode="fresh_current_demand",
        actor=counselor_user,
    )
    placement_run = _elapsed(stage_seconds, "section_placement", lambda: create_section_placement_run(
        academic_year_id=academic_year.id,
        input_mode="fixed_semester",
        created_by=counselor_user,
    ))
    if placement_run.status != "complete":
        # A production-scale validation must distinguish a real hard
        # infeasibility from a solver that found no incumbent. Persisted run
        # status intentionally remains non-approvable; this harness returns a
        # structured finding rather than manufacturing downstream state.
        assert placement_run.result["solver_outcome"] == "unknown", placement_run.result
        return SimulationSummary(
            student_count=STUDENT_COUNT,
            request_count=CourseRequest.objects.filter(academic_year=academic_year).count(),
            normal_section_count=Section.objects.filter(academic_year=academic_year, lifecycle_status="active").count(),
            online_session_count=OnlineSupervisionSession.objects.filter(academic_year=academic_year, lifecycle_status="active").count(),
            pipeline_status="blocked",
            stopped_at="section_placement",
            placement_solver_outcome=placement_run.result["solver_outcome"],
            enrollment_count=0,
            online_enrollment_count=0,
            commitment_count=0,
            review_code_counts=(),
            student_assignment_status="blocked",
            student_assignment_solver_outcome="unknown",
            student_assignment_assignment_count=0,
            student_assignment_unmet_count=0,
            student_assignment_optimization_facts={},
            schedule_fingerprint=(),
            stage_seconds=tuple(sorted(stage_seconds.items())),
        )
    _elapsed(stage_seconds, "placement_approval", lambda: approve_section_placement_run(
        placement_run, approved_by=counselor_user, reason="Approve scale Semester and A-D placement.",
    ))
    assert OnlineSupervisionSession.objects.filter(academic_year=academic_year, timeslot__isnull=False).count() > 1
    teacher_run = _elapsed(stage_seconds, "teacher_assignment", lambda: create_teacher_assignment_run(
        academic_year_id=academic_year.id, created_by=counselor_user,
    ))
    assert teacher_run.status == "complete", teacher_run.result
    _elapsed(stage_seconds, "teacher_approval", lambda: approve_teacher_assignment_run(
        teacher_run, approved_by=counselor_user, reason="Approve scale named teachers and supervisors.",
    ))
    _create_special_locks(
        academic_year=academic_year,
        timeslots=timeslots,
        counselor_user=counselor_user,
        focus_semesters=focus_semesters,
    )
    student_run = _elapsed(stage_seconds, "student_assignment", lambda: create_student_assignment_run(
        academic_year=academic_year,
        staffing_mode=STUDENT_ASSIGNMENT_STAFFING_MODE_FINAL_STAFFING,
        soft_constraint_importance=SOFT_IMPORTANCE,
        created_by=counselor_user,
    ))
    assert student_run.status == "complete", student_run.result
    student_result = student_run.result
    print(
        "[production-scale] student result: "
        f"status={student_result.get('status')} "
        f"solver_outcome={student_result.get('solver_outcome')} "
        f"assignments={len(student_result.get('assignments', ()))} "
        f"unmet={len(student_result.get('unmet_requests', ()))}",
        flush=True,
    )
    print(
        "[production-scale] student optimization facts: "
        f"{student_result.get('optimization_facts', {})}",
        flush=True,
    )
    print(
        "[production-scale] student objective components: "
        f"{student_result.get('objective_components', {})}",
        flush=True,
    )
    review = _elapsed(stage_seconds, "student_review", lambda: preview_student_assignment_approval(student_run))
    approval = _elapsed(stage_seconds, "student_approval", lambda: approve_student_assignment_run(
        student_run,
        approved_by=counselor_user,
        reason="Approve reviewed production-scale student schedule.",
    ))
    review_codes = _assert_persisted_invariants(academic_year=academic_year, review=review)
    rerun_summary = _run_controlled_reruns(
        academic_year=academic_year,
        approval=approval,
        counselor_user=counselor_user,
    ) if include_reruns else {"scoped_runs": 0, "historical_enrollments": 0, "active_locks": []}

    assert Student.objects.filter(academic_year=academic_year).count() == STUDENT_COUNT
    assert profile_counts["normal"] > sum(value for key, value in profile_counts.items() if key != "normal")
    assert Section.objects.filter(academic_year=academic_year, lifecycle_status="active").count() > 250
    assert OnlineEnrollment.objects.filter(
        supervision_session__academic_year=academic_year,
        lifecycle_status="active",
    ).count() > 400
    assert StudentScheduleCommitment.objects.filter(
        academic_year=academic_year,
        lifecycle_status="active",
    ).count() > 150
    return SimulationSummary(
        student_count=STUDENT_COUNT,
        request_count=CourseRequest.objects.filter(academic_year=academic_year).count(),
        normal_section_count=Section.objects.filter(academic_year=academic_year, lifecycle_status="active").count(),
        online_session_count=OnlineSupervisionSession.objects.filter(academic_year=academic_year, lifecycle_status="active").count(),
        pipeline_status="complete",
        stopped_at=None,
        placement_solver_outcome=placement_run.result.get("solver_outcome"),
        enrollment_count=Enrollment.objects.filter(section__academic_year=academic_year, lifecycle_status="active").count(),
        online_enrollment_count=OnlineEnrollment.objects.filter(supervision_session__academic_year=academic_year, lifecycle_status="active").count(),
        commitment_count=StudentScheduleCommitment.objects.filter(academic_year=academic_year, lifecycle_status="active").count(),
        review_code_counts=tuple(sorted(review_codes.items())),
        student_assignment_status=student_result.get("status"),
        student_assignment_solver_outcome=student_result.get("solver_outcome"),
        student_assignment_assignment_count=len(student_result.get("assignments", ())),
        student_assignment_unmet_count=len(student_result.get("unmet_requests", ())),
        student_assignment_optimization_facts=student_result.get("optimization_facts", {}),
        schedule_fingerprint=_schedule_fingerprint(academic_year=academic_year),
        stage_seconds=tuple(sorted(stage_seconds.items())),
    )


def _assert_complete_production_scale_summary(summary):
    """Validate one scenario without requiring identical parallel schedules."""

    assert summary.student_count == STUDENT_COUNT
    assert summary.request_count == 10760
    assert summary.normal_section_count == 304
    assert summary.online_session_count == 13
    assert summary.pipeline_status == "complete"
    assert summary.stopped_at is None
    assert summary.student_assignment_status == "complete"
    assert summary.student_assignment_unmet_count == 0
    facts = summary.student_assignment_optimization_facts
    stage_1 = facts["stage_1"]
    stage_2 = facts["stage_2"]
    assert stage_1["complete_seed_produced"] is True
    assert stage_1["seed_validated_against_full_model"] is True
    assert stage_1["solver_outcome"] in {"optimal", "feasible"}
    assert stage_2["validated_seed_received"] is True
    assert stage_2["worker_count"] == 8
    assert stage_2["time_limit_seconds"] == 1800.0
    assert stage_2["solver_outcome"] in {"optimal", "feasible", "unknown"}
    seed_vector = tuple(stage_1["objective_values"])
    final_vector = tuple(stage_2["objective_values"])
    assert seed_vector
    assert final_vector <= seed_vector
    assert stage_2["improved_over_stage_1"] is (final_vector < seed_vector)


@pytest.mark.django_db
def test_production_scale_special_scheduling_scenario(counselor_user):
    """Run one isolated production-scale scenario in a child process.

    Each full CP-SAT schedule can retain substantial native memory until its
    Python process exits. The release validation intentionally runs repeated
    independent scenarios in separate pytest processes, so the second result
    measures a clean production run rather than resource state left by the
    first. This changes no scheduling input, rule, objective, or workflow.
    """

    prefix = os.environ.get("SCHEDULING_PRODUCTION_SCALE_PREFIX")
    if prefix not in {"scale-a", "scale-b"}:
        pytest.skip(
            "the isolated scenario is invoked by the parent release-validation test"
        )
    summary = run_production_scale_special_scheduling_validation(
        counselor_user=counselor_user,
        prefix=prefix,
        include_reruns=prefix == "scale-a",
    )
    _assert_complete_production_scale_summary(summary)


def test_production_scale_special_scheduling_validation():
    """Run two independent full production-scale scenarios.

    The first scenario includes controlled reruns against accepted state; the
    second replays the initial pipeline. Independent parallel CP-SAT runs may
    choose different valid schedules, so validation is performed inside each
    isolated child against hard validity, completeness, and objective facts.
    """

    test_path = os.path.abspath(__file__)
    scenario_node = f"{test_path}::test_production_scale_special_scheduling_scenario"
    for prefix in ("scale-a", "scale-b"):
        child_environment = os.environ.copy()
        child_environment["SCHEDULING_PRODUCTION_SCALE_PREFIX"] = prefix
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "--create-db",
                "-q",
                "-s",
                scenario_node,
            ],
            cwd=os.path.dirname(os.path.dirname(os.path.dirname(test_path))),
            env=child_environment,
            check=False,
        )
        assert completed.returncode == 0, (
            f"isolated production-scale scenario {prefix} failed with "
            f"exit code {completed.returncode}"
        )
