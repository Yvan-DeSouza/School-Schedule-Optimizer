"""Pure named-teacher assignment after counselors approve section timing.

This solver consumes a detached snapshot.  It deliberately does not model
rooms, students, or timetable placement: a section's semester/block is already
accepted fixed context, and only the teacher dimension is being decided here.
"""

from __future__ import annotations

from collections import defaultdict
from itertools import combinations

from ortools.sat.python import cp_model

from .diagnostics import (
    LOCKED_TEACHER_INELIGIBLE,
    LOCKED_TEACHER_UNAVAILABLE,
    NO_COMPLETE_TEACHER_ASSIGNMENT,
    NO_ELIGIBLE_TEACHER_FOR_SECTION,
    TEACHER_COURSE_RULE_INFEASIBLE,
)
from .dto import TeacherAssignmentDTO, TeacherAssignmentInputDTO, TeacherAssignmentResultDTO


def compile_teacher_assignment_constraints(data: TeacherAssignmentInputDTO) -> dict:
    """Validate the narrow, ORM-free contract consumed by this stage."""

    teachers = {teacher.id: teacher for teacher in data.teachers}
    section_ids = set()
    for section in data.sections:
        if section.section_id in section_ids:
            raise ValueError(f"Duplicate teacher-assignment section {section.section_id}.")
        section_ids.add(section.section_id)
        if section.semester not in {1, 2} or not section.member_course_ids:
            raise ValueError(f"Section {section.section_id} lacks valid accepted timing/course context.")
        if section.locked_teacher_id is not None and section.locked_teacher_id not in teachers:
            raise ValueError(f"Section {section.section_id} locks a teacher outside the ready roster.")
    for rule in data.rules:
        if rule.teacher_id not in teachers or rule.minimum_sections < 0:
            raise ValueError("Teacher course rule references invalid planning input.")
        if rule.maximum_sections is not None and rule.maximum_sections < rule.minimum_sections:
            raise ValueError("Teacher course rule maximum cannot be lower than its minimum.")
    return {"teachers": teachers}


def _candidates(section, teachers):
    """Return teachers legal for a section before global collision/load rules."""

    candidates = []
    for teacher in teachers.values():
        if section.locked_teacher_id is not None and teacher.id != section.locked_teacher_id:
            continue
        # Availability defaults to available.  The adapter sends only explicit
        # denial rows, preventing a sparse import from becoming a closed list.
        if section.timeslot_id in teacher.unavailable_timeslot_ids:
            continue
        if not set(section.member_course_ids).issubset(teacher.eligible_course_ids):
            continue
        if teacher.remaining_annual <= 0:
            continue
        if section.semester == 1 and teacher.remaining_semester_1 <= 0:
            continue
        if section.semester == 2 and teacher.remaining_semester_2 <= 0:
            continue
        candidates.append(teacher)
    return candidates


