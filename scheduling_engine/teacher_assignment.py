"""Pure named-teacher assignment after counselors approve section timing.

This solver consumes a detached snapshot.  It deliberately does not model
rooms, students, or timetable placement: a section's semester/block is already
accepted fixed context, and only the teacher dimension is being decided here.
"""

from __future__ import annotations

from collections import defaultdict
from ortools.sat.python import cp_model

from .constants import TEACHER_ASSIGNMENT_WORKER_COUNT
from .diagnostics import (
    LOCKED_TEACHER_INELIGIBLE,
    LOCKED_TEACHER_UNAVAILABLE,
    NO_COMPLETE_TEACHER_ASSIGNMENT,
    NO_ELIGIBLE_TEACHER_FOR_SECTION,
    TEACHER_COURSE_RULE_INFEASIBLE,
)
from .dto import TeacherAssignmentDTO, TeacherAssignmentInputDTO, TeacherAssignmentResultDTO
from .teacher_assignment_evidence import build_teacher_assignment_candidate_ledger


def _decision_key(item):
    """Keep online supervision identities distinct from ordinary section IDs."""

    return (
        ("online_supervision", item.online_supervision_session_id)
        if item.is_online_supervision
        else ("section", item.section_id)
    )


def _diagnostic_identity(item):
    """Expose the correct counselor-review target without a fabricated section."""

    return (
        {"online_supervision_session_id": item.online_supervision_session_id}
        if item.is_online_supervision
        else {"section_id": item.section_id}
    )


def compile_teacher_assignment_constraints(data: TeacherAssignmentInputDTO) -> dict:
    """Validate the narrow, ORM-free contract consumed by this stage."""

    teachers = {teacher.id: teacher for teacher in data.teachers}
    decision_keys = set()
    for section in data.sections:
        key = _decision_key(section)
        if key in decision_keys:
            raise ValueError(f"Duplicate teacher-assignment decision unit {key}.")
        decision_keys.add(key)
        if section.semester not in {1, 2} or (
            not section.is_online_supervision and not section.member_course_ids
        ):
            raise ValueError(f"Teacher-assignment unit {key} lacks valid accepted timing/course context.")
        if section.is_online_supervision and section.online_supervision_session_id is None:
            raise ValueError("Online supervision staffing input requires its session identity.")
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
        if (
            not section.is_online_supervision
            and not set(section.member_course_ids).issubset(teacher.eligible_course_ids)
        ):
            continue
        if teacher.remaining_annual <= 0:
            continue
        if section.semester == 1 and teacher.remaining_semester_1 <= 0:
            continue
        if section.semester == 2 and teacher.remaining_semester_2 <= 0:
            continue
        candidates.append(teacher)
    return candidates


