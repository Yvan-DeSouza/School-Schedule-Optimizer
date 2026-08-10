"""Pure student-to-section assignment over fixed accepted schedule context.

This module intentionally has no Django dependency.  It consumes a detached
snapshot, recommends enrollment creation or replacement facts, and never
changes section timing, rooms, teachers, or persisted enrollment records.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import replace

from ortools.sat.python import cp_model

from .diagnostics import (
    NO_COMPLETE_STUDENT_ASSIGNMENT,
    STUDENT_ASSIGNMENT_HARD_PREREQUISITE_SEQUENCE_UNAVAILABLE,
    STUDENT_ASSIGNMENT_LIMITED_SEAT_CONTENTION,
    STUDENT_ASSIGNMENT_LOCKED_ENROLLMENT_BLOCKS_REQUEST,
    STUDENT_ASSIGNMENT_NO_ACTIVE_PLACED_SECTION,
    STUDENT_ASSIGNMENT_NO_ELIGIBLE_SECTION,
    STUDENT_ASSIGNMENT_REQUIRES_ADDITIONAL_CAPACITY,
    STUDENT_ASSIGNMENT_REQUIRES_LOCK_RELEASE,
    STUDENT_ASSIGNMENT_REQUIRES_PLACED_SECTION,
    STUDENT_ASSIGNMENT_REQUIRES_PREREQUISITE_SEQUENCE_CHANGE,
    STUDENT_ASSIGNMENT_REQUIRES_TIMESLOT_CHANGE,
    STUDENT_ASSIGNMENT_SECTION_BELOW_TARGET_CAPACITY,
    STUDENT_ASSIGNMENT_SECTION_CAPACITY_EXHAUSTED,
    STUDENT_ASSIGNMENT_SECTION_OVER_TARGET_CONCENTRATION,
    STUDENT_ASSIGNMENT_TIMESLOT_COLLISION,
    STUDENT_ASSIGNMENT_UNRESOLVED_REQUIRED_REQUEST,
)
from .dto import (
    StudentAssignmentDTO,
    StudentAssignmentInputDTO,
    StudentAssignmentLockCostDTO,
    StudentAssignmentResultDTO,
    StudentAssignmentSeatContentionDTO,
    StudentAssignmentSectionBalanceDTO,
    StudentAssignmentUnmetRequestDTO,
)


IMPORTANCE_LEVELS = {
    "not_important": 0,
    "a_little_bit_important": 1,
    "important": 2,
    "really_important": 3,
    "extremely_important": 4,
}

# These labels deliberately mirror the scheduling-domain constants without
# importing backend code. The engine owns no Django vocabulary dependency.
SCHEDULE_PRESERVATION_LEVELS = {
    "none": 0,
    "slight": 1,
    "moderate": 2,
    "strong": 4,
}

LOCK_TYPE_EXACT_SECTION = "exact_student_section"
LOCK_TYPE_WHOLE_SCHEDULE = "whole_student_schedule"
LOCK_TYPE_SECTION_ROSTER = "section_roster"
LOCK_TYPE_COURSE_ROSTER = "course_roster"
LOCK_TYPE_STUDENT_GROUP = "student_group_same_section"
LOCK_TYPE_STUDENT_TEACHER = "student_teacher_course"
LOCK_TYPES = {
    LOCK_TYPE_EXACT_SECTION,
    LOCK_TYPE_WHOLE_SCHEDULE,
    LOCK_TYPE_SECTION_ROSTER,
    LOCK_TYPE_COURSE_ROSTER,
    LOCK_TYPE_STUDENT_GROUP,
    LOCK_TYPE_STUDENT_TEACHER,
}


def _outcome_name(status):
    return {
        cp_model.OPTIMAL: "optimal",
        cp_model.FEASIBLE: "feasible",
        cp_model.INFEASIBLE: "infeasible",
        cp_model.MODEL_INVALID: "model_invalid",
        cp_model.UNKNOWN: "unknown",
    }.get(status, "unknown")


def _new_solver(time_limit_seconds, *, fix_hints=False):
    """Build the deliberately reproducible CP-SAT configuration for this stage."""

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_seconds
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = 0
    solver.parameters.fix_variables_to_their_hinted_value = fix_hints
    return solver


def _set_solver_hints(model, solver):
    """Carry a complete validated candidate into the next lexicographic pass.

    CP-SAT does not automatically retain a prior ``CpSolver`` solution after
    the model gains an equality for that objective. Reapplying all values keeps
    the next pass focused on improvement rather than rediscovering a candidate
    that is already known to satisfy every hard scheduling rule.
    """

    model.ClearHints()
    for index in range(len(model.Proto().variables)):
        variable = model.GetIntVarFromProtoIndex(index)
        model.AddHint(variable, solver.Value(variable))


def _set_assignment_hints(model, assignment_hints):
    """Set a complete enrollment-variable hint without depending on DTOs here."""

    model.ClearHints()
    for index, proto_variable in enumerate(model.Proto().variables):
        if not proto_variable.name.startswith("enroll_"):
            continue
        _prefix, request_id, section_id = proto_variable.name.split("_", 2)
        variable = model.GetIntVarFromProtoIndex(index)
        model.AddHint(
            variable,
            int(assignment_hints.get((int(request_id), int(section_id)), False)),
        )


def _validated_initial_hint_solver(model, assignment_hints, time_limit_seconds):
    """Return a full-model candidate only after CP-SAT validates the hint.

    The deterministic constructor below is search guidance, never a second
    scheduler. Lock, capacity, timeslot, group, and prerequisite constraints
    remain authoritative in the CP-SAT model. Fixing the proposed enrollment
    choices for this bounded preparatory solve lets CP-SAT fill every derived
    variable and rejects a hint that is incompatible with any hard rule.
    """

    if not assignment_hints:
        return None
    _set_assignment_hints(model, assignment_hints)
    preparer = _new_solver(min(time_limit_seconds, 5.0), fix_hints=True)
    status = preparer.Solve(model)
    if status not in {cp_model.OPTIMAL, cp_model.FEASIBLE}:
        model.ClearHints()
        return None
    return preparer


def _solve_lexicographically(model, objectives, time_limit_seconds, *, initial_assignment_hints=None):
    """Optimize ordered objectives while preserving the last valid candidate.

    Each later pass retains equality constraints for every completed objective.
    If bounded search cannot find a new candidate, the prior candidate already
    satisfies those constraints and remains a safe recommendation. Returning
    it is therefore faithful to the existing lexicographic priorities; only
    the uncompleted lower-priority improvement is omitted.
    """

    previous_solver = None
    initial_assignment_hints = initial_assignment_hints or {}
    for objective in objectives:
        # Several stages intentionally add an objective slot even when this
        # input has no rows in that tier. Re-solving a constant objective has
        # no scheduling value and previously could discard an earlier result.
        if isinstance(objective, int):
            continue
        model.Minimize(objective)
        if previous_solver is not None:
            _set_solver_hints(model, previous_solver)
        elif initial_assignment_hints:
            prepared_solver = _validated_initial_hint_solver(
                model,
                initial_assignment_hints,
                time_limit_seconds,
            )
            if prepared_solver is not None:
                _set_solver_hints(model, prepared_solver)
                # The preparatory candidate is safe even if the first bounded
                # optimization pass later reaches UNKNOWN without an incumbent.
                previous_solver = prepared_solver

        solver = _new_solver(time_limit_seconds)
        status = solver.Solve(model)
        if status not in {cp_model.OPTIMAL, cp_model.FEASIBLE}:
            return previous_solver, status
        previous_solver = solver
        model.Add(objective == solver.Value(objective))

    if previous_solver is None:
        # A fully protected rerun can legitimately have no decision variables
        # and only constant objective slots: the adapter has already removed
        # requests satisfied by fixed active enrollments. It still needs one
        # feasibility solve so that this valid zero-decision context remains a
        # complete, reviewable run rather than being mislabeled as UNKNOWN.
        solver = _new_solver(time_limit_seconds)
        status = solver.Solve(model)
        return (solver if status in {cp_model.OPTIMAL, cp_model.FEASIBLE} else None), status

    # The last successful pass already satisfies its just-added equality and
    # all higher-priority equalities. A redundant final cold solve can only
    # lose that candidate under a timeout, so it is intentionally omitted.
    return previous_solver, cp_model.FEASIBLE


def _is_active_enrollment(enrollment):
    """Historical DTO rows are audit-only even if an older caller leaves is_active true."""

    return enrollment.is_active and not enrollment.is_historical


def _scope_includes_enrollment(data, enrollment):
    """Apply the immutable resolved scope before the model sees an enrollment.

    A scoped run may identify a row directly in ``is_in_scope`` after the
    adapter resolves its three queryable scope dimensions. The explicit IDs
    remain available so a detached snapshot is sufficient to reproduce that
    decision without an ORM query.
    """

    if data.scope.scope_type == "full":
        return True
    return (
        enrollment.is_in_scope
        or enrollment.student_id in data.scope.student_ids
        or enrollment.course_id in data.scope.course_ids
        or enrollment.section_id in data.scope.section_ids
    )


def _request_matches_enrollment(request, enrollment):
    if request.student_id != enrollment.student_id or request.course_id != enrollment.course_id:
        return False
    return request.current_enrollment_id is None or request.current_enrollment_id == enrollment.enrollment_id


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

    for importance in (
        data.section_utilization_balance_importance,
        data.student_semester_balance_importance,
        data.course_sequence_preferences_importance,
    ):
        if importance not in IMPORTANCE_LEVELS:
            raise ValueError("Student-assignment importance values are invalid.")
    if data.schedule_preservation_level not in SCHEDULE_PRESERVATION_LEVELS:
        raise ValueError("Student-assignment schedule preservation level is invalid.")
    if data.scope.scope_type not in {"full", "scoped"}:
        raise ValueError("Student-assignment scope_type must be full or scoped.")
    if data.scope.scope_type == "scoped" and not any(
        (data.scope.student_ids, data.scope.course_ids, data.scope.section_ids)
    ):
        raise ValueError("A scoped student-assignment input requires at least one resolved scope ID.")
    if len(set(data.priority_request_ids)) != len(data.priority_request_ids):
        raise ValueError("Priority request IDs must be unique.")
    priority_ids = set(data.priority_request_ids)
    unknown_priority_ids = priority_ids - request_ids
    if unknown_priority_ids:
        raise ValueError("Priority request IDs must identify requests in this input.")
    if data.priority_request_limit is not None:
        if data.priority_request_limit < 0:
            raise ValueError("Priority request limit cannot be negative.")
        if len(priority_ids) > data.priority_request_limit:
            raise ValueError("Priority request IDs exceed the resolved run limit.")
    if any(not request.is_primary for request in data.requests if request.request_id in priority_ids):
        raise ValueError("Only primary requests may receive student-assignment priority.")

    lock_ids = set()
    for lock in data.student_assignment_locks:
        if lock.lock_id in lock_ids:
            raise ValueError(f"Duplicate student-assignment lock {lock.lock_id}.")
        lock_ids.add(lock.lock_id)
        if lock.lock_type not in LOCK_TYPES:
            raise ValueError(f"Unrecognized student-assignment lock type {lock.lock_type!r}.")
        if lock.lock_type == LOCK_TYPE_STUDENT_GROUP and lock.is_active:
            if len(set(lock.member_student_ids)) < 2:
                raise ValueError("An active student-group lock requires at least two distinct members.")
            if lock.course_id is None:
                raise ValueError("An active student-group lock requires a course target.")
    return offering_sections


def _active_locks(data):
    return tuple(sorted(
        (lock for lock in data.student_assignment_locks if lock.is_active),
        key=lambda lock: lock.lock_id,
    ))


def _build_initial_assignment_hints(
    *, data, request_candidates, fixed_by_section, fixed_slots, group_locks,
):
    """Construct deterministic enrollment guidance for the first CP-SAT pass.

    This is intentionally conservative. It only proposes ordinary independent
    requests and declines to guide inputs with group or same-year prerequisite
    relationships, whose coupled decisions deserve the full model's search.
    The later preparatory CP-SAT solve validates every proposed value, so a
    construction mistake can never weaken or bypass a hard constraint.
    """

    if group_locks or data.hard_prerequisites:
        return {}

    requests_by_student = defaultdict(list)
    for request in data.requests:
        if request_candidates[request.request_id]:
            requests_by_student[request.student_id].append(request)

    remaining_capacity = {
        section.section_id: section.capacity_max - len(fixed_by_section[section.section_id])
        for section in data.sections
    }
    request_count_by_offering = Counter(
        request.course_offering_id
        for request in data.requests
        if request_candidates[request.request_id]
    )
    capacity_by_offering = {}
    for request in data.requests:
        if request.course_offering_id in capacity_by_offering:
            continue
        capacity_by_offering[request.course_offering_id] = sum(
            remaining_capacity[section.section_id]
            for section, _variable in request_candidates[request.request_id]
        )
    slack_by_offering = {
        offering_id: capacity_by_offering[offering_id] - request_count
        for offering_id, request_count in request_count_by_offering.items()
    }
    assigned_section_by_request = {}

    def assign_student(requests, used_timeslots):
        """Backtrack within one student's small request set, not across students."""

        if not requests:
            return True
        request = requests[0]
        candidates = sorted(
            request_candidates[request.request_id],
            key=lambda item: (
                # Filling the least-used compatible physical section first
                # creates a balanced, capacity-safe seed for CP-SAT to improve.
                -remaining_capacity[item[0].section_id],
                item[0].section_id,
            ),
        )
        for section, _variable in candidates:
            if (
                remaining_capacity[section.section_id] <= 0
                or section.timeslot_id in used_timeslots
            ):
                continue
            remaining_capacity[section.section_id] -= 1
            assigned_section_by_request[request.request_id] = section.section_id
            if assign_student(requests[1:], used_timeslots | {section.timeslot_id}):
                return True
            assigned_section_by_request.pop(request.request_id)
            remaining_capacity[section.section_id] += 1
        return False

    for student_id in sorted(requests_by_student):
        student_requests = sorted(
            requests_by_student[student_id],
            key=lambda request: (
                # The seed should protect a one-seat low-demand offering
                # before a high-demand course with many spare seats. This is
                # only search guidance; CP-SAT remains responsible for every
                # fulfillment tier and hard scheduling rule.
                slack_by_offering[request.course_offering_id],
                not request.is_mandatory,
                not request.is_primary,
                len(request_candidates[request.request_id]),
                request.request_id,
            ),
        )
        before_student = set(assigned_section_by_request)
        if assign_student(student_requests, set(fixed_slots[student_id])):
            continue
        # Do not leave a partial individual schedule in a failed hint. The
        # model still receives the request unchanged and can solve it normally.
        for request_id in set(assigned_section_by_request) - before_student:
            section_id = assigned_section_by_request.pop(request_id)
            remaining_capacity[section_id] += 1

    return {
        (request_id, section_id): True
        for request_id, section_id in assigned_section_by_request.items()
    }


