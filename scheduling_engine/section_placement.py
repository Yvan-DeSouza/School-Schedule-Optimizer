"""Pure semester/A-D placement with an internal staffing-feasibility witness.

The witness variables choose a qualified available teacher only to prove that a
future teacher-assignment stage has at least one legal solution.  The returned
recommendation intentionally contains no teacher identity: treating a witness
as an operational assignment would bypass the separate counselor review stage.
Rooms are also intentionally absent.  This stage establishes a conflict-aware
time structure before room capacity and room collisions are considered.
"""

from __future__ import annotations

from collections import defaultdict
from itertools import combinations

from ortools.sat.python import cp_model

from .diagnostics import (
    NO_AVAILABLE_TIMESLOT,
    NO_COMPLETE_PLACEMENT,
    NO_ELIGIBLE_TEACHER,
    NO_LEGAL_SEMESTER,
    ONLINE_SUPERVISION_BLOCK_DIVERSITY_INSUFFICIENT,
    ONLINE_SUPERVISION_CAPACITY_INSUFFICIENT,
)
from .dto import (
    PlacementAssignmentDTO,
    PlacementInputDTO,
    PlacementResultDTO,
)


def compile_section_placement_constraints(data: PlacementInputDTO) -> dict:
    """Validate and index the narrow data contract consumed by this solver.

    This compiler deliberately owns only semester/block and staffing facts. It
    cannot grow room requirements by accident, because rooms are not part of
    ``PlacementInputDTO`` at all.
    """

    timeslots = {slot.id: slot for slot in data.timeslots if slot.academic_year_id == data.academic_year_id}
    if len(timeslots) != len(data.timeslots):
        raise ValueError("Placement input contains a timeslot outside its academic year.")
    unit_keys = set()
    for unit in data.units:
        if unit.key in unit_keys:
            raise ValueError(f"Duplicate placement unit key: {unit.key}.")
        unit_keys.add(unit.key)
        if unit.requires_course_qualification and not unit.member_course_ids:
            raise ValueError(f"Placement unit {unit.key} has no member courses.")
        if unit.online_supervision_session_id is not None and unit.requires_course_qualification:
            raise ValueError("Online supervision placement must not require course qualification.")
        legal = set(unit.allowed_semesters)
        if unit.fixed_semester is not None:
            legal &= {unit.fixed_semester}
        if not legal:
            raise ValueError(f"Placement unit {unit.key} has no legal semester.")
        if unit.locked_timeslot_id is not None:
            slot = timeslots.get(unit.locked_timeslot_id)
            if slot is None or slot.semester not in legal:
                raise ValueError(f"Placement lock for {unit.key} is outside its year or legal semester.")
    teacher_ids = {teacher.id for teacher in data.teachers}
    for fixed in data.fixed_placements:
        if fixed.timeslot_id not in timeslots:
            raise ValueError("Fixed placement references an unknown target-year timeslot.")
        if fixed.teacher_id is not None and fixed.teacher_id not in teacher_ids:
            raise ValueError("Fixed placement references a teacher outside the confirmed roster.")
    online_session_ids = set()
    for session in data.online_supervision_sessions:
        if session.session_id in online_session_ids:
            raise ValueError(f"Duplicate online supervision session {session.session_id}.")
        online_session_ids.add(session.session_id)
        if session.capacity_max <= 0:
            raise ValueError("Online supervision session capacity must be positive.")
        if not set(session.allowed_semesters) <= {1, 2} or not session.allowed_semesters:
            raise ValueError("Online supervision session must have one or two legal semesters.")
        if session.fixed_timeslot_id is not None:
            slot = timeslots.get(session.fixed_timeslot_id)
            if slot is None or slot.semester not in session.allowed_semesters:
                raise ValueError("Fixed online supervision timing is outside the session's legal semester.")
    online_demand_ids = set()
    for demand in data.online_supervision_demands:
        if demand.request_id in online_demand_ids:
            raise ValueError(f"Duplicate online supervision demand request {demand.request_id}.")
        online_demand_ids.add(demand.request_id)
        if not set(demand.allowed_semesters) <= {1, 2} or not demand.allowed_semesters:
            raise ValueError("Online supervision demand must have one or two legal semesters.")
    return {"timeslots": timeslots}