def solve_teacher_assignment(data: TeacherAssignmentInputDTO) -> TeacherAssignmentResultDTO:
    """Return a complete or diagnostic partial named-teacher recommendation."""

    compiled = compile_teacher_assignment_constraints(data)
    teachers = compiled["teachers"]
    model = cp_model.CpModel()
    diagnostics = []
    variables = {}
    section_candidates = {}
    decision_sections = [section for section in data.sections if not section.is_fixed]

    for section in sorted(decision_sections, key=lambda item: item.section_id):
        candidates = _candidates(section, teachers)
        section_candidates[section.section_id] = candidates
        if not candidates:
            locked = teachers.get(section.locked_teacher_id) if section.locked_teacher_id else None
            if locked and section.timeslot_id in locked.unavailable_timeslot_ids:
                diagnostics.append({"code": LOCKED_TEACHER_UNAVAILABLE, "section_id": section.section_id})
            elif locked and not set(section.member_course_ids).issubset(locked.eligible_course_ids):
                diagnostics.append({"code": LOCKED_TEACHER_INELIGIBLE, "section_id": section.section_id})
            else:
                diagnostics.append({"code": NO_ELIGIBLE_TEACHER_FOR_SECTION, "section_id": section.section_id})
        for teacher in candidates:
            variables[section.section_id, teacher.id] = model.NewBoolVar(
                f"teacher_{section.section_id}_{teacher.id}"
            )
        section_vars = [variables[section.section_id, teacher.id] for teacher in candidates]
        if section_vars:
            model.Add(sum(section_vars) <= 1)

    by_teacher_slot = defaultdict(list)
    by_teacher_semester = defaultdict(list)
    by_teacher_annual = defaultdict(list)
    for (section_id, teacher_id), variable in variables.items():
        section = next(item for item in decision_sections if item.section_id == section_id)
        by_teacher_slot[teacher_id, section.timeslot_id].append(variable)
        by_teacher_semester[teacher_id, section.semester].append(variable)
        by_teacher_annual[teacher_id].append(variable)
    fixed_slots = {(item.teacher_id, item.timeslot_id) for item in data.fixed_assignments}
    for (teacher_id, timeslot_id), rows in by_teacher_slot.items():
        model.Add(sum(rows) <= (0 if (teacher_id, timeslot_id) in fixed_slots else 1))
    for (teacher_id, semester), rows in by_teacher_semester.items():
        capacity = teachers[teacher_id].remaining_semester_1 if semester == 1 else teachers[teacher_id].remaining_semester_2
        model.Add(sum(rows) <= capacity)
    for teacher_id, rows in by_teacher_annual.items():
        model.Add(sum(rows) <= teachers[teacher_id].remaining_annual)

    # Course rules use member course codes, while workload counts the physical
    # section only once through the capacity constraints above.
    fixed_course_counts = defaultdict(int)
    for fixed in data.fixed_assignments:
        for course_id in fixed.member_course_ids:
            fixed_course_counts[fixed.teacher_id, course_id] += 1
    for rule in data.rules:
        rows = [
            variable
            for (section_id, teacher_id), variable in variables.items()
            if teacher_id == rule.teacher_id
            and rule.course_id in next(item for item in decision_sections if item.section_id == section_id).member_course_ids
        ]
        fixed_count = fixed_course_counts[rule.teacher_id, rule.course_id]
        if rule.maximum_sections is not None:
            model.Add(sum(rows or [0]) + fixed_count <= rule.maximum_sections)
        model.Add(sum(rows or [0]) + fixed_count >= rule.minimum_sections)

    assigned = list(variables.values())
    # Weighted scales encode the documented lexicographic ordering while keeping
    # one deterministic CP-SAT model.  They are facts-based counts, not hidden
    # user preference ratings.
    objective = [-1_000_000_000 * sum(assigned or [0])]
    requested, continuity, time_preference, seniority = [], [], [], []
    for (section_id, teacher_id), variable in variables.items():
        section = next(item for item in decision_sections if item.section_id == section_id)
        teacher = teachers[teacher_id]
        if set(section.member_course_ids) & set(teacher.preferred_course_ids):
            requested.append(variable)
        if set(section.member_course_ids) & set(teacher.prior_year_course_ids):
            continuity.append(variable)
        if section.timeslot_id in teacher.preferred_timeslot_ids:
            time_preference.append(variable)
        if section.timeslot_id in teacher.avoided_timeslot_ids:
            time_preference.append(-variable)
        if teacher.seniority:
            seniority.append(int(teacher.seniority) * variable)
    # Pairwise same-teacher use is a deliberately small final penalty.  It
    # spreads discretionary work across available staff without ever defeating
    # a course request, continuity, time preference, seniority, or hard rule.
    balance_terms = []
    for teacher_id in teachers:
        teacher_vars = [variable for (_section_id, candidate_teacher_id), variable in variables.items() if candidate_teacher_id == teacher_id]
        for index, (left, right) in enumerate(combinations(teacher_vars, 2)):
            together = model.NewBoolVar(f"teacher_load_pair_{teacher_id}_{index}")
            model.AddBoolAnd([left, right]).OnlyEnforceIf(together)
            model.AddBoolOr([left.Not(), right.Not(), together])
            balance_terms.append(together)
    objective.extend([
        -1_000_000 * sum(requested or [0]),
        -10_000 * sum(continuity or [0]),
        -100 * sum(time_preference or [0]),
        -sum(seniority or [0]),
        sum(balance_terms or [0]),
    ])
    model.Minimize(sum(objective))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = data.time_limit_seconds
    solver.parameters.num_search_workers = 1
    outcome = solver.Solve(model)
    outcome_name = {
        cp_model.OPTIMAL: "optimal", cp_model.FEASIBLE: "feasible",
        cp_model.INFEASIBLE: "infeasible", cp_model.MODEL_INVALID: "model_invalid",
    }.get(outcome, "unknown")
    if outcome not in {cp_model.OPTIMAL, cp_model.FEASIBLE}:
        diagnostics.append({"code": TEACHER_COURSE_RULE_INFEASIBLE if data.rules else NO_COMPLETE_TEACHER_ASSIGNMENT})
        return TeacherAssignmentResultDTO(
            status="infeasible", solver_outcome=outcome_name, assignments=(),
            unassigned_section_ids=tuple(item.section_id for item in decision_sections),
            diagnostics=tuple(diagnostics), objective_components={},
        )

    assignments = []
    for section in sorted(decision_sections, key=lambda item: item.section_id):
        for teacher in section_candidates[section.section_id]:
            variable = variables[section.section_id, teacher.id]
            if solver.Value(variable):
                assignments.append(TeacherAssignmentDTO(
                    section_id=section.section_id, teacher_id=teacher.id,
                    semester=section.semester, timeslot_id=section.timeslot_id,
                    explanation={
                        "requested_course_match": bool(set(section.member_course_ids) & set(teacher.preferred_course_ids)),
                        "prior_year_course_match": bool(set(section.member_course_ids) & set(teacher.prior_year_course_ids)),
                        "timeslot_preference": "preferred" if section.timeslot_id in teacher.preferred_timeslot_ids else ("avoid" if section.timeslot_id in teacher.avoided_timeslot_ids else "neutral"),
                        "seniority": teacher.seniority,
                    },
                ))
                break
    unassigned = tuple(section.section_id for section in decision_sections if section.section_id not in {row.section_id for row in assignments})
    if unassigned:
        diagnostics.append({"code": NO_COMPLETE_TEACHER_ASSIGNMENT, "unassigned_section_ids": list(unassigned)})
    return TeacherAssignmentResultDTO(
        status="complete" if not unassigned else "partial", solver_outcome=outcome_name,
        assignments=tuple(assignments), unassigned_section_ids=unassigned,
        diagnostics=tuple(diagnostics),
        objective_components={
            "assigned_sections": float(len(assignments)),
            "requested_course_matches": float(sum(item.explanation["requested_course_match"] for item in assignments)),
            "prior_year_course_matches": float(sum(item.explanation["prior_year_course_match"] for item in assignments)),
            "teacher_load_balance_penalty": float(sum(solver.Value(item) for item in balance_terms)),
        },
    )