def _diagnostic_for_unmet_request(
    *, request, offering_sections, candidates, fixed_slots, fixed_slot_rows,
    request_lock_blockers, direct_protected_requests, hard_sequence_impossible,
    selected_by_section, fixed_by_section, sections,
):
    """Return a stable reason plus the most specific available blocking IDs."""

    potential_sections = tuple(offering_sections.get(request.course_offering_id, ()))
    lock_ids = sorted(request_lock_blockers.get(request.request_id, ()))
    has_direct_protection = request.request_id in direct_protected_requests
    direct_protection = direct_protected_requests.get(request.request_id)
    if not potential_sections:
        return (
            STUDENT_ASSIGNMENT_NO_ACTIVE_PLACED_SECTION,
            None,
            None,
            None,
            (STUDENT_ASSIGNMENT_REQUIRES_PLACED_SECTION,),
        )
    if has_direct_protection or (lock_ids and not candidates):
        return (
            STUDENT_ASSIGNMENT_LOCKED_ENROLLMENT_BLOCKS_REQUEST,
            direct_protection or lock_ids[0],
            None,
            None,
            (STUDENT_ASSIGNMENT_REQUIRES_LOCK_RELEASE,),
        )
    if any(
        student_id == request.student_id
        and request.course_id in {prerequisite_id, course_id}
        for student_id, prerequisite_id, course_id in hard_sequence_impossible
    ):
        return (
            STUDENT_ASSIGNMENT_HARD_PREREQUISITE_SEQUENCE_UNAVAILABLE,
            None,
            None,
            None,
            (STUDENT_ASSIGNMENT_REQUIRES_PREREQUISITE_SEQUENCE_CHANGE,),
        )
    collided_rows = [
        row
        for section in potential_sections
        if section.timeslot_id in fixed_slots[request.student_id]
        for row in fixed_slot_rows[request.student_id, section.timeslot_id]
    ]
    if collided_rows and not candidates:
        lock_id = next((lock_id for row in collided_rows for lock_id in row.lock_ids), None)
        return (
            STUDENT_ASSIGNMENT_TIMESLOT_COLLISION,
            lock_id,
            collided_rows[0].section_id,
            collided_rows[0].student_id,
            (STUDENT_ASSIGNMENT_REQUIRES_TIMESLOT_CHANGE,),
        )
    if candidates:
        full_sections = []
        for section, _variable in candidates:
            assigned = selected_by_section.get(section.section_id, ())
            occupied = len(fixed_by_section[section.section_id]) + len(assigned)
            if occupied >= sections[section.section_id].capacity_max:
                blocking_student_id = assigned[0].student_id if assigned else (
                    fixed_by_section[section.section_id][0].student_id
                    if fixed_by_section[section.section_id] else None
                )
                full_sections.append((section.section_id, blocking_student_id))
        if full_sections:
            section_id, student_id = sorted(full_sections)[0]
            return (
                STUDENT_ASSIGNMENT_SECTION_CAPACITY_EXHAUSTED,
                None,
                section_id,
                student_id,
                (STUDENT_ASSIGNMENT_REQUIRES_ADDITIONAL_CAPACITY,),
            )
    return STUDENT_ASSIGNMENT_NO_ELIGIBLE_SECTION, None, None, None, ()


