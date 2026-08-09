"""Pure student-to-section assignment over fixed accepted schedule context.

This module is intentionally independent of Django.  It never changes section
timing, rooms, teachers, or existing enrollments; it only recommends new
enrollment facts for a counselor-reviewed immutable run.
"""

from __future__ import annotations

from collections import defaultdict

from ortools.sat.python import cp_model

from .diagnostics import (
    NO_COMPLETE_STUDENT_ASSIGNMENT,
    STUDENT_ASSIGNMENT_HARD_PREREQUISITE_SEQUENCE_UNAVAILABLE,
    STUDENT_ASSIGNMENT_NO_ELIGIBLE_SECTION,
    STUDENT_ASSIGNMENT_SECTION_CAPACITY_EXHAUSTED,
    STUDENT_ASSIGNMENT_TIMESLOT_COLLISION,
    STUDENT_ASSIGNMENT_UNRESOLVED_REQUIRED_REQUEST,
)
from .dto import (
    StudentAssignmentDTO,
    StudentAssignmentInputDTO,
    StudentAssignmentResultDTO,
    StudentAssignmentUnmetRequestDTO,
)


IMPORTANCE_LEVELS = {
    "not_important": 0,
    "a_little_bit_important": 1,
    "important": 2,
    "really_important": 3,
    "extremely_important": 4,
}


def _outcome_name(status):
    return {
        cp_model.OPTIMAL: "optimal",
        cp_model.FEASIBLE: "feasible",
        cp_model.INFEASIBLE: "infeasible",
        cp_model.MODEL_INVALID: "model_invalid",
        cp_model.UNKNOWN: "unknown",
    }.get(status, "unknown")


def _solve_lexicographically(model, objectives, time_limit_seconds):
    """Optimize each bounded objective without allowing lower-priority tradeoff."""

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_seconds
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = 0
    for objective in objectives:
        model.Minimize(objective)
        status = solver.Solve(model)
        if status not in {cp_model.OPTIMAL, cp_model.FEASIBLE}:
            return None, status
        model.Add(objective == solver.Value(objective))
    status = solver.Solve(model)
    return (solver if status in {cp_model.OPTIMAL, cp_model.FEASIBLE} else None), status


def _validate_input(data):
    section_ids = set()
    offering_sections = defaultdict(list)
    for section in data.sections:
        if section.section_id in section_ids:
            raise ValueError(f"Duplicate student-assignment section {section.section_id}.")
        if section.semester not in {1, 2} or section.timeslot_id <= 0 or section.capacity_max < 0:
            raise ValueError(f"Section {section.section_id} lacks accepted assignment context.")
        section_ids.add(section.section_id)
        for offering_id in section.member_course_offering_ids:
            offering_sections[offering_id].append(section)
    request_ids = set()
    for request in data.requests:
        if request.request_id in request_ids:
            raise ValueError(f"Duplicate effective course request {request.request_id}.")
        request_ids.add(request.request_id)
        if request.course_offering_id not in offering_sections:
            # A cancelled/unoffered request remains in the result as an honest
            # unmet request instead of causing a cryptic model error.
            continue
    for importance in (
        data.section_utilization_balance_importance,
        data.student_semester_balance_importance,
        data.course_sequence_preferences_importance,
    ):
        if importance not in IMPORTANCE_LEVELS:
            raise ValueError("Student-assignment importance values are invalid.")
    return offering_sections


