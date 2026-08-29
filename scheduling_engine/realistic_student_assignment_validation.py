"""Deterministic realistic-condition fixtures for student-assignment validation.

These fixtures complement, rather than replace, the uniform target-scale
benchmark. They model uneven demand and capacity plus the currently supported
student-assignment facts so regressions can be separated from legitimate unmet
demand.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
from statistics import mean, median
from time import perf_counter

from .dto import (
    CourseCategoryRelationshipDTO,
    CourseDifficultyDTO,
    CoursePrerequisiteDTO,
    CourseSequencePreferenceDTO,
    FixedEnrollmentDTO,
    OnlineSupervisionSessionDTO,
    StudentAssignmentInputDTO,
    StudentAssignmentLockDTO,
    StudentAssignmentRequestDTO,
    StudentAssignmentScopeDTO,
    StudentAssignmentSectionDTO,
    StudentScheduleCommitmentRequestDTO,
    StudentSpecialCommitmentLockDTO,
    TimeSlotDTO,
)
from .student_assignment import solve_student_assignment
from .student_assignment.runtime import semantic_student_assignment_input_fingerprint


def _section(
    section_id, course_id, semester, timeslot_id, capacity_max, *, target_capacity=None,
):
    return StudentAssignmentSectionDTO(
        section_id=section_id,
        delivery_group_id=course_id,
        member_course_offering_ids=(1000 + course_id,),
        member_course_ids=(course_id,),
        semester=semester,
        timeslot_id=timeslot_id,
        capacity_max=capacity_max,
        target_capacity=target_capacity if target_capacity is not None else capacity_max,
    )


def _request(
    request_id, student_id, course_id, *, is_mandatory=False,
    is_primary=True, priority_tier=4, assignment_basis="primary_request",
    current_enrollment_id=None, is_in_scope=True, delivery_kind="normal_instruction",
    duration="full_semester", credit_value=1.0, half_semester_segment=None,
    paired_half_course_id=None,
):
    return StudentAssignmentRequestDTO(
        request_id=request_id,
        student_id=student_id,
        course_id=course_id,
        course_offering_id=1000 + course_id,
        is_primary=is_primary,
        is_mandatory=is_mandatory,
        priority_tier=priority_tier,
        assignment_basis=assignment_basis,
        current_enrollment_id=current_enrollment_id,
        is_in_scope=is_in_scope,
        delivery_kind=delivery_kind,
        duration=duration,
        credit_value=credit_value,
        half_semester_segment=half_semester_segment,
        paired_half_course_id=paired_half_course_id,
    )


def build_realistic_validation_fixture() -> StudentAssignmentInputDTO:
    """Build a small, inspectable school scenario with deliberate edge cases.

    Course 5 is oversubscribed, course 6 has no placed section, and student 3
    has an approved backup request. Student 4 has a protected existing course,
    while student 5 has an exact course/section lock. Student 6 demonstrates a
    scoped preservation rerun. Courses 2 and 3 form a same-year prerequisite
    pair and therefore intentionally disable the independent-request seed.
    """

    sections = (
        _section(1, 1, 1, 1, 3, target_capacity=2),
        _section(2, 1, 2, 5, 2),
        _section(3, 2, 1, 2, 3),
        _section(4, 2, 1, 4, 2),
        _section(5, 3, 2, 6, 3),
        _section(6, 4, 1, 3, 1),
        _section(7, 5, 1, 4, 3),
        _section(8, 7, 2, 7, 1),
    )
    return StudentAssignmentInputDTO(
        academic_year_id=1,
        requests=(
            # Student 1 must receive the prerequisite in Semester 1 before
            # the dependent course in Semester 2.
            _request(1, 1, 2, is_mandatory=True, priority_tier=1),
            _request(2, 1, 3, is_mandatory=True, priority_tier=1),
            # Student 2 competes for a deliberately undersupplied practical.
            _request(3, 2, 5, is_mandatory=True, priority_tier=1),
            # Student 3 has an approved alternate, not a free-form fallback.
            _request(4, 3, 5, is_mandatory=True, priority_tier=1),
            _request(5, 3, 7, is_primary=False, assignment_basis="approved_backup"),
            # Course 6 is offered nowhere, so this primary is legitimately unmet.
            _request(6, 4, 6, is_mandatory=True, priority_tier=1),
            # The exact lock keeps student 5 in the only permitted practical.
            _request(7, 5, 5, is_mandatory=True, priority_tier=1),
            # This request may change only inside the scoped rerun and should
            # remain in its prior section when preservation is strong.
            _request(8, 6, 1, is_mandatory=True, priority_tier=1,
                     current_enrollment_id=601, is_in_scope=True),
            # This student has a same-timeslot pair; the lock below fixes the
            # first request in A-D slot 4, so the practical cannot also fit.
            _request(9, 7, 2, is_mandatory=True, priority_tier=1),
            _request(10, 7, 5, is_mandatory=True, priority_tier=1),
        ),
        sections=sections,
        fixed_enrollments=(
            # Protected existing enrollment: it consumes one seat and must not
            # become a decision variable or an unmet request.
            FixedEnrollmentDTO(
                enrollment_id=401,
                student_id=4,
                section_id=1,
                course_offering_id=1001,
                course_id=1,
                semester=1,
                timeslot_id=1,
                is_locked=True,
                lock_ids=(901,),
            ),
            # Historical rows are realistic audit facts but do not consume a
            # seat, a timeslot, or a decision variable.
            FixedEnrollmentDTO(
                enrollment_id=402,
                student_id=8,
                section_id=1,
                course_offering_id=1001,
                course_id=1,
                semester=1,
                timeslot_id=1,
                is_historical=True,
            ),
            FixedEnrollmentDTO(
                enrollment_id=601,
                student_id=6,
                section_id=2,
                course_offering_id=1001,
                course_id=1,
                semester=2,
                timeslot_id=5,
                is_in_scope=True,
            ),
        ),
        hard_prerequisites=(CoursePrerequisiteDTO(course_id=3, prerequisite_id=2),),
        soft_sequence_preferences=(),
        section_utilization_balance_importance="important",
        student_semester_balance_importance="important",
        course_sequence_preferences_importance="not_important",
        student_assignment_locks=(StudentAssignmentLockDTO(
            lock_id=902,
            lock_type="exact_student_section",
            student_id=5,
            course_id=5,
            section_id=7,
        ), StudentAssignmentLockDTO(
            lock_id=903,
            lock_type="exact_student_section",
            student_id=7,
            course_id=2,
            section_id=4,
        )),
        schedule_preservation_level="strong",
        scope=StudentAssignmentScopeDTO(
            scope_type="scoped",
            student_ids=(1, 2, 3, 4, 5, 6, 7),
        ),
        time_limit_seconds=10.0,
    )


def build_realistic_scale_fixture(*, student_count=1400) -> StudentAssignmentInputDTO:
    """Build a 1,400-student, uneven-demand 300-section validation problem.

    Ten high-demand courses have eight larger sections, twenty medium-demand
    courses have six standard sections, and twenty low-demand courses have five
    smaller sections. Each student's two high-, three medium-, and two
    low-demand requests create uneven per-course demand while retaining a
    one-seat course-level buffer for the smallest offerings.
    """

    sections = []
    section_id = 1
    for course_id in range(1, 51):
        if course_id <= 10:
            section_count = 8
            capacities = (41, 41, 41, 41, 43, 43, 43, 43)
        elif course_id <= 30:
            section_count = 6
            capacities = (35, 35, 35, 37, 37, 37)
        else:
            section_count = 5
            capacities = (27, 28, 28, 29, 29)
        for index in range(section_count):
            timeslot_id = 1 + ((course_id * 3 + index) % 8)
            sections.append(_section(
                section_id,
                course_id,
                1 if timeslot_id <= 4 else 2,
                timeslot_id,
                capacities[index],
                target_capacity=max(1, capacities[index] - 4),
            ))
            section_id += 1

    requests = []
    request_id = 1
    for student_id in range(1, student_count + 1):
        course_ids = (
            1 + ((student_id - 1) % 10),
            1 + ((student_id + 4) % 10),
            11 + ((student_id * 3) % 20),
            11 + ((student_id * 3 + 7) % 20),
            11 + ((student_id * 3 + 14) % 20),
            31 + ((student_id - 1) % 20),
            31 + ((student_id + 9) % 20),
        )
        for index, course_id in enumerate(course_ids):
            requests.append(_request(
                request_id,
                student_id,
                course_id,
                is_mandatory=index < 4,
                priority_tier=1 if index < 4 else 4,
            ))
            request_id += 1
    return StudentAssignmentInputDTO(
        academic_year_id=1,
        requests=tuple(requests),
        sections=tuple(sections),
        fixed_enrollments=(),
        hard_prerequisites=(),
        soft_sequence_preferences=(),
        section_utilization_balance_importance="important",
        student_semester_balance_importance="important",
        course_sequence_preferences_importance="not_important",
        time_limit_seconds=30.0,
    )


def build_production_shaped_medium_fixture(
    *, student_count=240, special_profile_cycle=100
) -> StudentAssignmentInputDTO:
    """Build a practical mixed fixture shaped from the school-scale input.

    This is intentionally a DTO-level diagnostic fixture rather than a second
    scheduling engine.  Its normal sections and request distribution start
    from ``build_realistic_scale_fixture``; a small deterministic cohort is
    then given the special commitment patterns that matter at production
    scale.  The special cohort uses fewer ordinary requests so Focus and
    Co-op remain legitimate commitments instead of creating an intentionally
    contradictory fixture.

    ``special_profile_cycle`` is a diagnostic-only control for changing the
    density of the same defined special cohorts.  It does not add new
    commitment types or alter their construction.  The fixture is used for
    comparative Stage 2 experiments only.  It does not change the
    production-scale Django fixture or ordinary scheduling defaults.
    """

    if student_count < 80:
        raise ValueError("The production-shaped medium fixture needs at least 80 students.")
    if special_profile_cycle < 13:
        raise ValueError("The special profile cycle must include every defined profile.")

    base = build_realistic_scale_fixture(student_count=student_count)
    requests_by_student = {}
    for request in base.requests:
        requests_by_student.setdefault(request.student_id, []).append(request)

    requests = []
    commitment_requests = []
    special_locks = []
    next_request_id = 1
    next_commitment_id = 100000

    def add_commitment(student_id, commitment_type, *, exact_semester=None,
                       excluded_timeslot=None, co_op_pair=None):
        nonlocal next_commitment_id
        commitment_id = next_commitment_id
        next_commitment_id += 1
        commitment_requests.append(
            StudentScheduleCommitmentRequestDTO(
                request_id=commitment_id,
                student_id=student_id,
                commitment_type=commitment_type,
            )
        )
        if exact_semester is not None:
            special_locks.append(StudentSpecialCommitmentLockDTO(
                lock_id=200000 + commitment_id,
                lock_type=("focus_semester" if commitment_type == "focus" else "study_time"),
                lock_mode="exact",
                schedule_commitment_request_id=commitment_id,
                semester=exact_semester,
            ))
        if excluded_timeslot is not None:
            special_locks.append(StudentSpecialCommitmentLockDTO(
                lock_id=200000 + commitment_id,
                lock_type="study_time",
                lock_mode="exclude",
                schedule_commitment_request_id=commitment_id,
                timeslot_id=excluded_timeslot,
            ))
        if co_op_pair is not None:
            special_locks.append(StudentSpecialCommitmentLockDTO(
                lock_id=200000 + commitment_id,
                lock_type="co_op_time",
                lock_mode="exact",
                course_request_id=commitment_id,
                co_op_block_pair=co_op_pair,
            ))
        return commitment_id

    def add_course(student_id, course_id, *, mandatory=True, delivery_kind="normal_instruction",
                   duration="full_semester", credit_value=1.0,
                   half_semester_segment=None, paired_half_course_id=None):
        nonlocal next_request_id
        requests.append(_request(
            next_request_id,
            student_id,
            course_id,
            is_mandatory=mandatory,
            priority_tier=1 if mandatory else 4,
            delivery_kind=delivery_kind,
            duration=duration,
            credit_value=credit_value,
            half_semester_segment=half_semester_segment,
            paired_half_course_id=paired_half_course_id,
        ))
        next_request_id += 1
        return next_request_id - 1

    for student_id in range(1, student_count + 1):
        base_rows = requests_by_student[student_id]
        profile = student_id % special_profile_cycle
        # Most students preserve the full uneven seven-request pattern from
        # the existing realistic scale fixture.  The special cohorts retain
        # enough ordinary demand to create real collisions without making a
        # mandatory Focus or Co-op decision contradictory by construction.
        if profile in {0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12}:
            ordinary_rows = base_rows[:2]
        else:
            ordinary_rows = base_rows
        for row in ordinary_rows:
            add_course(
                student_id,
                row.course_id,
                mandatory=row.is_mandatory,
            )

        if profile == 0:
            add_commitment(student_id, "focus", exact_semester=2)
        elif profile == 1:
            course_request_id = add_course(
                student_id, 56, delivery_kind="co_op", credit_value=2.0,
            )
            # The lock targets the Co-op course request, not the separate
            # schedule-commitment request namespace.
            special_locks.append(StudentSpecialCommitmentLockDTO(
                lock_id=300000 + student_id,
                lock_type="co_op_time",
                lock_mode="exact",
                course_request_id=course_request_id,
                semester=2,
                co_op_block_pair="c_d",
            ))
        elif profile == 2:
            add_commitment(student_id, "study", excluded_timeslot=1)
        elif profile == 3:
            add_course(student_id, 53, delivery_kind="online")
            add_course(student_id, 54, delivery_kind="online")
        elif profile == 4:
            add_course(
                student_id, 51, duration="half_semester",
                half_semester_segment="first_half", paired_half_course_id=52,
            )
            add_course(
                student_id, 52, duration="half_semester",
                half_semester_segment="second_half", paired_half_course_id=51,
            )
        elif profile == 5:
            add_course(
                student_id, 51, duration="half_semester",
                half_semester_segment="first_half", paired_half_course_id=52,
            )
        elif profile == 6:
            course_request_id = add_course(
                student_id, 56, delivery_kind="co_op", credit_value=2.0,
            )
            add_course(student_id, 53, delivery_kind="online")
            special_locks.append(StudentSpecialCommitmentLockDTO(
                lock_id=300000 + student_id,
                lock_type="co_op_time",
                lock_mode="exclude",
                course_request_id=course_request_id,
                semester=1,
                co_op_block_pair="a_b",
            ))
        elif profile == 7:
            add_commitment(student_id, "study")
            add_course(student_id, 53, delivery_kind="online")
        elif profile == 8:
            add_commitment(student_id, "focus", exact_semester=2)
            add_course(student_id, 53, delivery_kind="online")
        elif profile == 9:
            add_course(
                student_id, 55, delivery_kind="online",
                duration="half_semester", half_semester_segment="first_half",
            )
        elif profile == 10:
            add_commitment(student_id, "study")
            add_commitment(student_id, "study")
        elif profile == 11:
            add_commitment(student_id, "study")
            add_course(student_id, 56, delivery_kind="co_op", credit_value=2.0)
        elif profile == 12:
            add_course(student_id, 53, delivery_kind="online")

    sections = list(base.sections)
    sections.extend((
        StudentAssignmentSectionDTO(
            section_id=301,
            delivery_group_id=51,
            member_course_offering_ids=(1051,),
            member_course_ids=(51,),
            semester=1,
            timeslot_id=1,
            capacity_max=max(80, student_count // 3),
            target_capacity=max(70, student_count // 4),
            half_semester_segment="first_half",
            half_semester_pair_key="medium-half-s1",
        ),
        StudentAssignmentSectionDTO(
            section_id=302,
            delivery_group_id=52,
            member_course_offering_ids=(1052,),
            member_course_ids=(52,),
            semester=1,
            timeslot_id=1,
            capacity_max=max(80, student_count // 3),
            target_capacity=max(70, student_count // 4),
            half_semester_segment="second_half",
            half_semester_pair_key="medium-half-s1",
        ),
        StudentAssignmentSectionDTO(
            section_id=303,
            delivery_group_id=51,
            member_course_offering_ids=(1051,),
            member_course_ids=(51,),
            semester=2,
            timeslot_id=5,
            capacity_max=max(80, student_count // 3),
            target_capacity=max(70, student_count // 4),
            half_semester_segment="first_half",
            half_semester_pair_key="medium-half-s2",
        ),
        StudentAssignmentSectionDTO(
            section_id=304,
            delivery_group_id=52,
            member_course_offering_ids=(1052,),
            member_course_ids=(52,),
            semester=2,
            timeslot_id=5,
            capacity_max=max(80, student_count // 3),
            target_capacity=max(70, student_count // 4),
            half_semester_segment="second_half",
            half_semester_pair_key="medium-half-s2",
        ),
    ))

    online_sessions = []
    for index, (session_id, semester, timeslot_id) in enumerate(
        ((1, 1, 2), (2, 1, 3), (3, 2, 6), (4, 2, 7)),
        start=1,
    ):
        online_sessions.append(OnlineSupervisionSessionDTO(
            session_id=session_id,
            semester=semester,
            timeslot_id=timeslot_id,
            capacity_max=max(60, student_count // 3),
            target_capacity=max(50, student_count // 4),
        ))
        sections.append(StudentAssignmentSectionDTO(
            section_id=-session_id,
            delivery_group_id=-session_id,
            member_course_offering_ids=(1053, 1054, 1055),
            member_course_ids=(53, 54, 55),
            semester=semester,
            timeslot_id=timeslot_id,
            capacity_max=max(60, student_count // 3),
            target_capacity=max(50, student_count // 4),
        ))

    timeslots = tuple(
        TimeSlotDTO(
            id=slot_id,
            academic_year_id=1,
            semester=1 if slot_id <= 4 else 2,
            block=("A", "B", "C", "D")[(slot_id - 1) % 4],
        )
        for slot_id in range(1, 9)
    )
    difficulties = tuple(
        CourseDifficultyDTO(
            course_id=course_id,
            category=("math", "science", "english", "arts")[course_id % 4],
            calculated_difficulty=20 + ((course_id * 17) % 81),
            manual_difficulty_override=None,
            effective_difficulty=20 + ((course_id * 17) % 81),
            calculation_version="production_shaped_medium_v1",
        )
        for course_id in range(1, 57)
    )

    return StudentAssignmentInputDTO(
        academic_year_id=1,
        requests=tuple(requests),
        sections=tuple(sections),
        fixed_enrollments=(),
        hard_prerequisites=(CoursePrerequisiteDTO(course_id=11, prerequisite_id=1),),
        soft_sequence_preferences=(CourseSequencePreferenceDTO(1, 11),),
        section_utilization_balance_importance="important",
        student_semester_balance_importance="important",
        course_sequence_preferences_importance="important",
        difficulty_balance_importance="important",
        course_category_diversity_importance="important",
        time_limit_seconds=30.0,
        course_difficulties=difficulties,
        course_category_relationships=(
            CourseCategoryRelationshipDTO("math", "science", 45),
            CourseCategoryRelationshipDTO("english", "arts", 30),
        ),
        online_supervision_sessions=tuple(online_sessions),
        schedule_commitment_requests=tuple(commitment_requests),
        special_commitment_locks=tuple(special_locks),
        timeslots=timeslots,
    )


def build_mixed_grade_v2_fixture(
    *, student_count=240, special_profile_cycle=100
) -> StudentAssignmentInputDTO:
    """Return a current, deterministic mixed-grade v2 study fixture.

    The existing production-shaped medium fixture deliberately exercises the
    special-commitment and section interactions.  This wrapper adds actual
    student-grade facts and opts the detached input into the current v2
    objective semantics.  It is synthetic benchmark data, not a claim about a
    particular school's grade distribution, and it never changes the
    production-scale Django fixture or the durable v1 artifact.
    """

    base = build_production_shaped_medium_fixture(
        student_count=student_count,
        special_profile_cycle=special_profile_cycle,
    )
    return apply_mixed_grade_v2_profile(base)


def apply_mixed_grade_v2_profile(
    data: StudentAssignmentInputDTO,
) -> StudentAssignmentInputDTO:
    """Return ``data`` with a deterministic synthetic mixed-grade v2 profile.

    This helper is intentionally DTO-only so a detached production-shaped
    input can be screened without mutating its source artifact.  Grade facts
    are synthetic study metadata; they are not inferred from course catalog
    grades and are not presented as real school data.
    """

    grade_facts = tuple(
        (student_id, 9 + ((student_id - 1) % 4))
        for student_id in sorted({
            request.student_id for request in data.requests
        } | {
            enrollment.student_id for enrollment in data.fixed_enrollments
        } | {
            commitment.student_id
            for commitment in data.schedule_commitment_requests
        })
    )
    return replace(
        data,
        objective_semantics_version="v2",
        objective_importance_scores={
            "section_utilization_balance": 6,
            "student_semester_balance": 6,
            "course_sequence_preferences": 6,
            "difficulty_balance": 6,
            "course_category_diversity": 6,
        },
        student_grades=grade_facts,
    )


def summarize_mixed_grade_v2_fixture(data):
    """Return benchmark identity facts for a mixed-grade v2 fixture."""

    grade_counts = {}
    for _student_id, grade_level in data.student_grades:
        grade_counts[int(grade_level)] = grade_counts.get(int(grade_level), 0) + 1
    return {
        "objective_semantics_version": data.objective_semantics_version,
        "student_count": len(data.student_grades),
        "grade_counts": dict(sorted(grade_counts.items())),
        "request_count": len(data.requests),
        "section_count": len(data.sections),
        "special_commitment_count": len(data.schedule_commitment_requests),
        "online_supervision_session_count": len(data.online_supervision_sessions),
        "input_fingerprint": semantic_student_assignment_input_fingerprint(data),
    }


def summarize_production_shaped_medium_fixture(data, result=None):
    """Return structural facts used by the Stage 2 experiment report."""

    summary = summarize_realistic_fixture(data, result)
    requests_by_student = {}
    for request in data.requests:
        requests_by_student.setdefault(request.student_id, []).append(request)
    sections_by_group = {}
    for section in data.sections:
        sections_by_group.setdefault(section.delivery_group_id, []).append(section)
    candidate_domain_sizes = []
    for request in data.requests:
        if request.delivery_kind == "co_op":
            candidate_domain_sizes.append(0)
            continue
        candidate_domain_sizes.append(sum(
            request.course_offering_id in section.member_course_offering_ids
            for section in data.sections
        ))
    request_counts = [len(rows) for rows in requests_by_student.values()]
    section_counts = [len(rows) for rows in sections_by_group.values()]
    summary.update({
        "requests_per_student_mean": round(mean(request_counts), 3),
        "requests_per_student_median": median(request_counts),
        "sections_per_delivery_group_mean": round(mean(section_counts), 3),
        "sections_per_delivery_group_median": median(section_counts),
        "candidate_domain_mean": round(mean(candidate_domain_sizes), 3),
        "candidate_domain_median": median(candidate_domain_sizes),
        "candidate_domain_maximum": max(candidate_domain_sizes, default=0),
        "capacity_to_demand_by_course": {
            str(course_id): {
                "demand": sum(request.course_id == course_id for request in data.requests),
                "capacity": sum(
                    section.capacity_max
                    for section in data.sections
                    if course_id in section.member_course_ids
                ),
            }
            for course_id in sorted({request.course_id for request in data.requests})
        },
        "online_request_count": sum(
            request.delivery_kind == "online" for request in data.requests
        ),
        "co_op_request_count": sum(
            request.delivery_kind == "co_op" for request in data.requests
        ),
        "half_semester_request_count": sum(
            request.duration == "half_semester" for request in data.requests
        ),
        "study_request_count": sum(
            request.commitment_type == "study"
            for request in data.schedule_commitment_requests
        ),
        "focus_request_count": sum(
            request.commitment_type == "focus"
            for request in data.schedule_commitment_requests
        ),
        "online_supervision_session_count": len(data.online_supervision_sessions),
        "special_lock_count": len(data.special_commitment_locks),
        "course_difficulty_count": len(data.course_difficulties),
    })
    return summary


def build_realistic_scoped_rerun_fixture(*, schedule_preservation_level="strong"):
    """Build a partial rerun after one student adds a course request.

    Student 2's existing enrollment is protected outside the selected scope.
    Student 1's course 1 enrollment may be replaced while the newly requested
    course 2 is assigned. This mirrors a counselor changing one student's
    demand without reopening another student's accepted schedule.
    """

    return StudentAssignmentInputDTO(
        academic_year_id=1,
        requests=(
            _request(1, 1, 1, is_mandatory=True, priority_tier=1,
                     current_enrollment_id=701, is_in_scope=True),
            _request(2, 1, 2, is_mandatory=True, priority_tier=1, is_in_scope=True),
            # Retained snapshot context: the engine must not rewrite it outside
            # the scoped student boundary.
            _request(3, 2, 1, is_mandatory=True, priority_tier=1, is_in_scope=False),
        ),
        sections=(
            _section(1, 1, 1, 1, 2),
            _section(2, 1, 1, 2, 2),
            _section(3, 2, 2, 5, 2),
        ),
        fixed_enrollments=(
            FixedEnrollmentDTO(
                enrollment_id=701,
                student_id=1,
                section_id=2,
                course_offering_id=1001,
                course_id=1,
                semester=1,
                timeslot_id=2,
                is_in_scope=True,
            ),
            FixedEnrollmentDTO(
                enrollment_id=702,
                student_id=2,
                section_id=1,
                course_offering_id=1001,
                course_id=1,
                semester=1,
                timeslot_id=1,
                is_locked=True,
                is_in_scope=False,
                lock_ids=(904,),
            ),
        ),
        hard_prerequisites=(),
        soft_sequence_preferences=(),
        section_utilization_balance_importance="not_important",
        student_semester_balance_importance="not_important",
        course_sequence_preferences_importance="not_important",
        schedule_preservation_level=schedule_preservation_level,
        scope=StudentAssignmentScopeDTO(scope_type="scoped", student_ids=(1,)),
        time_limit_seconds=10.0,
    )


def build_realistic_quality_tradeoff_fixture(
    *, difficulty_importance="important", course_category_diversity_importance="important",
):
    """Build an auditable counselor-quality tradeoff without relaxing context.

    A protected high-difficulty science enrollment is already in Semester 1.
    The two movable mathematics courses can share Semester 2 for a perfect
    difficulty split, or separate for a better category distribution. This
    demonstrates that counselor importance changes only a soft preference.
    """

    return StudentAssignmentInputDTO(
        academic_year_id=1,
        requests=(
            _request(1, 1, 1, is_mandatory=True),
            _request(2, 1, 2, is_mandatory=True),
        ),
        sections=(
            _section(1, 1, 1, 1, 2),
            _section(2, 1, 2, 5, 2),
            _section(3, 2, 1, 2, 2),
            _section(4, 2, 2, 6, 2),
            _section(5, 3, 1, 3, 2),
        ),
        fixed_enrollments=(FixedEnrollmentDTO(
            enrollment_id=801, student_id=1, section_id=5,
            course_offering_id=1003, course_id=3, semester=1, timeslot_id=3,
            is_locked=True, lock_ids=(950,),
        ),),
        hard_prerequisites=(),
        soft_sequence_preferences=(),
        section_utilization_balance_importance="not_important",
        student_semester_balance_importance="not_important",
        course_sequence_preferences_importance="not_important",
        difficulty_balance_importance=difficulty_importance,
        course_category_diversity_importance=course_category_diversity_importance,
        course_difficulties=(
            CourseDifficultyDTO(1, "math", 90, None, 90, "validation_v1"),
            CourseDifficultyDTO(2, "math", 10, None, 10, "validation_v1"),
            CourseDifficultyDTO(3, "science", 100, None, 100, "validation_v1"),
        ),
        time_limit_seconds=10.0,
    )


def summarize_realistic_fixture(data, result=None):
    """Return non-sensitive input facts and optional solved-result evidence."""

    summary = {
        "student_count": len({request.student_id for request in data.requests}),
        "section_count": len(data.sections),
        "request_count": len(data.requests),
        "mandatory_request_count": sum(request.is_mandatory for request in data.requests),
        "primary_request_count": sum(request.is_primary for request in data.requests),
        "approved_backup_request_count": sum(
            not request.is_primary and request.assignment_basis == "approved_backup"
            for request in data.requests
        ),
        "nominal_seat_capacity": sum(section.capacity_max for section in data.sections),
        "fixed_active_enrollment_count": sum(
            enrollment.is_active and not enrollment.is_historical
            for enrollment in data.fixed_enrollments
        ),
    }
    if result is None:
        return summary
    loads = Counter(assignment.section_id for assignment in result.assignments)
    summary.update({
        "status": result.status,
        "solver_outcome": result.solver_outcome,
        "assignment_count": len(result.assignments),
        "unmet_request_count": len(result.unmet_requests),
        "objective_components": dict(result.objective_components),
        "quality": result.optimization_facts.get("quality"),
        "optimization_passes": result.optimization_facts.get("optimization_passes", ()),
        "minimum_assigned_section_load": min(loads.values(), default=0),
        "maximum_assigned_section_load": max(loads.values(), default=0),
    })
    return summary


def run_realistic_scale_validation():
    """Solve the uneven school-scale fixture without changing production input."""

    data = build_realistic_scale_fixture()
    started = perf_counter()
    result = solve_student_assignment(data)
    summary = summarize_realistic_fixture(data, result)
    summary["elapsed_seconds"] = round(perf_counter() - started, 3)
    return summary


if __name__ == "__main__":
    for key, value in run_realistic_scale_validation().items():
        print(f"{key}: {value}")