def _build_lock_costs(data, result):
    """Measure each lock's cost with an internal deterministic relaxation.

    This is result evidence, not the counselor-facing what-if workflow. Each
    comparison removes exactly one active lock from the same immutable input.
    A request counts only when it is unresolved with that lock and becomes
    assigned without it, so overlapping locks are never presented as a claim
    that their individual counts sum to the total unmet demand.
    """

    base_unmet_request_ids = {item.request_id for item in result.unmet_requests}
    costs = []
    for lock in _active_locks(data):
        relaxed_data = replace(
            data,
            student_assignment_locks=tuple(
                item for item in data.student_assignment_locks if item.lock_id != lock.lock_id
            ),
            # Lock-cost evidence is bounded independently of the main run so a
            # large number of locks cannot turn a review request into an
            # unbounded sequence of counterfactual solves.
            time_limit_seconds=min(data.time_limit_seconds, 5.0),
        )
        relaxed_result = _solve_student_assignment(relaxed_data, include_lock_costs=False)
        newly_assigned = {
            item.request_id for item in relaxed_result.assignments
        } & base_unmet_request_ids
        costs.append(StudentAssignmentLockCostDTO(
            lock_id=lock.lock_id,
            attributable_request_count=len(newly_assigned),
            unresolved_request_ids=tuple(sorted(newly_assigned)),
        ))
    return tuple(costs)