def solve_student_assignment(data: StudentAssignmentInputDTO) -> StudentAssignmentResultDTO:
    """Return the best safe recommendation, marking unmet required demand partial."""

    offering_sections = _validate_input(data)
    model = cp_model.CpModel()
    sections = {item.section_id: item for item in data.sections}
    fixed_by_section = defaultdict(list)
    fixed_slots = defaultdict(set)
    fixed_courses = defaultdict(list)
    diagnostics = []
    for enrollment in data.fixed_enrollments:
        if enrollment.section_id not in sections:
            raise ValueError(f"Fixed enrollment references inactive section {enrollment.section_id}.")
        fixed_by_section[enrollment.section_id].append(enrollment)
        fixed_slots[enrollment.student_id].add(enrollment.timeslot_id)
        fixed_courses[enrollment.student_id, enrollment.course_id].append(enrollment)
    for section_id, rows in fixed_by_section.items():
        if len(rows) > sections[section_id].capacity_max:
            raise ValueError(f"Fixed enrollments exceed capacity for section {section_id}.")

    variables = {}
    request_candidates = {}
    for request in sorted(data.requests, key=lambda item: item.request_id):
        candidates = []
        for section in offering_sections.get(request.course_offering_id, ()):
            if section.timeslot_id in fixed_slots[request.student_id]:
                continue
            variable = model.NewBoolVar(f"enroll_{request.request_id}_{section.section_id}")
            variables[request.request_id, section.section_id] = variable
            candidates.append((section, variable))
        request_candidates[request.request_id] = candidates
        if candidates:
            model.Add(sum(variable for _section, variable in candidates) <= 1)
        else:
            diagnostics.append({
                "code": STUDENT_ASSIGNMENT_NO_ELIGIBLE_SECTION,
                "request_id": request.request_id,
                "student_id": request.student_id,
                "course_id": request.course_id,
            })

    by_section = defaultdict(list)
    by_student_timeslot = defaultdict(list)
    by_student_section = defaultdict(list)
    by_student_course_semester = defaultdict(list)
    for (request_id, section_id), variable in variables.items():
        request = next(item for item in data.requests if item.request_id == request_id)
        section = sections[section_id]
        by_section[section_id].append(variable)
        by_student_timeslot[request.student_id, section.timeslot_id].append(variable)
        by_student_section[request.student_id, section_id].append(variable)
        by_student_course_semester[request.student_id, request.course_id, section.semester].append(variable)
    for section_id, rows in by_section.items():
        remaining = sections[section_id].capacity_max - len(fixed_by_section[section_id])
        model.Add(sum(rows) <= remaining)
    for rows in by_student_timeslot.values():
        model.Add(sum(rows) <= 1)
    for rows in by_student_section.values():
        # Cross-listed offerings share one physical meeting and cannot appear
        # twice on one student's roster.
        model.Add(sum(rows) <= 1)

    # Same-year hard prerequisites apply only when both courses are actually
    # assigned in this target year.  Prior completion is deliberately assumed.
    student_ids = {request.student_id for request in data.requests} | set(fixed_slots)
    fixed_semesters = {
        (student_id, course_id): {row.semester for row in rows}
        for (student_id, course_id), rows in fixed_courses.items()
    }
    hard_sequence_impossible = set()
    for edge in data.hard_prerequisites:
        for student_id in student_ids:
            prerequisite_rows = [
                (semester, variable)
                for (candidate_student, course_id, semester), variables_for_course in by_student_course_semester.items()
                if candidate_student == student_id and course_id == edge.prerequisite_id
                for variable in variables_for_course
            ]
            dependent_rows = [
                (semester, variable)
                for (candidate_student, course_id, semester), variables_for_course in by_student_course_semester.items()
                if candidate_student == student_id and course_id == edge.course_id
                for variable in variables_for_course
            ]
            prerequisite_rows.extend((semester, None) for semester in fixed_semesters.get((student_id, edge.prerequisite_id), ()))
            dependent_rows.extend((semester, None) for semester in fixed_semesters.get((student_id, edge.course_id), ()))
            for prerequisite_semester, prerequisite_variable in prerequisite_rows:
                for dependent_semester, dependent_variable in dependent_rows:
                    if prerequisite_semester == 1 and dependent_semester == 2:
                        continue
                    if prerequisite_variable is None and dependent_variable is None:
                        hard_sequence_impossible.add((student_id, edge.prerequisite_id, edge.course_id))
                    elif prerequisite_variable is None:
                        model.Add(dependent_variable == 0)
                    elif dependent_variable is None:
                        model.Add(prerequisite_variable == 0)
                    else:
                        model.Add(prerequisite_variable + dependent_variable <= 1)
    for student_id, prerequisite_id, course_id in sorted(hard_sequence_impossible):
        diagnostics.append({
            "code": STUDENT_ASSIGNMENT_HARD_PREREQUISITE_SEQUENCE_UNAVAILABLE,
            "student_id": student_id,
            "prerequisite_course_id": prerequisite_id,
            "course_id": course_id,
        })

    all_variables = list(variables.values())
    objectives = []
    mandatory = [
        variable
        for request in data.requests if request.is_mandatory
        for _section, variable in request_candidates[request.request_id]
    ]
    objectives.append(-sum(mandatory or [0]))
    for priority_tier in sorted({request.priority_tier for request in data.requests if request.is_primary}):
        rows = [
            variable
            for request in data.requests
            if request.is_primary and request.priority_tier == priority_tier
            for _section, variable in request_candidates[request.request_id]
        ]
        objectives.append(-sum(rows or [0]))
    approved_backups = [
        variable
        for request in data.requests if not request.is_primary
        for _section, variable in request_candidates[request.request_id]
    ]
    objectives.append(-sum(approved_backups or [0]))

    soft_objectives = defaultdict(list)
    # Reward the requested soft sequence only if both related courses appear.
    sequence_satisfied = []
    for preference in data.soft_sequence_preferences:
        for student_id in student_ids:
            early_s1 = list(by_student_course_semester[student_id, preference.earlier_course_id, 1])
            later_s2 = list(by_student_course_semester[student_id, preference.later_course_id, 2])
            early_fixed = 1 if 1 in fixed_semesters.get((student_id, preference.earlier_course_id), ()) else 0
            later_fixed = 1 if 2 in fixed_semesters.get((student_id, preference.later_course_id), ()) else 0
            early_expression = sum(early_s1) + early_fixed
            later_expression = sum(later_s2) + later_fixed
            if not early_s1 and not later_s2 and not (early_fixed and later_fixed):
                continue
            satisfied = model.NewBoolVar(
                f"sequence_{student_id}_{preference.earlier_course_id}_{preference.later_course_id}"
            )
            model.Add(satisfied <= early_expression)
            model.Add(satisfied <= later_expression)
            model.Add(satisfied >= early_expression + later_expression - 1)
            sequence_satisfied.append((preference, student_id, satisfied))
    sequence_level = IMPORTANCE_LEVELS[data.course_sequence_preferences_importance]
    if sequence_level:
        soft_objectives[sequence_level].append(
            -sum(item[2] for item in sequence_satisfied) if sequence_satisfied else 0
        )

    section_balance_terms = []
    by_group = defaultdict(list)
    for section in data.sections:
        by_group[section.delivery_group_id].append(section)
    for group_sections in by_group.values():
        group_sections = sorted(group_sections, key=lambda item: item.section_id)
        for index, left in enumerate(group_sections):
            for right in group_sections[index + 1:]:
                left_count = len(fixed_by_section[left.section_id]) + sum(by_section[left.section_id])
                right_count = len(fixed_by_section[right.section_id]) + sum(by_section[right.section_id])
                difference = model.NewIntVar(-max(left.capacity_max, right.capacity_max), max(left.capacity_max, right.capacity_max), f"utilization_difference_{left.section_id}_{right.section_id}")
                penalty = model.NewIntVar(0, max(left.capacity_max, right.capacity_max), f"utilization_penalty_{left.section_id}_{right.section_id}")
                model.Add(difference == left_count - right_count)
                model.AddAbsEquality(penalty, difference)
                section_balance_terms.append(penalty)
    utilization_level = IMPORTANCE_LEVELS[data.section_utilization_balance_importance]
    if utilization_level:
        soft_objectives[utilization_level].append(sum(section_balance_terms or [0]))

    semester_balance_terms = []
    for student_id in student_ids:
        requested_course_ids = {request.course_id for request in data.requests}
        semester_1 = sum(
            variable
            for course_id in requested_course_ids
            for variable in by_student_course_semester[student_id, course_id, 1]
        )
        semester_2 = sum(
            variable
            for course_id in requested_course_ids
            for variable in by_student_course_semester[student_id, course_id, 2]
        )
        semester_1 += sum(1 for row in data.fixed_enrollments if row.student_id == student_id and row.semester == 1)
        semester_2 += sum(1 for row in data.fixed_enrollments if row.student_id == student_id and row.semester == 2)
        penalty = model.NewIntVar(0, len(data.requests) + len(data.fixed_enrollments), f"semester_balance_{student_id}")
        model.AddAbsEquality(penalty, semester_1 - semester_2)
        semester_balance_terms.append(penalty)
    semester_level = IMPORTANCE_LEVELS[data.student_semester_balance_importance]
    if semester_level:
        soft_objectives[semester_level].append(sum(semester_balance_terms or [0]))
    for level in sorted(soft_objectives, reverse=True):
        objectives.append(sum(soft_objectives[level]))
    # A final opaque-ID objective makes equivalent recommendations stable.
    objectives.append(sum((request_id * 100000 + section_id) * variable for (request_id, section_id), variable in variables.items()) if variables else 0)

    solver, outcome = _solve_lexicographically(model, objectives, data.time_limit_seconds)
    if solver is None:
        diagnostics.append({"code": NO_COMPLETE_STUDENT_ASSIGNMENT})
        return StudentAssignmentResultDTO(
            status="infeasible",
            solver_outcome=_outcome_name(outcome),
            assignments=(),
            unmet_requests=tuple(
                StudentAssignmentUnmetRequestDTO(
                    request_id=item.request_id, student_id=item.student_id,
                    course_id=item.course_id, is_primary=item.is_primary,
                    is_mandatory=item.is_mandatory, assignment_basis=item.assignment_basis,
                    diagnostic_code=STUDENT_ASSIGNMENT_UNRESOLVED_REQUIRED_REQUEST,
                ) for item in data.requests
            ),
            diagnostics=tuple(diagnostics), objective_components={}, sequence_outcomes=(),
        )

    assignments = []
    assigned_request_ids = set()
    for request in sorted(data.requests, key=lambda item: item.request_id):
        for section, variable in request_candidates[request.request_id]:
            if solver.Value(variable):
                assignments.append(StudentAssignmentDTO(
                    request_id=request.request_id, student_id=request.student_id,
                    section_id=section.section_id, course_offering_id=request.course_offering_id,
                    course_id=request.course_id, semester=section.semester,
                    timeslot_id=section.timeslot_id, assignment_basis=request.assignment_basis,
                    backup_resolution_snapshot=request.backup_resolution_snapshot,
                ))
                assigned_request_ids.add(request.request_id)
                break
    unmet = []
    for request in data.requests:
        if request.request_id in assigned_request_ids:
            continue
        diagnostic_code = (
            STUDENT_ASSIGNMENT_SECTION_CAPACITY_EXHAUSTED
            if request_candidates[request.request_id]
            else STUDENT_ASSIGNMENT_NO_ELIGIBLE_SECTION
        )
        if any(
            sections[section.section_id].timeslot_id in fixed_slots[request.student_id]
            for section in offering_sections.get(request.course_offering_id, ())
        ):
            diagnostic_code = STUDENT_ASSIGNMENT_TIMESLOT_COLLISION
        unmet.append(StudentAssignmentUnmetRequestDTO(
            request_id=request.request_id, student_id=request.student_id,
            course_id=request.course_id, is_primary=request.is_primary,
            is_mandatory=request.is_mandatory, assignment_basis=request.assignment_basis,
            diagnostic_code=diagnostic_code,
        ))
    required_unmet = [item for item in unmet if item.is_mandatory or item.is_primary]
    if required_unmet:
        diagnostics.append({
            "code": STUDENT_ASSIGNMENT_UNRESOLVED_REQUIRED_REQUEST,
            "request_ids": [item.request_id for item in required_unmet],
        })
    sequence_outcomes = []
    assigned_courses = defaultdict(dict)
    for row in data.fixed_enrollments:
        assigned_courses[row.student_id][row.course_id] = row.semester
    for row in assignments:
        assigned_courses[row.student_id][row.course_id] = row.semester
    for preference in data.soft_sequence_preferences:
        for student_id, courses in assigned_courses.items():
            if preference.earlier_course_id in courses and preference.later_course_id in courses:
                sequence_outcomes.append({
                    "student_id": student_id,
                    "earlier_course_id": preference.earlier_course_id,
                    "later_course_id": preference.later_course_id,
                    "satisfied": courses[preference.earlier_course_id] == 1 and courses[preference.later_course_id] == 2,
                })
    return StudentAssignmentResultDTO(
        status="complete" if not required_unmet and not hard_sequence_impossible else "partial",
        solver_outcome=_outcome_name(outcome),
        assignments=tuple(assignments), unmet_requests=tuple(unmet), diagnostics=tuple(diagnostics),
        objective_components={
            "mandatory_fulfilled": float(sum(1 for row in assignments if next(item for item in data.requests if item.request_id == row.request_id).is_mandatory)),
            "primary_fulfilled": float(sum(1 for row in assignments if next(item for item in data.requests if item.request_id == row.request_id).is_primary)),
            "approved_backup_fulfilled": float(sum(1 for row in assignments if not next(item for item in data.requests if item.request_id == row.request_id).is_primary)),
            "section_utilization_balance_penalty": float(sum(solver.Value(item) for item in section_balance_terms)),
            "student_semester_balance_penalty": float(sum(solver.Value(item) for item in semester_balance_terms)),
            "soft_sequence_preferences_satisfied": float(sum(item["satisfied"] for item in sequence_outcomes)),
        },
        sequence_outcomes=tuple(sequence_outcomes),
    )