def _eligible_candidates(data: PlacementInputDTO, unit, timeslots):
    """Return hidden (slot, teacher) choices satisfying hard staffing rules."""

    legal_semesters = set(unit.allowed_semesters)
    if unit.fixed_semester is not None:
        legal_semesters &= {unit.fixed_semester}
    slots = [
        slot for slot in timeslots.values()
        if slot.is_available
        and slot.semester in legal_semesters
        and (unit.locked_timeslot_id is None or slot.id == unit.locked_timeslot_id)
    ]
    choices = []
    for slot in slots:
        for teacher in data.teachers:
            if unit.locked_teacher_id is not None and teacher.id != unit.locked_teacher_id:
                continue
            # Availability is available-by-default: only an explicit false row
            # reaches this DTO as a denied slot.
            if slot.id in teacher.unavailable_timeslot_ids:
                continue
            if (
                unit.requires_course_qualification
                and not set(unit.member_course_ids).issubset(teacher.eligible_course_ids)
            ):
                continue
            if teacher.remaining_annual <= 0:
                continue
            if slot.semester == 1 and teacher.remaining_semester_1 <= 0:
                continue
            if slot.semester == 2 and teacher.remaining_semester_2 <= 0:
                continue
            choices.append((slot, teacher))
    return slots, choices


def _course_pair_weights(data: PlacementInputDTO) -> dict[tuple[int, int], float]:
    """Compile annual conflict facts once before expanding physical section pairs.

    A placement input can contain hundreds of physical units. Rebuilding this
    immutable lookup for every unit pair made model construction grow with both
    the square of section count and the complete conflict matrix, even though
    the underlying annual facts never change during one solve.
    """

    return {
        tuple(sorted((row.course_a_id, row.course_b_id))): (
            float(row.weight) * float(row.estimated_retained_co_request_count)
        )
        for row in data.conflicts
    }


def _course_pair_weight(unit_a, unit_b, weights: dict[tuple[int, int], float]) -> int:
    """Return a scaled conflict cost for two physical delivery units."""

    value = sum(
        weights.get(tuple(sorted((course_a, course_b))), 0.0)
        for course_a in unit_a.member_course_ids
        for course_b in unit_b.member_course_ids
        if course_a != course_b
    )
    # CP-SAT uses integer coefficients. The multiplier retains useful fractional
    # historical-demand evidence without exposing float behavior to the model.
    return round(value * 100)


def _online_session_slot_options(data, placement_vars):
    """Return legal generic supervision placements without exposing course sections.

    The normal timing model already decides every unplaced supervision session's
    semester/A-D block.  These options let a small internal witness prove that
    the same generic seats can serve students with more than one online course
    in distinct blocks.  They never become persisted student assignments.
    """

    unit_by_session_id = {
        unit.online_supervision_session_id: unit
        for unit in data.units
        if unit.online_supervision_session_id is not None
    }
    options = {}
    for session in data.online_supervision_sessions:
        if session.fixed_timeslot_id is not None:
            options[session.session_id] = ((session.fixed_timeslot_id, None),)
            continue
        unit = unit_by_session_id.get(session.session_id)
        if unit is None:
            options[session.session_id] = ()
            continue
        options[session.session_id] = tuple(
            (timeslot_id, variable)
            for (unit_key, timeslot_id), variable in placement_vars.items()
            if unit_key == unit.key
        )
    return options