def _teacher_load_balance_penalties(model, variables, teachers):
    """Return the existing pairwise workload penalty without pair variables.

    The former model created one Boolean for every pair of candidate variables
    for a teacher.  For Boolean choices, that sum is exactly ``n choose 2``,
    where ``n`` is the number of that teacher's selected candidate rows.  An
    element lookup preserves the objective's value—including the deliberate
    double counting of the two linked half-semester rows—without making model
    size quadratic in the ready-roster candidate pool.
    """

    penalties = []
    for teacher_id in teachers:
        teacher_vars = [
            variable
            for (_section_key, candidate_teacher_id), variable in variables.items()
            if candidate_teacher_id == teacher_id
        ]
        if not teacher_vars:
            continue
        selected_count = model.NewIntVar(
            0,
            len(teacher_vars),
            f"teacher_selected_candidate_count_{teacher_id}",
        )
        model.Add(selected_count == sum(teacher_vars))
        maximum_penalty = len(teacher_vars) * (len(teacher_vars) - 1) // 2
        penalty = model.NewIntVar(
            0,
            maximum_penalty,
            f"teacher_load_pair_penalty_{teacher_id}",
        )
        model.AddElement(
            selected_count,
            [count * (count - 1) // 2 for count in range(len(teacher_vars) + 1)],
            penalty,
        )
        penalties.append(penalty)
    return penalties


def solve_teacher_assignment(data: TeacherAssignmentInputDTO) -> TeacherAssignmentResultDTO:
    """Return a complete or diagnostic partial named-teacher recommendation."""

    compiled = compile_teacher_assignment_constraints(data)
    teachers = compiled["teachers"]
    model = cp_model.CpModel()
    diagnostics = []
    variables = {}
    section_candidates = {}
    decision_sections = [section for section in data.sections if not section.is_fixed]

    for section in sorted(decision_sections, key=_decision_key):
        key = _decision_key(section)
        candidates = _candidates(section, teachers)
        section_candidates[key] = candidates
        if not candidates:
            locked = teachers.get(section.locked_teacher_id) if section.locked_teacher_id else None
            if locked and section.timeslot_id in locked.unavailable_timeslot_ids:
                diagnostics.append({"code": LOCKED_TEACHER_UNAVAILABLE, **_diagnostic_identity(section)})
            elif locked and not section.is_online_supervision and not set(section.member_course_ids).issubset(locked.eligible_course_ids):
                diagnostics.append({"code": LOCKED_TEACHER_INELIGIBLE, **_diagnostic_identity(section)})
            else:
                diagnostics.append({"code": NO_ELIGIBLE_TEACHER_FOR_SECTION, **_diagnostic_identity(section)})
        for teacher in candidates:
            variables[key, teacher.id] = model.NewBoolVar(
                f"teacher_{key[0]}_{key[1]}_{teacher.id}"
            )
        section_vars = [variables[key, teacher.id] for teacher in candidates]
        if section_vars:
            model.Add(sum(section_vars) <= 1)

    # Sequential trimestre sections are one shared teaching block. The pair
    # still has two academic-course identities for qualification/rule checks,
    # but equal teacher variables and a single workload representative ensure
    # it consumes one teacher load rather than two concurrent sections.
    workload_representative = {}
    paired_sections = defaultdict(list)
    for section in decision_sections:
        if section.shared_staffing_key:
            paired_sections[section.shared_staffing_key].append(section)
    for pair_key, pair_sections in paired_sections.items():
        if len(pair_sections) != 2:
            raise ValueError(f"Shared half-semester staffing key {pair_key} must identify exactly two sections.")
        first, second = sorted(pair_sections, key=_decision_key)
        first_key, second_key = _decision_key(first), _decision_key(second)
        first_vars = {
            teacher_id: variable
            for (section_key, teacher_id), variable in variables.items()
            if section_key == first_key
        }
        second_vars = {
            teacher_id: variable
            for (section_key, teacher_id), variable in variables.items()
            if section_key == second_key
        }
        for teacher_id in set(first_vars) | set(second_vars):
            left, right = first_vars.get(teacher_id), second_vars.get(teacher_id)
            if left is None:
                model.Add(right == 0)
            elif right is None:
                model.Add(left == 0)
            else:
                model.Add(left == right)
                workload_representative[right.Index()] = left

    by_teacher_slot = defaultdict(list)
    by_teacher_semester = defaultdict(list)
    by_teacher_annual = defaultdict(list)
    decision_by_key = {_decision_key(item): item for item in decision_sections}
    seen_workload_rows = set()
    for (section_key, teacher_id), variable in variables.items():
        section = decision_by_key[section_key]
        variable = workload_representative.get(variable.Index(), variable)
        workload_key = (teacher_id, section.timeslot_id, variable.Index())
        if workload_key in seen_workload_rows:
            continue
        seen_workload_rows.add(workload_key)
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
            for (section_key, teacher_id), variable in variables.items()
            if teacher_id == rule.teacher_id
            and rule.course_id in decision_by_key[section_key].member_course_ids
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
    for (section_key, teacher_id), variable in variables.items():
        section = decision_by_key[section_key]
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
    balance_terms = _teacher_load_balance_penalties(model, variables, teachers)
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
    solver.parameters.num_search_workers = TEACHER_ASSIGNMENT_WORKER_COUNT
    outcome = solver.Solve(model)
    outcome_name = {
        cp_model.OPTIMAL: "optimal", cp_model.FEASIBLE: "feasible",
        cp_model.INFEASIBLE: "infeasible", cp_model.MODEL_INVALID: "model_invalid",
    }.get(outcome, "unknown")
    if outcome not in {cp_model.OPTIMAL, cp_model.FEASIBLE}:
        diagnostics.append({"code": TEACHER_COURSE_RULE_INFEASIBLE if data.rules else NO_COMPLETE_TEACHER_ASSIGNMENT})
        return TeacherAssignmentResultDTO(
            status="infeasible", solver_outcome=outcome_name, assignments=(),
            unassigned_section_ids=tuple(item.section_id for item in decision_sections if item.section_id is not None),
            diagnostics=tuple(diagnostics), objective_components={},
            unassigned_online_supervision_session_ids=tuple(
                item.online_supervision_session_id
                for item in decision_sections
                if item.is_online_supervision and item.online_supervision_session_id is not None
            ),
            candidate_ledger=build_teacher_assignment_candidate_ledger(
                data=data,
                assignments=(),
                has_solution=False,
            ),
        )

    assignments = []
    for section in sorted(decision_sections, key=_decision_key):
        key = _decision_key(section)
        for teacher in section_candidates[key]:
            variable = variables[key, teacher.id]
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
                    online_supervision_session_id=section.online_supervision_session_id,
                ))
                break
    assigned_keys = {
        ("online_supervision", row.online_supervision_session_id)
        if row.online_supervision_session_id is not None
        else ("section", row.section_id)
        for row in assignments
    }
    unassigned = tuple(
        section.section_id
        for section in decision_sections
        if section.section_id is not None and _decision_key(section) not in assigned_keys
    )
    unassigned_online = tuple(
        section.online_supervision_session_id
        for section in decision_sections
        if section.is_online_supervision
        and section.online_supervision_session_id is not None
        and _decision_key(section) not in assigned_keys
    )
    if unassigned or unassigned_online:
        diagnostics.append({
            "code": NO_COMPLETE_TEACHER_ASSIGNMENT,
            "unassigned_section_ids": list(unassigned),
            "unassigned_online_supervision_session_ids": list(unassigned_online),
        })
    return TeacherAssignmentResultDTO(
        status="complete" if not unassigned and not unassigned_online else "partial", solver_outcome=outcome_name,
        assignments=tuple(assignments), unassigned_section_ids=unassigned,
        diagnostics=tuple(diagnostics),
        objective_components={
            "assigned_sections": float(len(assignments)),
            "requested_course_matches": float(sum(item.explanation["requested_course_match"] for item in assignments)),
            "prior_year_course_matches": float(sum(item.explanation["prior_year_course_match"] for item in assignments)),
            "teacher_load_balance_penalty": float(sum(solver.Value(item) for item in balance_terms)),
        },
        unassigned_online_supervision_session_ids=unassigned_online,
        candidate_ledger=build_teacher_assignment_candidate_ledger(
            data=data,
            assignments=tuple(assignments),
            has_solution=True,
        ),
    )
