"""Deterministic realistic-condition fixtures for student-assignment validation.

These fixtures complement, rather than replace, the uniform target-scale
benchmark. They model uneven demand and capacity plus the currently supported
student-assignment facts so regressions can be separated from legitimate unmet
demand.
"""

from __future__ import annotations

from collections import Counter
from time import perf_counter

from .dto import (
    CourseDifficultyDTO,
    CoursePrerequisiteDTO,
    FixedEnrollmentDTO,
    StudentAssignmentInputDTO,
    StudentAssignmentLockDTO,
    StudentAssignmentRequestDTO,
    StudentAssignmentScopeDTO,
    StudentAssignmentSectionDTO,
)
from .student_assignment import solve_student_assignment


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
    current_enrollment_id=None, is_in_scope=True,
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