def _add_online_supervision_demand_witness(model, data, timeslots, session_options):
    """Require a feasible temporary allocation of online requests to generic seats.

    A session may supervise many course codes, so the witness has no
    course-to-session restriction.  It does, however, bind each request to the
    session's selected time and prevents one student from using two
    supervision seats in the same semester/A-D slot.  This protects the later
    student-assignment stage without coupling normal course placement to a
    persisted online roster.
    """

    if not data.online_supervision_demands:
        return
    sessions_by_id = {
        session.session_id: session
        for session in data.online_supervision_sessions
    }
    assignments_by_session = defaultdict(list)
    assignments_by_student_slot = defaultdict(list)
    for demand in sorted(data.online_supervision_demands, key=lambda item: item.request_id):
        choices = []
        for session_id, session in sessions_by_id.items():
            for timeslot_id, placement_variable in session_options.get(session_id, ()):
                if timeslots[timeslot_id].semester not in demand.allowed_semesters:
                    continue
                variable = model.NewBoolVar(
                    f"online_supervision_request_{demand.request_id}_{session_id}_{timeslot_id}"
                )
                if placement_variable is not None:
                    model.Add(variable <= placement_variable)
                choices.append(variable)
                assignments_by_session[session_id].append(variable)
                assignments_by_student_slot[demand.student_id, timeslot_id].append(variable)
        if choices:
            model.AddExactlyOne(choices)
        else:
            # Preserve an explicit hard infeasibility rather than allowing a
            # student with online demand to disappear from the placement proof.
            model.Add(0 == 1)
    for session_id, assignments in assignments_by_session.items():
        model.Add(sum(assignments) <= sessions_by_id[session_id].capacity_max)
    for assignments in assignments_by_student_slot.values():
        model.Add(sum(assignments) <= 1)


def _online_supervision_demand_feasibility(data, timeslots, session_options):
    """Classify an online-only capacity/block failure before the full solve.

    This deliberately omits normal sections and named staffing competition.  A
    negative result therefore truthfully identifies a problem inherent to the
    available generic supervision seats or their block diversity, while other
    full-placement failures retain their existing diagnostics.
    """

    if not data.online_supervision_demands:
        return None
    sessions_by_id = {
        session.session_id: session
        for session in data.online_supervision_sessions
    }
    if sum(session.capacity_max for session in sessions_by_id.values()) < len(data.online_supervision_demands):
        return "capacity"
    if any(
        not session_options.get(session_id)
        for session_id in sessions_by_id
    ):
        # A normal placement diagnostic already explains the session with no
        # time/teacher candidate; do not mislabel it as a block-diversity issue.
        return None

    model = cp_model.CpModel()
    placement_variables = {}
    for session_id, options in session_options.items():
        values = []
        for timeslot_id, _placement_variable in options:
            variable = model.NewBoolVar(f"online_session_{session_id}_{timeslot_id}")
            placement_variables[session_id, timeslot_id] = variable
            values.append(variable)
        model.AddExactlyOne(values)

    assignments_by_session = defaultdict(list)
    assignments_by_student_slot = defaultdict(list)
    for demand in sorted(data.online_supervision_demands, key=lambda item: item.request_id):
        choices = []
        for session_id, options in session_options.items():
            for timeslot_id, _placement_variable in options:
                if timeslots[timeslot_id].semester not in demand.allowed_semesters:
                    continue
                variable = model.NewBoolVar(
                    f"online_demand_{demand.request_id}_{session_id}_{timeslot_id}"
                )
                model.Add(variable <= placement_variables[session_id, timeslot_id])
                choices.append(variable)
                assignments_by_session[session_id].append(variable)
                assignments_by_student_slot[demand.student_id, timeslot_id].append(variable)
        if choices:
            model.AddExactlyOne(choices)
        else:
            model.Add(0 == 1)
    for session_id, assignments in assignments_by_session.items():
        model.Add(sum(assignments) <= sessions_by_id[session_id].capacity_max)
    for assignments in assignments_by_student_slot.values():
        model.Add(sum(assignments) <= 1)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = min(float(data.time_limit_seconds), 2.0)
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = 0
    status = solver.Solve(model)
    if status in {cp_model.OPTIMAL, cp_model.FEASIBLE}:
        return "feasible"
    if status == cp_model.INFEASIBLE:
        return "infeasible"
    return "unknown"