def solve_student_assignment(data: StudentAssignmentInputDTO) -> StudentAssignmentResultDTO:
    """Return the best safe recommendation with immutable review evidence."""

    return _solve_student_assignment(data, include_lock_costs=True)


def _solve_student_assignment(data, *, include_lock_costs):
    if data.scope.scope_type == "scoped":
        # Keep the complete request list in the detached run snapshot, but do
        # not let a partial rerun silently rewrite demand outside its approved
        # boundary. The adapter resolves the flag; the engine only consumes it.
        data = replace(
            data,
            requests=tuple(request for request in data.requests if request.is_in_scope),
            priority_request_ids=tuple(
                request_id
                for request_id in data.priority_request_ids
                if any(
                    request.request_id == request_id and request.is_in_scope
                    for request in data.requests
                )
            ),
        )
    offering_sections = _validate_input(data)
    model = cp_model.CpModel()
    sections = {item.section_id: item for item in data.sections}
    requests_by_id = {item.request_id: item for item in data.requests}
    active_locks = _active_locks(data)

    whole_schedule_lock_ids = defaultdict(set)
    frozen_section_lock_ids = defaultdict(set)
    frozen_course_lock_ids = defaultdict(set)
    exact_locks_by_student_course = defaultdict(list)
    teacher_locks_by_student_course = defaultdict(list)
    group_locks = []
    for lock in active_locks:
        if lock.lock_type == LOCK_TYPE_WHOLE_SCHEDULE and lock.student_id is not None:
            whole_schedule_lock_ids[lock.student_id].add(lock.lock_id)
        elif lock.lock_type == LOCK_TYPE_SECTION_ROSTER and lock.section_id is not None:
            frozen_section_lock_ids[lock.section_id].add(lock.lock_id)
        elif lock.lock_type == LOCK_TYPE_COURSE_ROSTER and lock.course_id is not None:
            frozen_course_lock_ids[lock.course_id].add(lock.lock_id)
        elif lock.lock_type == LOCK_TYPE_EXACT_SECTION and lock.student_id is not None and lock.course_id is not None:
            exact_locks_by_student_course[lock.student_id, lock.course_id].append(lock)
        elif lock.lock_type == LOCK_TYPE_STUDENT_TEACHER and lock.student_id is not None and lock.course_id is not None:
            teacher_locks_by_student_course[lock.student_id, lock.course_id].append(lock)
        elif lock.lock_type == LOCK_TYPE_STUDENT_GROUP:
            group_locks.append(lock)

    active_enrollments = [
        row for row in data.fixed_enrollments if _is_active_enrollment(row)
    ]
    for enrollment in active_enrollments:
        if enrollment.section_id not in sections:
            raise ValueError(f"Active enrollment references inactive section {enrollment.section_id}.")

    # A movable enrollment must have a matching request in this run. Without
    # one, releasing its capacity would silently erase a student's accepted
    # course, so it remains fixed even inside a full run.
    potential_movable = []
    fixed_rows = []
    for enrollment in active_enrollments:
        exact_locks = exact_locks_by_student_course[enrollment.student_id, enrollment.course_id]
        is_fixed = (
            not _scope_includes_enrollment(data, enrollment)
            or enrollment.is_locked
            or enrollment.student_id in whole_schedule_lock_ids
            or enrollment.section_id in frozen_section_lock_ids
            or enrollment.course_id in frozen_course_lock_ids
            or any(lock.section_id == enrollment.section_id for lock in exact_locks)
        )
        if is_fixed:
            fixed_rows.append(enrollment)
        else:
            potential_movable.append(enrollment)
    movable_rows = []
    for enrollment in potential_movable:
        if any(_request_matches_enrollment(request, enrollment) for request in data.requests):
            movable_rows.append(enrollment)
        else:
            fixed_rows.append(enrollment)

    movable_by_student_course = defaultdict(list)
    for enrollment in movable_rows:
        movable_by_student_course[enrollment.student_id, enrollment.course_id].append(enrollment)
    if any(len(rows) > 1 for rows in movable_by_student_course.values()):
        raise ValueError("A student/course pair cannot have multiple movable active enrollments.")

    fixed_by_section = defaultdict(list)
    fixed_slots = defaultdict(set)
    fixed_slot_rows = defaultdict(list)
    fixed_courses = defaultdict(list)
    for enrollment in fixed_rows:
        fixed_by_section[enrollment.section_id].append(enrollment)
        fixed_slots[enrollment.student_id].add(enrollment.timeslot_id)
        fixed_slot_rows[enrollment.student_id, enrollment.timeslot_id].append(enrollment)
        fixed_courses[enrollment.student_id, enrollment.course_id].append(enrollment)
    for section_id, rows in fixed_by_section.items():
        if len(rows) > sections[section_id].capacity_max:
            raise ValueError(f"Fixed enrollments exceed capacity for section {section_id}.")

    variables = {}
    request_candidates = {}
    request_lock_blockers = defaultdict(set)
    direct_protected_requests = {}
    previous_enrollment_by_request = {}
    for request in sorted(data.requests, key=lambda item: item.request_id):
        student_course_key = request.student_id, request.course_id
        existing_fixed = fixed_courses[student_course_key]
        if existing_fixed:
            lock_ids = {
                lock_id for row in existing_fixed for lock_id in row.lock_ids
            }
            lock_ids.update(whole_schedule_lock_ids[request.student_id])
            for row in existing_fixed:
                lock_ids.update(frozen_section_lock_ids[row.section_id])
            lock_ids.update(frozen_course_lock_ids[request.course_id])
            for lock in exact_locks_by_student_course[student_course_key]:
                lock_ids.add(lock.lock_id)
            if any(row.is_locked for row in existing_fixed) or lock_ids:
                direct_protected_requests[request.request_id] = min(lock_ids) if lock_ids else None
            request_lock_blockers[request.request_id].update(lock_ids)
            request_candidates[request.request_id] = []
            continue

        movable_rows_for_request = [
            row for row in movable_by_student_course[student_course_key]
            if _request_matches_enrollment(request, row)
        ]
        if movable_rows_for_request:
            previous_enrollment_by_request[request.request_id] = movable_rows_for_request[0]

        if request.student_id in whole_schedule_lock_ids:
            request_lock_blockers[request.request_id].update(whole_schedule_lock_ids[request.student_id])
            request_candidates[request.request_id] = []
            continue
        if request.course_id in frozen_course_lock_ids:
            request_lock_blockers[request.request_id].update(frozen_course_lock_ids[request.course_id])
            request_candidates[request.request_id] = []
            continue

        exact_locks = exact_locks_by_student_course[student_course_key]
        # Two active exact locks must both be true. Their target intersection
        # therefore fails closed when an invalid duplicate configuration names
        # different sections for the same student/course pair.
        allowed_exact_section_ids = (
            {lock.section_id for lock in exact_locks}
            if len({lock.section_id for lock in exact_locks}) == 1
            else set()
        )
        teacher_locks = teacher_locks_by_student_course[student_course_key]
        allowed_teacher_ids = (
            {lock.teacher_id for lock in teacher_locks}
            if len({lock.teacher_id for lock in teacher_locks}) == 1
            else set()
        )
        candidates = []
        for section in offering_sections.get(request.course_offering_id, ()):
            if section.timeslot_id in fixed_slots[request.student_id]:
                continue
            if section.section_id in frozen_section_lock_ids:
                request_lock_blockers[request.request_id].update(
                    frozen_section_lock_ids[section.section_id]
                )
                continue
            if exact_locks and section.section_id not in allowed_exact_section_ids:
                request_lock_blockers[request.request_id].update(lock.lock_id for lock in exact_locks)
                continue
            if teacher_locks and section.teacher_id not in allowed_teacher_ids:
                request_lock_blockers[request.request_id].update(lock.lock_id for lock in teacher_locks)
                continue
            variable = model.NewBoolVar(f"enroll_{request.request_id}_{section.section_id}")
            variables[request.request_id, section.section_id] = variable
            candidates.append((section, variable))
        request_candidates[request.request_id] = candidates

    # Group locks express one indivisible counselor decision. Restricting every
    # member to the same candidate section before capacity constraints prevents
    # a partial group placement from looking like a successful recommendation.
    requests_by_student_course = defaultdict(list)
    for request in data.requests:
        requests_by_student_course[request.student_id, request.course_id].append(request)
    for lock in group_locks:
        members = tuple(sorted(set(lock.member_student_ids)))
        member_requests = [
            requests_by_student_course[student_id, lock.course_id]
            for student_id in members
        ]
        if any(len(rows) != 1 for rows in member_requests):
            for rows in member_requests:
                for request in rows:
                    request_candidates[request.request_id] = []
                    request_lock_blockers[request.request_id].add(lock.lock_id)
            continue
        group_requests = [rows[0] for rows in member_requests]
        fixed_group_sections = {
            row.section_id
            for student_id in members
            for row in fixed_courses[student_id, lock.course_id]
        }
        candidate_sets = [
            {section.section_id for section, _variable in request_candidates[request.request_id]}
            for request in group_requests
        ]
        common_section_ids = set.intersection(*candidate_sets) if candidate_sets else set()
        if fixed_group_sections:
            # A fixed group member establishes the only lawful destination for
            # the movable members. Multiple fixed destinations are already an
            # irreconcilable group lock, so no member may be reassigned.
            common_section_ids &= fixed_group_sections if len(fixed_group_sections) == 1 else set()
        if not common_section_ids:
            for request in group_requests:
                request_candidates[request.request_id] = []
                request_lock_blockers[request.request_id].add(lock.lock_id)
            continue
        for request in group_requests:
            request_candidates[request.request_id] = [
                (section, variable)
                for section, variable in request_candidates[request.request_id]
                if section.section_id in common_section_ids
            ]
        if fixed_group_sections:
            for request in group_requests:
                model.Add(sum(variable for _section, variable in request_candidates[request.request_id]) == 1)
        else:
            for section_id in sorted(common_section_ids):
                member_variables = [
                    next(
                        variable
                        for section, variable in request_candidates[request.request_id]
                        if section.section_id == section_id
                    )
                    for request in group_requests
                ]
                for variable in member_variables[1:]:
                    model.Add(member_variables[0] == variable)

    for candidates in request_candidates.values():
        if candidates:
            model.Add(sum(variable for _section, variable in candidates) <= 1)

    by_section = defaultdict(list)
    by_student_timeslot = defaultdict(list)
    by_student_section = defaultdict(list)
    by_student_course_semester = defaultdict(list)
    for request_id, candidates in request_candidates.items():
        request = requests_by_id[request_id]
        for section, variable in candidates:
            by_section[section.section_id].append(variable)
            by_student_timeslot[request.student_id, section.timeslot_id].append(variable)
            by_student_section[request.student_id, section.section_id].append(variable)
            by_student_course_semester[
                request.student_id, request.course_id, section.semester
            ].append(variable)
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
    # assigned in this target year. Prior completion remains deliberately
    # assumed by the accepted first-release decision.
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
            prerequisite_rows.extend(
                (semester, None)
                for semester in fixed_semesters.get((student_id, edge.prerequisite_id), ())
            )
            dependent_rows.extend(
                (semester, None)
                for semester in fixed_semesters.get((student_id, edge.course_id), ())
            )
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

    objectives = []
    mandatory = [
        variable
        for request in data.requests if request.is_mandatory
        for _section, variable in request_candidates[request.request_id]
    ]
    objectives.append(-sum(mandatory or [0]))
    priority_request_ids = set(data.priority_request_ids)
    priority_rows = [
        variable
        for request in data.requests
        if request.request_id in priority_request_ids and request.is_primary and not request.is_mandatory
        for _section, variable in request_candidates[request.request_id]
    ]
    objectives.append(-sum(priority_rows or [0]))
    for priority_tier in sorted({request.priority_tier for request in data.requests if request.is_primary}):
        rows = [
            variable
            for request in data.requests
            if (
                request.is_primary
                and not request.is_mandatory
                and request.request_id not in priority_request_ids
                and request.priority_tier == priority_tier
            )
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
                difference = model.NewIntVar(
                    -max(left.capacity_max, right.capacity_max),
                    max(left.capacity_max, right.capacity_max),
                    f"utilization_difference_{left.section_id}_{right.section_id}",
                )
                penalty = model.NewIntVar(
                    0,
                    max(left.capacity_max, right.capacity_max),
                    f"utilization_penalty_{left.section_id}_{right.section_id}",
                )
                model.Add(difference == left_count - right_count)
                model.AddAbsEquality(penalty, difference)
                section_balance_terms.append(penalty)
    utilization_level = IMPORTANCE_LEVELS[data.section_utilization_balance_importance]
    if utilization_level:
        soft_objectives[utilization_level].append(sum(section_balance_terms or [0]))

    semester_balance_terms = []
    for student_id in student_ids:
        requested_course_ids = {request.course_id for request in data.requests if request.student_id == student_id}
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
        semester_1 += sum(1 for row in fixed_rows if row.student_id == student_id and row.semester == 1)
        semester_2 += sum(1 for row in fixed_rows if row.student_id == student_id and row.semester == 2)
        penalty = model.NewIntVar(
            0,
            len(data.requests) + len(fixed_rows),
            f"semester_balance_{student_id}",
        )
        model.AddAbsEquality(penalty, semester_1 - semester_2)
        semester_balance_terms.append(penalty)
    semester_level = IMPORTANCE_LEVELS[data.student_semester_balance_importance]
    if semester_level:
        soft_objectives[semester_level].append(sum(semester_balance_terms or [0]))

    preservation_terms = []
    for request_id, enrollment in previous_enrollment_by_request.items():
        preservation_terms.extend(
            variable
            for section, variable in request_candidates[request_id]
            if section.section_id != enrollment.section_id
        )
    preservation_level = SCHEDULE_PRESERVATION_LEVELS[data.schedule_preservation_level]
    if preservation_level:
        # A stronger counselor choice both promotes this objective above lower
        # soft tiers and scales its internal penalty without exposing numeric
        # weights through the public contract.
        soft_objectives[preservation_level].append(
            preservation_level * sum(preservation_terms or [0])
        )
    for level in sorted(soft_objectives, reverse=True):
        objectives.append(sum(soft_objectives[level]))
    # A final opaque-ID objective makes equivalent recommendations stable.
    objectives.append(
        sum(
            (request_id * 100000 + section_id) * variable
            for (request_id, section_id), variable in variables.items()
            if any(
                candidate_section.section_id == section_id and candidate_variable is variable
                for candidate_section, candidate_variable in request_candidates[request_id]
            )
        ) if variables else 0
    )

    initial_assignment_hints = _build_initial_assignment_hints(
        data=data,
        request_candidates=request_candidates,
        fixed_by_section=fixed_by_section,
        fixed_slots=fixed_slots,
        group_locks=group_locks,
    )
    solver, outcome = _solve_lexicographically(
        model,
        objectives,
        data.time_limit_seconds,
        initial_assignment_hints=initial_assignment_hints,
    )
    if solver is None:
        # CP-SAT ``UNKNOWN`` means the bounded solve ended without a proof or a
        # usable candidate. It is not evidence that the scheduling facts are
        # mathematically infeasible, so preserve that distinction for review
        # and for the target-scale benchmark.
        result_status = "infeasible" if outcome == cp_model.INFEASIBLE else "failed"
        result = StudentAssignmentResultDTO(
            status=result_status,
            solver_outcome=_outcome_name(outcome),
            assignments=(),
            unmet_requests=tuple(
                StudentAssignmentUnmetRequestDTO(
                    request_id=item.request_id,
                    student_id=item.student_id,
                    course_id=item.course_id,
                    is_primary=item.is_primary,
                    is_mandatory=item.is_mandatory,
                    assignment_basis=item.assignment_basis,
                    diagnostic_code=STUDENT_ASSIGNMENT_UNRESOLVED_REQUIRED_REQUEST,
                )
                for item in data.requests
            ),
            diagnostics=({"code": NO_COMPLETE_STUDENT_ASSIGNMENT},),
            objective_components={},
            sequence_outcomes=(),
        )
        return replace(result, lock_costs=_build_lock_costs(data, result)) if include_lock_costs else result

    assignments = []
    assigned_request_ids = set()
    selected_by_section = defaultdict(list)
    for request in sorted(data.requests, key=lambda item: item.request_id):
        for section, variable in request_candidates[request.request_id]:
            if solver.Value(variable):
                previous = previous_enrollment_by_request.get(request.request_id)
                assignment = StudentAssignmentDTO(
                    request_id=request.request_id,
                    student_id=request.student_id,
                    section_id=section.section_id,
                    course_offering_id=request.course_offering_id,
                    course_id=request.course_id,
                    semester=section.semester,
                    timeslot_id=section.timeslot_id,
                    assignment_basis=request.assignment_basis,
                    backup_resolution_snapshot=request.backup_resolution_snapshot,
                    previous_enrollment_id=previous.enrollment_id if previous else None,
                    previous_section_id=previous.section_id if previous else None,
                )
                assignments.append(assignment)
                selected_by_section[section.section_id].append(assignment)
                assigned_request_ids.add(request.request_id)
                break

    unmet = []
    diagnostics = []
    for request in data.requests:
        if request.request_id in assigned_request_ids:
            continue
        diagnostic_code, blocking_lock_id, blocking_section_id, blocking_student_id, remediation_codes = (
            _diagnostic_for_unmet_request(
                request=request,
                offering_sections=offering_sections,
                candidates=request_candidates[request.request_id],
                fixed_slots=fixed_slots,
                fixed_slot_rows=fixed_slot_rows,
                request_lock_blockers=request_lock_blockers,
                direct_protected_requests=direct_protected_requests,
                hard_sequence_impossible=hard_sequence_impossible,
                selected_by_section=selected_by_section,
                fixed_by_section=fixed_by_section,
                sections=sections,
            )
        )
        unmet.append(StudentAssignmentUnmetRequestDTO(
            request_id=request.request_id,
            student_id=request.student_id,
            course_id=request.course_id,
            is_primary=request.is_primary,
            is_mandatory=request.is_mandatory,
            assignment_basis=request.assignment_basis,
            diagnostic_code=diagnostic_code,
            blocking_lock_id=blocking_lock_id,
            blocking_section_id=blocking_section_id,
            blocking_student_id=blocking_student_id,
            remediation_codes=remediation_codes,
        ))
        diagnostics.append({
            "code": diagnostic_code,
            "request_id": request.request_id,
            "student_id": request.student_id,
            "course_id": request.course_id,
            **({"blocking_lock_id": blocking_lock_id} if blocking_lock_id is not None else {}),
            **({"blocking_section_id": blocking_section_id} if blocking_section_id is not None else {}),
            **({"blocking_student_id": blocking_student_id} if blocking_student_id is not None else {}),
            **({"remediation_codes": remediation_codes} if remediation_codes else {}),
        })

    required_unmet = [item for item in unmet if item.is_mandatory or item.is_primary]
    if required_unmet:
        diagnostics.append({
            "code": STUDENT_ASSIGNMENT_UNRESOLVED_REQUIRED_REQUEST,
            "request_ids": [item.request_id for item in required_unmet],
        })
    for student_id, prerequisite_id, course_id in sorted(hard_sequence_impossible):
        diagnostics.append({
            "code": STUDENT_ASSIGNMENT_HARD_PREREQUISITE_SEQUENCE_UNAVAILABLE,
            "student_id": student_id,
            "prerequisite_course_id": prerequisite_id,
            "course_id": course_id,
        })

    sequence_outcomes = []
    assigned_courses = defaultdict(dict)
    for row in fixed_rows:
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
                    "satisfied": courses[preference.earlier_course_id] == 1
                    and courses[preference.later_course_id] == 2,
                })

    seat_contention = []
    for section_id, awarded in sorted(selected_by_section.items()):
        competing_request_ids = tuple(sorted({
            request_id
            for request_id, candidates in request_candidates.items()
            if any(section.section_id == section_id for section, _variable in candidates)
        }))
        if len(competing_request_ids) > len(awarded):
            diagnostics.append({
                "code": STUDENT_ASSIGNMENT_LIMITED_SEAT_CONTENTION,
                "section_id": section_id,
                "competing_request_ids": competing_request_ids,
                "awarded_request_ids": tuple(item.request_id for item in awarded),
            })
        seat_contention.append(StudentAssignmentSeatContentionDTO(
            section_id=section_id,
            available_seat_count=sections[section_id].capacity_max - len(fixed_by_section[section_id]),
            awarded_request_ids=tuple(item.request_id for item in awarded),
            competing_request_ids=competing_request_ids,
        ))

    section_balance_facts = []
    for section in sorted(data.sections, key=lambda item: item.section_id):
        enrollment_count = len(fixed_by_section[section.section_id]) + len(selected_by_section.get(section.section_id, ()))
        balance_code = None
        if enrollment_count < section.target_capacity:
            balance_code = STUDENT_ASSIGNMENT_SECTION_BELOW_TARGET_CAPACITY
        elif enrollment_count > section.target_capacity:
            balance_code = STUDENT_ASSIGNMENT_SECTION_OVER_TARGET_CONCENTRATION
        if balance_code:
            diagnostics.append({
                "code": balance_code,
                "section_id": section.section_id,
                "enrollment_count": enrollment_count,
                "target_capacity": section.target_capacity,
            })
        section_balance_facts.append(StudentAssignmentSectionBalanceDTO(
            section_id=section.section_id,
            enrollment_count=enrollment_count,
            target_capacity=section.target_capacity,
            diagnostic_code=balance_code,
        ))

    result = StudentAssignmentResultDTO(
        status="complete" if not required_unmet and not hard_sequence_impossible else "partial",
        solver_outcome=_outcome_name(outcome),
        assignments=tuple(assignments),
        unmet_requests=tuple(unmet),
        diagnostics=tuple(diagnostics),
        objective_components={
            "mandatory_fulfilled": float(sum(
                1 for row in assignments if requests_by_id[row.request_id].is_mandatory
            )),
            "priority_primary_fulfilled": float(sum(
                1 for row in assignments if row.request_id in priority_request_ids
            )),
            "primary_fulfilled": float(sum(
                1 for row in assignments if requests_by_id[row.request_id].is_primary
            )),
            "approved_backup_fulfilled": float(sum(
                1 for row in assignments if not requests_by_id[row.request_id].is_primary
            )),
            "section_utilization_balance_penalty": float(sum(solver.Value(item) for item in section_balance_terms)),
            "student_semester_balance_penalty": float(sum(solver.Value(item) for item in semester_balance_terms)),
            "schedule_preservation_move_penalty": float(sum(solver.Value(item) for item in preservation_terms)),
            "soft_sequence_preferences_satisfied": float(sum(item["satisfied"] for item in sequence_outcomes)),
        },
        sequence_outcomes=tuple(sequence_outcomes),
        seat_contention=tuple(seat_contention),
        section_balance_facts=tuple(section_balance_facts),
    )
    return replace(result, lock_costs=_build_lock_costs(data, result)) if include_lock_costs else result