def solve_section_placement(data: PlacementInputDTO) -> PlacementResultDTO:
    """Find a reviewable timing candidate while proving hidden staffing exists."""

    compiled = compile_section_placement_constraints(data)
    timeslots = compiled["timeslots"]
    model = cp_model.CpModel()
    candidate_vars = {}
    placement_vars = {}
    diagnostics = []

    for unit in sorted(data.units, key=lambda item: item.key):
        slots, candidates = _eligible_candidates(data, unit, timeslots)
        if not set(unit.allowed_semesters) if unit.fixed_semester is None else not ({unit.fixed_semester} & set(unit.allowed_semesters)):
            diagnostics.append({"code": NO_LEGAL_SEMESTER, "unit_key": unit.key})
        elif not slots:
            diagnostics.append({"code": NO_AVAILABLE_TIMESLOT, "unit_key": unit.key})
        elif not candidates:
            diagnostics.append({"code": NO_ELIGIBLE_TEACHER, "unit_key": unit.key})
        vars_for_unit = []
        for slot, teacher in candidates:
            variable = model.NewBoolVar(f"w_{unit.key}_{slot.id}_{teacher.id}")
            candidate_vars[unit.key, slot.id, teacher.id] = variable
            vars_for_unit.append(variable)
        for slot in slots:
            variables = [
                candidate_vars[unit.key, slot.id, teacher.id]
                for _slot, teacher in candidates
                if _slot.id == slot.id
            ]
            if variables:
                placement = model.NewBoolVar(f"p_{unit.key}_{slot.id}")
                model.Add(sum(variables) == placement)
                placement_vars[unit.key, slot.id] = placement
        if vars_for_unit:
            model.Add(sum(vars_for_unit) <= 1)

    online_session_options = _online_session_slot_options(data, placement_vars)
    online_feasibility = _online_supervision_demand_feasibility(
        data, timeslots, online_session_options,
    )
    if online_feasibility == "capacity":
        diagnostics.append({
            "code": ONLINE_SUPERVISION_CAPACITY_INSUFFICIENT,
            "online_request_count": len(data.online_supervision_demands),
            "total_supervision_capacity": sum(
                session.capacity_max for session in data.online_supervision_sessions
            ),
        })
    elif online_feasibility == "infeasible":
        diagnostics.append({
            "code": ONLINE_SUPERVISION_BLOCK_DIVERSITY_INSUFFICIENT,
            "online_request_count": len(data.online_supervision_demands),
            "online_session_count": len(data.online_supervision_sessions),
            "affected_student_ids": sorted({
                demand.student_id for demand in data.online_supervision_demands
            }),
        })
    _add_online_supervision_demand_witness(
        model, data, timeslots, online_session_options,
    )

    # A configured trimestre pair is sequential delivery by one qualified
    # teacher in one recurring block. Equalizing the hidden (slot, teacher)
    # witnesses proves both timing and shared staffing without leaking a named
    # teacher into the placement result.
    workload_representative = {}
    paired_units = defaultdict(list)
    for unit in data.units:
        if unit.shared_staffing_key:
            paired_units[unit.shared_staffing_key].append(unit)
    for pair_key, pair_units in paired_units.items():
        if len(pair_units) != 2:
            raise ValueError(f"Shared half-semester staffing key {pair_key} must identify exactly two units.")
        first, second = sorted(pair_units, key=lambda item: item.key)
        first_vars = {
            (slot_id, teacher_id): variable
            for (unit_key, slot_id, teacher_id), variable in candidate_vars.items()
            if unit_key == first.key
        }
        second_vars = {
            (slot_id, teacher_id): variable
            for (unit_key, slot_id, teacher_id), variable in candidate_vars.items()
            if unit_key == second.key
        }
        for candidate_key in set(first_vars) | set(second_vars):
            left = first_vars.get(candidate_key)
            right = second_vars.get(candidate_key)
            if left is None:
                model.Add(right == 0)
            elif right is None:
                model.Add(left == 0)
            else:
                model.Add(left == right)
                workload_representative[right.Index()] = left

    # A witness teacher may never cover concurrent candidate sections. Accepted
    # assignments outside this run reserve their teacher/time pair first.
    fixed_teacher_times = {(item.teacher_id, item.timeslot_id) for item in data.fixed_placements if item.teacher_id is not None}
    by_teacher_slot = defaultdict(list)
    by_teacher_semester = defaultdict(list)
    by_teacher_annual = defaultdict(list)
    seen_workload_rows = set()
    for (unit_key, slot_id, teacher_id), variable in candidate_vars.items():
        variable = workload_representative.get(variable.Index(), variable)
        workload_key = (teacher_id, slot_id, variable.Index())
        if workload_key in seen_workload_rows:
            continue
        seen_workload_rows.add(workload_key)
        by_teacher_slot[teacher_id, slot_id].append(variable)
        by_teacher_semester[teacher_id, timeslots[slot_id].semester].append(variable)
        by_teacher_annual[teacher_id].append(variable)
    teachers = {item.id: item for item in data.teachers}
    for (teacher_id, slot_id), variables in by_teacher_slot.items():
        model.Add(sum(variables) <= (0 if (teacher_id, slot_id) in fixed_teacher_times else 1))
    for (teacher_id, semester), variables in by_teacher_semester.items():
        capacity = teachers[teacher_id].remaining_semester_1 if semester == 1 else teachers[teacher_id].remaining_semester_2
        model.Add(sum(variables) <= capacity)
    for teacher_id, variables in by_teacher_annual.items():
        model.Add(sum(variables) <= teachers[teacher_id].remaining_annual)

    placed = list(placement_vars.values())
    objective_terms = []
    # Highest priority: maximum placement. The configured scale dominates every
    # possible soft metric in a school-sized run, preserving the intended
    # lexicographic outcome without repeatedly rebuilding the CP-SAT model.
    objective_terms.append(-1_000_000_000 * sum(placed or [0]))

    units = sorted(data.units, key=lambda item: item.key)
    collision_terms = []
    course_pair_weights = _course_pair_weights(data)
    for unit_a, unit_b in combinations(units, 2):
        pair_weight = _course_pair_weight(unit_a, unit_b, course_pair_weights)
        if pair_weight <= 0:
            continue
        for slot_id, slot in timeslots.items():
            first = placement_vars.get((unit_a.key, slot_id))
            second = placement_vars.get((unit_b.key, slot_id))
            if first is None or second is None:
                continue
            collision = model.NewBoolVar(f"collision_{unit_a.key}_{unit_b.key}_{slot_id}")
            model.AddBoolAnd([first, second]).OnlyEnforceIf(collision)
            model.AddBoolOr([first.Not(), second.Not(), collision])
            collision_terms.append(pair_weight * collision)
    objective_terms.append(sum(collision_terms or [0]))

    # In annual mode, pairs often chosen together should have similar semester
    # coverage. We compare count/total ratios by cross-multiplying, avoiding
    # floating point coefficients in CP-SAT.
    semester_terms = []
    if data.input_mode == "annual_total":
        unit_by_course = defaultdict(list)
        for unit in units:
            for course_id in unit.member_course_ids:
                unit_by_course[course_id].append(unit)
        for conflict in data.conflicts:
            left = unit_by_course.get(conflict.course_a_id, [])
            right = unit_by_course.get(conflict.course_b_id, [])
            if not left or not right:
                continue
            left_s1 = sum(placement_vars.get((unit.key, slot.id), 0) for unit in left for slot in timeslots.values() if slot.semester == 1)
            right_s1 = sum(placement_vars.get((unit.key, slot.id), 0) for unit in right for slot in timeslots.values() if slot.semester == 1)
            difference = model.NewIntVar(-len(left) * len(right), len(left) * len(right), f"semester_pair_{conflict.course_a_id}_{conflict.course_b_id}")
            model.Add(difference == left_s1 * len(right) - right_s1 * len(left))
            absolute = model.NewIntVar(0, len(left) * len(right), f"semester_pair_abs_{conflict.course_a_id}_{conflict.course_b_id}")
            model.AddAbsEquality(absolute, difference)
            semester_terms.append(round(float(conflict.weight) * float(conflict.estimated_retained_co_request_count) * 100) * absolute)
    objective_terms.append(sum(semester_terms or [0]))

    # Same-course spread is intentionally a delivery-group objective rather
    # than a fake course-to-itself conflict row. It encourages diverse access
    # while preserving the meaning of the counselor-managed pair matrix.
    spread_terms = []
    for group_id in sorted({unit.delivery_group_id for unit in units}):
        group_units = [unit for unit in units if unit.delivery_group_id == group_id]
        if data.input_mode == "annual_total" and len(group_units) > 1:
            s1 = sum(placement_vars.get((unit.key, slot.id), 0) for unit in group_units for slot in timeslots.values() if slot.semester == 1)
            imbalance = model.NewIntVar(0, len(group_units), f"group_semester_imbalance_{group_id}")
            delta = model.NewIntVar(-len(group_units), len(group_units), f"group_semester_delta_{group_id}")
            model.Add(delta == (2 * s1) - len(group_units))
            model.AddAbsEquality(imbalance, delta)
            spread_terms.append(imbalance)
        for slot_id in timeslots:
            selected = [placement_vars.get((unit.key, slot_id)) for unit in group_units]
            selected = [item for item in selected if item is not None]
            for first, second in combinations(selected, 2):
                concentration = model.NewBoolVar(f"group_block_{group_id}_{slot_id}_{len(spread_terms)}")
                model.AddBoolAnd([first, second]).OnlyEnforceIf(concentration)
                model.AddBoolOr([first.Not(), second.Not(), concentration])
                spread_terms.append(concentration)
    objective_terms.append(sum(spread_terms or [0]))

    # Conservative priorities maintain the declared ordering: conflict exposure
    # is more important than annual split balance, which is more important than
    # spreading sibling sections. The last tiny term gives deterministic output.
    weighted_objective = (
        objective_terms[0]
        + 10_000 * objective_terms[1]
        + 100 * objective_terms[2]
        + objective_terms[3]
        + sum((index + 1) * variable for index, variable in enumerate(placed))
    )
    model.Minimize(weighted_objective)
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = data.time_limit_seconds
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = 0
    status = solver.Solve(model)
    status_name = {
        cp_model.OPTIMAL: "optimal", cp_model.FEASIBLE: "feasible",
        cp_model.INFEASIBLE: "infeasible", cp_model.MODEL_INVALID: "model_invalid",
    }.get(status, "unknown")

    assignments = []
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        for unit in units:
            for slot_id, slot in timeslots.items():
                variable = placement_vars.get((unit.key, slot_id))
                if variable is not None and solver.Value(variable):
                    assignments.append(PlacementAssignmentDTO(
                        unit_key=unit.key, section_id=unit.section_id,
                        delivery_group_id=unit.delivery_group_id, semester=slot.semester,
                        timeslot_id=slot.id, block=slot.block, annual_index=unit.annual_index,
                        online_supervision_session_id=unit.online_supervision_session_id,
                    ))
                    break
    assigned_keys = {item.unit_key for item in assignments}
    unplaced = tuple(unit.key for unit in units if unit.key not in assigned_keys)
    if not assignments and units:
        result_status = "infeasible"
    elif unplaced:
        result_status = "partial"
        diagnostics.append({"code": NO_COMPLETE_PLACEMENT, "unplaced_unit_keys": list(unplaced)})
    else:
        result_status = "complete"
    return PlacementResultDTO(
        status=result_status,
        solver_outcome=status_name,
        assignments=tuple(assignments),
        unplaced_unit_keys=unplaced,
        diagnostics=tuple(diagnostics),
        objective_components={
            "placed_units": float(len(assignments)),
            "course_pair_collision_penalty": float(solver.Value(sum(collision_terms or [0])) if assignments else 0),
            "semester_balance_penalty": float(solver.Value(sum(semester_terms or [0])) if assignments else 0),
        },
        staffing_summary={
            "confirmed_teacher_count": len(data.teachers),
            "witness_proven": bool(assignments),
            "teacher_names_or_assignments_returned": False,
        },
    )
