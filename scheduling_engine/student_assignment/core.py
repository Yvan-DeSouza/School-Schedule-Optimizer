"""Pure student-to-section assignment over fixed accepted schedule context.

This module intentionally has no Django dependency.  It consumes a detached
snapshot, recommends enrollment creation or replacement facts, and never
changes section timing, rooms, teachers, or persisted enrollment records.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import replace

from ortools.sat.python import cp_model

from ..constants import (
    HALF_SEMESTER_SEGMENTS,
    IMPORTANCE_LEVELS,
    LOCK_TYPES,
    LOCK_TYPE_COURSE_ROSTER,
    LOCK_TYPE_EXACT_SECTION,
    LOCK_TYPE_SECTION_ROSTER,
    LOCK_TYPE_STUDENT_GROUP,
    LOCK_TYPE_STUDENT_TEACHER,
    LOCK_TYPE_WHOLE_SCHEDULE,
    SCHEDULE_PRESERVATION_LEVELS,
    STUDENT_ASSIGNMENT_HARD_FEASIBILITY_TIME_LIMIT_SECONDS,
    STUDENT_ASSIGNMENT_HARD_FEASIBILITY_WORKER_COUNT,
    STUDENT_ASSIGNMENT_HARD_FEASIBILITY_VALIDATION_TIME_LIMIT_SECONDS,
    STUDENT_ASSIGNMENT_HARD_FEASIBILITY_VALIDATION_WORKER_COUNT,
    STUDENT_ASSIGNMENT_OPTIMIZATION_WORKER_COUNT,
)
from ..diagnostics import (
    NO_COMPLETE_STUDENT_ASSIGNMENT,
    STUDENT_ASSIGNMENT_HARD_PREREQUISITE_SEQUENCE_UNAVAILABLE,
    STUDENT_ASSIGNMENT_COMBINED_SECTION_COLLISION,
    STUDENT_ASSIGNMENT_HALF_SEMESTER_UNALLOCATED_OPPOSITE_HALF,
    STUDENT_ASSIGNMENT_LIMITED_SEAT_CONTENTION,
    STUDENT_ASSIGNMENT_LOCKED_ENROLLMENT_BLOCKS_REQUEST,
    STUDENT_ASSIGNMENT_NO_ACTIVE_PLACED_SECTION,
    STUDENT_ASSIGNMENT_ONLINE_HALF_SEMESTER_UNUSED_SUPERVISION_HALF,
    STUDENT_ASSIGNMENT_ONLINE_SUPERVISION_CAPACITY_EXHAUSTED,
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
    STUDENT_ASSIGNMENT_UNALLOCATED_SCHOOL_TIME,
    STUDENT_ASSIGNMENT_NO_VALID_CO_OP_BLOCK_PAIR,
    STUDENT_ASSIGNMENT_NO_VALID_FOCUS_SEMESTER,
    STUDENT_ASSIGNMENT_SPECIAL_COMMITMENT_LOCK_BLOCKS_REQUEST,
)
from ..dto import (
    StudentAssignmentCandidateLedgerDTO,
    StudentAssignmentDTO,
    StudentAssignmentInputDTO,
    StudentAssignmentLockCostDTO,
    StudentAssignmentResultDTO,
    StudentAssignmentSeatContentionDTO,
    StudentAssignmentSectionBalanceDTO,
    StudentAssignmentUnmetRequestDTO,
    StudentScheduleCommitmentAssignmentDTO,
    StudentAssignmentReviewItemDTO,
)
from .validation import (
    is_active_enrollment as _is_active_enrollment,
    request_matches_enrollment as _request_matches_enrollment,
    scope_includes_enrollment as _scope_includes_enrollment,
    validate_input as _validate_input,
)
from .locks import (
    active_locks as _active_locks,
    special_lock_allows_candidate as _special_lock_allows_candidate,
    special_lock_candidates as _special_lock_candidates,
)
from .occupancy import (
    append_unallocated_school_time_review_items as _append_unallocated_school_time_review_items,
    fixed_enrollment_occupied_half_segments as _fixed_enrollment_occupied_half_segments,
    occupied_half_segments as _occupied_half_segments,
    request_occupied_half_segments as _request_occupied_half_segments,
)
from .solver import (
    outcome_name as _outcome_name,
    solve_complete_hard_feasibility_seed as _solve_complete_hard_feasibility_seed,
    solve_lexicographically as _solve_lexicographically,
    validate_complete_hard_feasibility_seed as _validate_complete_hard_feasibility_seed,
)
from .initial_hint import build_initial_assignment_hints as _build_initial_assignment_hints
from .unmet_diagnostics import diagnostic_for_unmet_request as _diagnostic_for_unmet_request
from .lock_costs import build_lock_costs
from .candidate_evidence import (
    CANDIDATE_LEDGER_MAX_REJECTED_ALTERNATIVES,
    append_candidate_rejection as _append_candidate_rejection,
    candidate_rejection as _candidate_rejection,
    new_candidate_ledger_entry as _new_candidate_ledger_entry,
    new_commitment_candidate_evidence as _new_commitment_candidate_evidence,
    new_section_candidate_evidence as _new_section_candidate_evidence,
    public_candidate_evidence as _public_candidate_evidence,
)


def _candidate_sort_key(candidate):
    """Keep counselor evidence stable without reusing database-ID solver ordering."""

    return (
        candidate["candidate_kind"],
        candidate.get("semester") or 0,
        candidate.get("block") or "",
        candidate.get("timeslot_id") or 0,
        candidate.get("section_id") or 0,
        candidate.get("online_supervision_session_id") or 0,
        candidate.get("co_op_block_pair") or "",
    )


def _objective_values(solver, objectives):
    """Read the existing ordered objective vector from an existing solution."""

    if solver is None:
        return ()
    return tuple(
        float(objective)
        if isinstance(objective, (int, float))
        else float(solver.Value(objective))
        for objective in objectives
    )


def _optimization_facts(
    *,
    hard_feasibility_outcome,
    required_group_count,
    hard_seed_solver,
    validated_seed_solver,
    final_solver,
    final_outcome,
    objectives,
):
    """Expose stage handoff and quality facts without changing solver logic."""

    seed_values = _objective_values(validated_seed_solver, objectives)
    final_values = _objective_values(final_solver, objectives)
    improved = bool(seed_values and final_values and final_values < seed_values)
    return {
        "stage_1": {
            "solver_outcome": _outcome_name(hard_feasibility_outcome),
            "required_decision_group_count": required_group_count,
            "complete_seed_produced": hard_seed_solver is not None,
            "seed_validated_against_full_model": validated_seed_solver is not None,
            "objective_values": list(seed_values),
        },
        "stage_2": {
            "solver_outcome": _outcome_name(final_outcome),
            "worker_count": STUDENT_ASSIGNMENT_OPTIMIZATION_WORKER_COUNT,
            "validated_seed_received": validated_seed_solver is not None,
            "objective_values": list(final_values),
            "improved_over_stage_1": improved,
        },
    }


def _candidate_source_from_commitment(commitment):
    if commitment.course_request_id is not None:
        return "course", commitment.course_request_id
    # Candidate recommendations intentionally store their source in the common
    # ``request_id`` field, while persisted fixed commitments retain the more
    # explicit model-origin field. Avoid ``getattr(..., commitment.request_id)``
    # here: its fallback expression would be evaluated even for fixed rows.
    source_request_id = getattr(commitment, "schedule_commitment_request_id", None)
    if source_request_id is None:
        source_request_id = commitment.request_id
    return "commitment", source_request_id


def _final_course_placements(*, assignments, fixed_rows):
    """Index final course context once for bounded alternative evidence."""

    placements = defaultdict(list)
    for assignment in assignments:
        placements[assignment.student_id, assignment.course_id].append(
            (assignment.semester, assignment.request_id)
        )
    for row in fixed_rows:
        placements[row.student_id, row.course_id].append((row.semester, None))
    return placements


def _candidate_prerequisite_rejections(*, request, candidate_semester,
                                       final_course_placements,
                                       hard_prerequisites):
    """Return final-state prerequisite incompatibilities for one alternative.

    This is evidence about the returned recommendation, not a second attempt to
    solve with the candidate forced.  It only names a prerequisite conflict
    when the other side is already present in final or fixed context.
    """

    if not hard_prerequisites:
        return ()
    rejections = []
    for edge in hard_prerequisites:
        if edge.prerequisite_id == request.course_id:
            for dependent_semester, dependent_request_id in final_course_placements[
                request.student_id, edge.course_id
            ]:
                if dependent_request_id == request.request_id:
                    continue
                if candidate_semester == 1 and dependent_semester == 2:
                    continue
                rejections.append(_candidate_rejection(
                    code=STUDENT_ASSIGNMENT_HARD_PREREQUISITE_SEQUENCE_UNAVAILABLE,
                    phase="final",
                    blocking_request_id=dependent_request_id,
                    detail={
                        "prerequisite_course_id": edge.prerequisite_id,
                        "dependent_course_id": edge.course_id,
                    },
                ))
        elif edge.course_id == request.course_id:
            for prerequisite_semester, prerequisite_request_id in final_course_placements[
                request.student_id, edge.prerequisite_id
            ]:
                if prerequisite_request_id == request.request_id:
                    continue
                if prerequisite_semester == 1 and candidate_semester == 2:
                    continue
                rejections.append(_candidate_rejection(
                    code=STUDENT_ASSIGNMENT_HARD_PREREQUISITE_SEQUENCE_UNAVAILABLE,
                    phase="final",
                    blocking_request_id=prerequisite_request_id,
                    detail={
                        "prerequisite_course_id": edge.prerequisite_id,
                        "dependent_course_id": edge.course_id,
                    },
                ))
    return rejections


def _build_candidate_ledger(
    *, data, entries, sections, fixed_rows, fixed_by_section,
    selected_by_section, assignments, commitment_assignments, unmet_requests,
    review_items, has_solution,
):
    """Finalize bounded candidate evidence from one already-completed solve.

    Candidate generation above remains the source of static eligibility.  This
    function only compares those choices with the final immutable result.  It
    deliberately does not force alternatives, change variable domains, or
    invoke CP-SAT again; a candidate with no final hard blocker is labelled as
    insufficient evidence rather than being given an invented explanation.
    """

    unmet_by_request = {item.request_id: item for item in unmet_requests}
    review_codes_by_source = defaultdict(list)
    for item in review_items:
        if item.request_id is not None:
            review_codes_by_source[("course", item.request_id)].append(item.code)
            review_codes_by_source[("commitment", item.request_id)].append(item.code)

    assignment_by_source = {
        ("course", item.request_id): item for item in assignments
    }
    commitment_by_source = {
        _candidate_source_from_commitment(item): item
        for item in commitment_assignments
    }
    fixed_commitments_by_source = {
        _candidate_source_from_commitment(item): item
        for item in data.fixed_schedule_commitments
        if item.is_active and not item.is_historical
        and (
            item.course_request_id is not None
            or item.schedule_commitment_request_id is not None
        )
    }
    request_by_id = {item.request_id: item for item in data.requests}
    final_course_placements = _final_course_placements(
        assignments=assignments,
        fixed_rows=fixed_rows,
    ) if has_solution and data.hard_prerequisites else {}
    occupancy_by_student_slot = defaultdict(list)
    occupancy_by_student_section = defaultdict(list)
    final_rows_by_section = {
        section_id: tuple(fixed_by_section[section_id]) + tuple(
            selected_by_section.get(section_id, ())
        )
        for section_id in sections
    }
    for row in fixed_rows:
        for segment in _fixed_enrollment_occupied_half_segments(row):
            occupancy = {
                "source_key": ("fixed_enrollment", row.enrollment_id),
                "timeslot_id": row.timeslot_id,
                "half_semester_segment": segment,
                "section_id": row.section_id,
                "student_id": row.student_id,
            }
            occupancy_by_student_slot[
                row.student_id, row.timeslot_id, segment
            ].append(occupancy)
            occupancy_by_student_section[
                row.student_id, row.section_id
            ].append(occupancy)
    for item in assignments:
        request = request_by_id[item.request_id]
        engine_section_id = (
            item.section_id
            if item.section_id is not None
            else -item.online_supervision_session_id
        )
        section = sections[engine_section_id]
        for segment in _request_occupied_half_segments(request, section):
            occupancy = {
                "source_key": ("course", item.request_id),
                "timeslot_id": item.timeslot_id,
                "half_semester_segment": segment,
                "section_id": engine_section_id,
                "student_id": item.student_id,
            }
            occupancy_by_student_slot[
                item.student_id, item.timeslot_id, segment
            ].append(occupancy)
            occupancy_by_student_section[
                item.student_id, engine_section_id
            ].append(occupancy)
    for item in commitment_assignments:
        source_key = _candidate_source_from_commitment(item)
        for timeslot_id, segment in item.occupancy:
            occupancy = {
                "source_key": source_key,
                "timeslot_id": timeslot_id,
                "half_semester_segment": segment,
                "section_id": None,
                "student_id": item.student_id,
            }
            occupancy_by_student_slot[
                item.student_id, timeslot_id, segment
            ].append(occupancy)
    for item in fixed_commitments_by_source.values():
        source_key = _candidate_source_from_commitment(item)
        for timeslot_id, segment in item.occupancy:
            occupancy = {
                "source_key": source_key,
                "timeslot_id": timeslot_id,
                "half_semester_segment": segment,
                "section_id": None,
                "student_id": item.student_id,
            }
            occupancy_by_student_slot[
                item.student_id, timeslot_id, segment
            ].append(occupancy)

    ledger = []
    for source_key, entry in sorted(entries.items(), key=lambda item: item[0]):
        selected_assignment = assignment_by_source.get(source_key)
        selected_commitment = commitment_by_source.get(source_key)
        fixed_commitment = fixed_commitments_by_source.get(source_key)
        selected_candidate = None
        if selected_assignment is not None:
            selected_engine_section_id = (
                selected_assignment.section_id
                if selected_assignment.section_id is not None
                else -selected_assignment.online_supervision_session_id
            )
            for candidate in entry["candidates"]:
                if candidate["engine_section_id"] == selected_engine_section_id:
                    candidate["is_selected"] = True
                    selected_candidate = candidate
                    break
        elif selected_commitment is not None:
            selected_occupancy = tuple(selected_commitment.occupancy)
            for candidate in entry["candidates"]:
                if candidate["occupancy"] == selected_occupancy:
                    candidate["is_selected"] = True
                    selected_candidate = candidate
                    break
        elif fixed_commitment is not None:
            selected_candidate = _new_commitment_candidate_evidence(
                candidate_kind=f"{fixed_commitment.commitment_kind}_fixed_context",
                semester=next((
                    slot.semester for slot in data.timeslots
                    if fixed_commitment.occupancy and slot.id == fixed_commitment.occupancy[0][0]
                ), None),
                occupancy=fixed_commitment.occupancy,
                timeslots_by_id={item.id: item for item in data.timeslots},
            )
            selected_candidate["is_selected"] = True

        if has_solution:
            for candidate in entry["candidates"]:
                if candidate["is_selected"] or not candidate["is_statically_eligible"]:
                    continue
                if candidate["engine_section_id"] is not None:
                    section = sections[candidate["engine_section_id"]]
                    occupied_rows = final_rows_by_section[section.section_id]
                    if len(occupied_rows) >= section.capacity_max:
                        blocking_row = occupied_rows[0] if occupied_rows else None
                        _append_candidate_rejection(
                            candidate,
                            _candidate_rejection(
                                code=(
                                    STUDENT_ASSIGNMENT_ONLINE_SUPERVISION_CAPACITY_EXHAUSTED
                                    if section.section_id < 0
                                    else STUDENT_ASSIGNMENT_SECTION_CAPACITY_EXHAUSTED
                                ),
                                phase="final",
                                blocking_section_id=(
                                    None if section.section_id < 0 else section.section_id
                                ),
                                blocking_student_id=(
                                    getattr(blocking_row, "student_id", None)
                                ),
                                blocking_request_id=(
                                    getattr(blocking_row, "request_id", None)
                                ),
                            ),
                            static=False,
                        )
                    if entry["request_kind"] in {"course_request", "co_op_request"}:
                        request = request_by_id.get(entry["request_id"])
                        if request is not None:
                            for rejection in _candidate_prerequisite_rejections(
                                request=request,
                                candidate_semester=candidate["semester"],
                                final_course_placements=final_course_placements,
                                hard_prerequisites=data.hard_prerequisites,
                            ):
                                _append_candidate_rejection(
                                    candidate, rejection, static=False,
                                )
                conflicts = [
                    row
                    for timeslot_id, segment in candidate["occupancy"]
                    for row in occupancy_by_student_slot[
                        entry["student_id"], timeslot_id, segment
                    ]
                    if row["source_key"] != source_key
                ]
                if conflicts:
                    blocking = sorted(
                        conflicts,
                        key=lambda item: (
                            item["timeslot_id"], item["half_semester_segment"],
                            item["source_key"],
                        ),
                    )[0]
                    _append_candidate_rejection(
                        candidate,
                        _candidate_rejection(
                            code=STUDENT_ASSIGNMENT_TIMESLOT_COLLISION,
                            phase="final",
                            blocking_section_id=blocking["section_id"],
                            blocking_student_id=blocking["student_id"],
                            blocking_request_id=(
                                blocking["source_key"][1]
                                if blocking["source_key"][0] in {"course", "commitment"}
                                else None
                            ),
                        ),
                        static=False,
                    )
                if candidate["engine_section_id"] is not None:
                    shared_section_rows = [
                        row for row in occupancy_by_student_section[
                            entry["student_id"], candidate["engine_section_id"]
                        ]
                        if row["source_key"] != source_key
                    ]
                    if shared_section_rows:
                        blocking = shared_section_rows[0]
                        _append_candidate_rejection(
                            candidate,
                            _candidate_rejection(
                                code=STUDENT_ASSIGNMENT_COMBINED_SECTION_COLLISION,
                                phase="final",
                                blocking_section_id=(
                                    candidate["engine_section_id"]
                                    if candidate["engine_section_id"] > 0 else None
                                ),
                                blocking_student_id=blocking["student_id"],
                                blocking_request_id=(
                                    blocking["source_key"][1]
                                    if blocking["source_key"][0] == "course" else None
                                ),
                            ),
                            static=False,
                        )

        rejected = [
            candidate for candidate in entry["candidates"] if not candidate["is_selected"]
        ]
        rejected.sort(key=_candidate_sort_key)
        visible_rejected = rejected[:CANDIDATE_LEDGER_MAX_REJECTED_ALTERNATIVES]
        unresolved = unmet_by_request.get(entry["request_id"])
        review_item_codes = tuple(sorted(set(review_codes_by_source[source_key])))
        if selected_candidate is not None:
            selection_state = "fixed_context" if fixed_commitment is not None else "selected"
            unresolved_reason_code = None
            selection_factors = (
                {"kind": "fixed_context"},
            ) if fixed_commitment is not None else ()
        elif unresolved is not None:
            selection_state = "unresolved"
            unresolved_reason_code = unresolved.diagnostic_code
            selection_factors = (_candidate_rejection(
                code=unresolved.diagnostic_code,
                phase="final",
                blocking_lock_id=unresolved.blocking_lock_id,
                blocking_section_id=unresolved.blocking_section_id,
                blocking_student_id=unresolved.blocking_student_id,
            ),)
        elif review_item_codes:
            selection_state = "review_required"
            unresolved_reason_code = review_item_codes[0]
            selection_factors = tuple(
                _candidate_rejection(code=code, phase="final")
                for code in review_item_codes
            )
        else:
            selection_state = "no_solver_incumbent" if not has_solution else "not_selected"
            unresolved_reason_code = None
            selection_factors = ({"kind": "insufficient_evidence"},)

        ledger.append(StudentAssignmentCandidateLedgerDTO(
            request_id=entry["request_id"],
            student_id=entry["student_id"],
            request_kind=entry["request_kind"],
            course_id=entry["course_id"],
            course_offering_id=entry["course_offering_id"],
            assignment_basis=entry["assignment_basis"],
            delivery_kind=entry["delivery_kind"],
            duration=entry["duration"],
            half_semester_segment=entry["half_semester_segment"],
            paired_half_course_id=entry["paired_half_course_id"],
            selection_state=selection_state,
            unresolved_reason_code=unresolved_reason_code,
            selected_candidate=(
                _public_candidate_evidence(selected_candidate)
                if selected_candidate is not None else None
            ),
            static_candidate_count=len(entry["candidates"]),
            statically_eligible_candidate_count=sum(
                candidate["is_statically_eligible"] for candidate in entry["candidates"]
            ),
            recorded_rejected_candidate_count=len(visible_rejected),
            omitted_rejected_candidate_count=len(rejected) - len(visible_rejected),
            alternatives=tuple(
                _public_candidate_evidence(candidate)
                for candidate in visible_rejected
            ),
            selection_factors=selection_factors,
            review_item_codes=review_item_codes,
        ))
    return tuple(ledger)


def _build_lock_costs(data, result):
    return build_lock_costs(
        data,
        result,
        solve_without_lock_costs=lambda relaxed_data: _solve_student_assignment(
            relaxed_data,
            include_lock_costs=False,
            include_candidate_ledger=False,
            use_hard_feasibility_bootstrap=False,
        ),
    )


def solve_student_assignment(
    data: StudentAssignmentInputDTO,
    *,
    use_hard_feasibility_bootstrap=True,
) -> StudentAssignmentResultDTO:
    """Return the best safe recommendation with immutable review evidence."""

    return _solve_student_assignment(
        data,
        include_lock_costs=True,
        include_candidate_ledger=True,
        use_hard_feasibility_bootstrap=use_hard_feasibility_bootstrap,
    )


def _solve_student_assignment(
    data,
    *,
    include_lock_costs,
    include_candidate_ledger=True,
    use_hard_feasibility_bootstrap=True,
):
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
    timeslots_by_id = {item.id: item for item in data.timeslots}
    requests_by_id = {item.request_id: item for item in data.requests}
    active_locks = _active_locks(data)
    candidate_ledger_entries = {}

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
        for segment in _fixed_enrollment_occupied_half_segments(enrollment):
            fixed_slots[enrollment.student_id].add((enrollment.timeslot_id, segment))
            fixed_slot_rows[
                enrollment.student_id, enrollment.timeslot_id, segment
            ].append(enrollment)
        fixed_courses[enrollment.student_id, enrollment.course_id].append(enrollment)
    for section_id, rows in fixed_by_section.items():
        if len(rows) > sections[section_id].capacity_max:
            raise ValueError(f"Fixed enrollments exceed capacity for section {section_id}.")

    # Existing Study, Co-op, and Focus commitments follow the same scoped-rerun
    # rule as enrollments: outside scope or locked means fixed occupied student
    # time; only in-scope unlocked commitments may be reconsidered.
    fixed_commitment_sources = set()
    movable_commitments_by_source = {}
    for commitment in data.fixed_schedule_commitments:
        if not commitment.is_active or commitment.is_historical:
            continue
        source_key = (
            "course", commitment.course_request_id
        ) if commitment.course_request_id is not None else (
            "commitment", commitment.schedule_commitment_request_id
        )
        if source_key[1] is None:
            raise ValueError("Active special commitment lacks immutable source-request provenance.")
        is_fixed = (
            not commitment.is_in_scope
            or commitment.is_locked
            or commitment.student_id in whole_schedule_lock_ids
        )
        if is_fixed:
            fixed_commitment_sources.add(source_key)
            for timeslot_id, segment in commitment.occupancy:
                fixed_slots[commitment.student_id].add((timeslot_id, segment))
                fixed_slot_rows[commitment.student_id, timeslot_id, segment].append(commitment)
        else:
            movable_commitments_by_source[source_key] = commitment

    variables = {}
    request_candidates = {}
    request_lock_blockers = defaultdict(set)
    direct_protected_requests = {}
    previous_enrollment_by_request = {}
    special_locks_by_course_request = defaultdict(list)
    for lock in data.special_commitment_locks:
        if lock.is_active and lock.course_request_id is not None:
            special_locks_by_course_request[lock.course_request_id].append(lock)
    for request in sorted(data.requests, key=lambda item: item.request_id):
        source_key = ("course", request.request_id)
        ledger_entry = _new_candidate_ledger_entry(
            source_key=source_key,
            student_id=request.student_id,
            request_kind="course_request",
            course_id=request.course_id,
            course_offering_id=request.course_offering_id,
            assignment_basis=request.assignment_basis,
            delivery_kind=request.delivery_kind,
            duration=request.duration,
            half_semester_segment=request.half_semester_segment,
            paired_half_course_id=request.paired_half_course_id,
        )
        candidate_ledger_entries[source_key] = ledger_entry
        if request.delivery_kind == "co_op":
            # Co-op is fulfilled by its paired external commitment below. It
            # must not be forced through a normal instructional section merely
            # because it is still an academic two-credit course request.
            request_candidates[request.request_id] = []
            continue
        potential_sections = tuple(offering_sections.get(request.course_offering_id, ()))
        candidate_by_section_id = {
            section.section_id: _new_section_candidate_evidence(
                request=request,
                section=section,
                timeslots_by_id=timeslots_by_id,
            )
            for section in potential_sections
        }
        ledger_entry["candidates"].extend(candidate_by_section_id.values())
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
            for candidate in candidate_by_section_id.values():
                _append_candidate_rejection(
                    candidate,
                    _candidate_rejection(
                        code=STUDENT_ASSIGNMENT_LOCKED_ENROLLMENT_BLOCKS_REQUEST,
                        phase="static",
                        blocking_lock_id=min(lock_ids) if lock_ids else None,
                    ),
                    static=True,
                )
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
            for candidate in candidate_by_section_id.values():
                _append_candidate_rejection(
                    candidate,
                    _candidate_rejection(
                        code=STUDENT_ASSIGNMENT_LOCKED_ENROLLMENT_BLOCKS_REQUEST,
                        phase="static",
                        blocking_lock_id=min(whole_schedule_lock_ids[request.student_id]),
                    ),
                    static=True,
                )
            request_candidates[request.request_id] = []
            continue
        if request.course_id in frozen_course_lock_ids:
            request_lock_blockers[request.request_id].update(frozen_course_lock_ids[request.course_id])
            for candidate in candidate_by_section_id.values():
                _append_candidate_rejection(
                    candidate,
                    _candidate_rejection(
                        code=STUDENT_ASSIGNMENT_LOCKED_ENROLLMENT_BLOCKS_REQUEST,
                        phase="static",
                        blocking_lock_id=min(frozen_course_lock_ids[request.course_id]),
                    ),
                    static=True,
                )
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
        special_locks = special_locks_by_course_request[request.request_id]
        exact_special_timeslot_ids = {
            lock.timeslot_id for lock in special_locks
            if lock.lock_mode == "exact" and lock.timeslot_id is not None
        }
        excluded_special_timeslot_ids = {
            lock.timeslot_id for lock in special_locks
            if lock.lock_mode == "exclude" and lock.timeslot_id is not None
        }
        if len(exact_special_timeslot_ids) > 1:
            request_lock_blockers[request.request_id].update(
                lock.lock_id for lock in special_locks if lock.lock_mode == "exact"
            )
            blocking_lock_id = min(
                lock.lock_id for lock in special_locks if lock.lock_mode == "exact"
            )
            for candidate in candidate_by_section_id.values():
                _append_candidate_rejection(
                    candidate,
                    _candidate_rejection(
                        code=STUDENT_ASSIGNMENT_SPECIAL_COMMITMENT_LOCK_BLOCKS_REQUEST,
                        phase="static",
                        blocking_lock_id=blocking_lock_id,
                    ),
                    static=True,
                )
            request_candidates[request.request_id] = []
            continue
        candidates = []
        for section in potential_sections:
            candidate = candidate_by_section_id[section.section_id]
            colliding_rows = [
                row
                for segment in _occupied_half_segments(section.half_semester_segment)
                if (section.timeslot_id, segment) in fixed_slots[request.student_id]
                for row in fixed_slot_rows[request.student_id, section.timeslot_id, segment]
            ]
            if colliding_rows:
                blocking_row = colliding_rows[0]
                _append_candidate_rejection(
                    candidate,
                    _candidate_rejection(
                        code=STUDENT_ASSIGNMENT_TIMESLOT_COLLISION,
                        phase="static",
                        blocking_lock_id=(
                            blocking_row.lock_ids[0]
                            if getattr(blocking_row, "lock_ids", ()) else None
                        ),
                        blocking_section_id=getattr(blocking_row, "section_id", None),
                        blocking_student_id=getattr(blocking_row, "student_id", None),
                    ),
                    static=True,
                )
                continue
            if section.section_id in frozen_section_lock_ids:
                request_lock_blockers[request.request_id].update(
                    frozen_section_lock_ids[section.section_id]
                )
                _append_candidate_rejection(
                    candidate,
                    _candidate_rejection(
                        code=STUDENT_ASSIGNMENT_LOCKED_ENROLLMENT_BLOCKS_REQUEST,
                        phase="static",
                        blocking_lock_id=min(frozen_section_lock_ids[section.section_id]),
                        blocking_section_id=section.section_id,
                    ),
                    static=True,
                )
                continue
            if exact_locks and section.section_id not in allowed_exact_section_ids:
                request_lock_blockers[request.request_id].update(lock.lock_id for lock in exact_locks)
                _append_candidate_rejection(
                    candidate,
                    _candidate_rejection(
                        code=STUDENT_ASSIGNMENT_LOCKED_ENROLLMENT_BLOCKS_REQUEST,
                        phase="static",
                        blocking_lock_id=min(lock.lock_id for lock in exact_locks),
                        blocking_section_id=section.section_id,
                    ),
                    static=True,
                )
                continue
            if teacher_locks and section.teacher_id not in allowed_teacher_ids:
                request_lock_blockers[request.request_id].update(lock.lock_id for lock in teacher_locks)
                _append_candidate_rejection(
                    candidate,
                    _candidate_rejection(
                        code=STUDENT_ASSIGNMENT_LOCKED_ENROLLMENT_BLOCKS_REQUEST,
                        phase="static",
                        blocking_lock_id=min(lock.lock_id for lock in teacher_locks),
                        blocking_section_id=section.section_id,
                    ),
                    static=True,
                )
                continue
            if request.delivery_kind == "online":
                # A counselor restriction names the student's supervision
                # block, never an academic section or supervisor identity.
                if exact_special_timeslot_ids and section.timeslot_id not in exact_special_timeslot_ids:
                    request_lock_blockers[request.request_id].update(
                        lock.lock_id for lock in special_locks if lock.lock_mode == "exact"
                    )
                    _append_candidate_rejection(
                        candidate,
                        _candidate_rejection(
                            code=STUDENT_ASSIGNMENT_SPECIAL_COMMITMENT_LOCK_BLOCKS_REQUEST,
                            phase="static",
                            blocking_lock_id=min(
                                lock.lock_id for lock in special_locks
                                if lock.lock_mode == "exact"
                            ),
                        ),
                        static=True,
                    )
                    continue
                if section.timeslot_id in excluded_special_timeslot_ids:
                    request_lock_blockers[request.request_id].update(
                        lock.lock_id for lock in special_locks if lock.lock_mode == "exclude"
                    )
                    _append_candidate_rejection(
                        candidate,
                        _candidate_rejection(
                            code=STUDENT_ASSIGNMENT_SPECIAL_COMMITMENT_LOCK_BLOCKS_REQUEST,
                            phase="static",
                            blocking_lock_id=min(
                                lock.lock_id for lock in special_locks
                                if lock.lock_mode == "exclude"
                            ),
                        ),
                        static=True,
                    )
                    continue
            variable = model.NewBoolVar(f"enroll_{request.request_id}_{section.section_id}")
            variables[request.request_id, section.section_id] = variable
            candidates.append((section, variable))
        request_candidates[request.request_id] = candidates

    # The catalog pair is intentionally narrow: it models the school's two
    # known trimestre courses, not arbitrary partial-duration combinations.
    # When a student requests both, a normal instructional placement may only
    # select the corresponding first/second-half section pair.  Either request
    # can remain unresolved for counselor review; the engine never invents its
    # missing partner or permits two unrelated half-course placements.
    requests_by_half_pair = defaultdict(list)
    for request in data.requests:
        if request.duration == "half_semester" and request.paired_half_course_id:
            pair_key = tuple(sorted((request.course_id, request.paired_half_course_id)))
            requests_by_half_pair[request.student_id, pair_key].append(request)
    for _pair_key, pair_requests in requests_by_half_pair.items():
        if len(pair_requests) != 2:
            continue
        left, right = sorted(pair_requests, key=lambda item: item.request_id)
        for left_section, left_variable in request_candidates[left.request_id]:
            for right_section, right_variable in request_candidates[right.request_id]:
                compatible_normal_pair = (
                    left_section.half_semester_pair_key
                    and left_section.half_semester_pair_key == right_section.half_semester_pair_key
                )
                compatible_online_pair = (
                    left.delivery_kind == "online"
                    and right.delivery_kind == "online"
                    and left_section.section_id == right_section.section_id
                )
                if not compatible_normal_pair and not compatible_online_pair:
                    model.Add(left_variable + right_variable <= 1)

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
                    for candidate in candidate_ledger_entries[
                        ("course", request.request_id)
                    ]["candidates"]:
                        if candidate["is_statically_eligible"]:
                            _append_candidate_rejection(
                                candidate,
                                _candidate_rejection(
                                    code=STUDENT_ASSIGNMENT_LOCKED_ENROLLMENT_BLOCKS_REQUEST,
                                    phase="static",
                                    blocking_lock_id=lock.lock_id,
                                ),
                                static=True,
                            )
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
                for candidate in candidate_ledger_entries[
                    ("course", request.request_id)
                ]["candidates"]:
                    if candidate["is_statically_eligible"]:
                        _append_candidate_rejection(
                            candidate,
                            _candidate_rejection(
                                code=STUDENT_ASSIGNMENT_LOCKED_ENROLLMENT_BLOCKS_REQUEST,
                                phase="static",
                                blocking_lock_id=lock.lock_id,
                            ),
                            static=True,
                        )
            continue
        for request in group_requests:
            request_candidates[request.request_id] = [
                (section, variable)
                for section, variable in request_candidates[request.request_id]
                if section.section_id in common_section_ids
            ]
            for candidate in candidate_ledger_entries[
                ("course", request.request_id)
            ]["candidates"]:
                if (
                    candidate["is_statically_eligible"]
                    and candidate["engine_section_id"] not in common_section_ids
                ):
                    _append_candidate_rejection(
                        candidate,
                        _candidate_rejection(
                            code=STUDENT_ASSIGNMENT_LOCKED_ENROLLMENT_BLOCKS_REQUEST,
                            phase="static",
                            blocking_lock_id=lock.lock_id,
                        ),
                        static=True,
                    )
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
            for segment in _occupied_half_segments(section.half_semester_segment):
                by_student_timeslot[request.student_id, section.timeslot_id, segment].append(variable)
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

    # Study and Focus are requested commitments, never gaps that the engine is
    # permitted to fill automatically. Co-op is an academic request but has no
    # normal section; its two-credit program is represented by one paired-time
    # commitment so it cannot be split into unrelated one-block classes.
    commitment_variables = {}
    commitment_candidates = {}
    commitment_metadata = {}
    available_timeslots = tuple(
        slot for slot in data.timeslots
        if slot.is_available and slot.academic_year_id == data.academic_year_id
    )
    slots_by_semester_block = {
        (slot.semester, slot.block): slot for slot in available_timeslots
    }

    for request in data.schedule_commitment_requests:
        source_key = ("commitment", request.request_id)
        ledger_entry = _new_candidate_ledger_entry(
            source_key=source_key,
            student_id=request.student_id,
            request_kind=f"{request.commitment_type}_commitment_request",
        )
        candidate_ledger_entries[source_key] = ledger_entry
        if not request.is_in_scope or source_key in fixed_commitment_sources:
            continue
        kind = request.commitment_type
        if kind == "study":
            locks = _special_lock_candidates(
                data.special_commitment_locks,
                lock_type="study_time",
                request_id=request.request_id,
            )
            choices = []
            for slot in available_timeslots:
                occupancy = tuple((slot.id, segment) for segment in HALF_SEMESTER_SEGMENTS)
                candidate = _new_commitment_candidate_evidence(
                    candidate_kind="study_time",
                    semester=slot.semester,
                    occupancy=occupancy,
                    timeslots_by_id=timeslots_by_id,
                )
                ledger_entry["candidates"].append(candidate)
                colliding_rows = [
                    row
                    for timeslot_id, segment in occupancy
                    for row in fixed_slot_rows[request.student_id, timeslot_id, segment]
                ]
                if colliding_rows:
                    blocking_row = colliding_rows[0]
                    _append_candidate_rejection(
                        candidate,
                        _candidate_rejection(
                            code=STUDENT_ASSIGNMENT_TIMESLOT_COLLISION,
                            phase="static",
                            blocking_lock_id=(
                                blocking_row.lock_ids[0]
                                if getattr(blocking_row, "lock_ids", ()) else None
                            ),
                            blocking_section_id=getattr(blocking_row, "section_id", None),
                            blocking_student_id=getattr(blocking_row, "student_id", None),
                        ),
                        static=True,
                    )
                    continue
                if not _special_lock_allows_candidate(
                    locks, timeslot_id=slot.id, semester=slot.semester,
                ):
                    _append_candidate_rejection(
                        candidate,
                        _candidate_rejection(
                            code=STUDENT_ASSIGNMENT_SPECIAL_COMMITMENT_LOCK_BLOCKS_REQUEST,
                            phase="static",
                            blocking_lock_id=min(lock.lock_id for lock in locks),
                        ),
                        static=True,
                    )
                    continue
                choices.append((slot.id, occupancy, None))
            commitment_candidates[source_key] = choices
            commitment_metadata[source_key] = (request.student_id, "study", None, None, None)
        elif kind == "focus":
            locks = _special_lock_candidates(
                data.special_commitment_locks,
                lock_type="focus_semester",
                request_id=request.request_id,
            )
            choices = []
            for semester in (1, 2):
                semester_slots = [slot for slot in available_timeslots if slot.semester == semester]
                occupancy = tuple(
                    (slot.id, segment)
                    for slot in semester_slots
                    for segment in HALF_SEMESTER_SEGMENTS
                )
                candidate = _new_commitment_candidate_evidence(
                    candidate_kind="focus_semester",
                    semester=semester,
                    occupancy=occupancy,
                    timeslots_by_id=timeslots_by_id,
                )
                ledger_entry["candidates"].append(candidate)
                if len(semester_slots) != 4:
                    _append_candidate_rejection(
                        candidate,
                        _candidate_rejection(
                            code=STUDENT_ASSIGNMENT_NO_VALID_FOCUS_SEMESTER,
                            phase="static",
                        ),
                        static=True,
                    )
                    continue
                colliding_rows = [
                    row
                    for timeslot_id, segment in occupancy
                    for row in fixed_slot_rows[request.student_id, timeslot_id, segment]
                ]
                if colliding_rows:
                    blocking_row = colliding_rows[0]
                    _append_candidate_rejection(
                        candidate,
                        _candidate_rejection(
                            code=STUDENT_ASSIGNMENT_TIMESLOT_COLLISION,
                            phase="static",
                            blocking_lock_id=(
                                blocking_row.lock_ids[0]
                                if getattr(blocking_row, "lock_ids", ()) else None
                            ),
                            blocking_section_id=getattr(blocking_row, "section_id", None),
                            blocking_student_id=getattr(blocking_row, "student_id", None),
                        ),
                        static=True,
                    )
                    continue
                if not _special_lock_allows_candidate(locks, semester=semester):
                    _append_candidate_rejection(
                        candidate,
                        _candidate_rejection(
                            code=STUDENT_ASSIGNMENT_SPECIAL_COMMITMENT_LOCK_BLOCKS_REQUEST,
                            phase="static",
                            blocking_lock_id=min(lock.lock_id for lock in locks),
                        ),
                        static=True,
                    )
                    continue
                choices.append((semester, occupancy, None))
            commitment_candidates[source_key] = choices
            commitment_metadata[source_key] = (request.student_id, "focus", None, None, None)

    for request in data.requests:
        if request.delivery_kind != "co_op":
            continue
        source_key = ("course", request.request_id)
        ledger_entry = candidate_ledger_entries[source_key]
        ledger_entry["request_kind"] = "co_op_request"
        if not request.is_in_scope or source_key in fixed_commitment_sources:
            continue
        locks = _special_lock_candidates(
            data.special_commitment_locks,
            lock_type="co_op_time",
            request_id=request.request_id,
        )
        choices = []
        for semester in (1, 2):
            for pair, blocks in (("a_b", ("A", "B")), ("c_d", ("C", "D"))):
                slots = [slots_by_semester_block.get((semester, block)) for block in blocks]
                occupancy = tuple(
                    (slot.id, segment)
                    for slot in slots if slot is not None
                    for segment in HALF_SEMESTER_SEGMENTS
                )
                candidate = _new_commitment_candidate_evidence(
                    candidate_kind="co_op_block_pair",
                    semester=semester,
                    occupancy=occupancy,
                    timeslots_by_id=timeslots_by_id,
                    co_op_block_pair=pair,
                )
                ledger_entry["candidates"].append(candidate)
                if any(slot is None for slot in slots):
                    _append_candidate_rejection(
                        candidate,
                        _candidate_rejection(
                            code=STUDENT_ASSIGNMENT_NO_VALID_CO_OP_BLOCK_PAIR,
                            phase="static",
                        ),
                        static=True,
                    )
                    continue
                colliding_rows = [
                    row
                    for timeslot_id, segment in occupancy
                    for row in fixed_slot_rows[request.student_id, timeslot_id, segment]
                ]
                if colliding_rows:
                    blocking_row = colliding_rows[0]
                    _append_candidate_rejection(
                        candidate,
                        _candidate_rejection(
                            code=STUDENT_ASSIGNMENT_TIMESLOT_COLLISION,
                            phase="static",
                            blocking_lock_id=(
                                blocking_row.lock_ids[0]
                                if getattr(blocking_row, "lock_ids", ()) else None
                            ),
                            blocking_section_id=getattr(blocking_row, "section_id", None),
                            blocking_student_id=getattr(blocking_row, "student_id", None),
                        ),
                        static=True,
                    )
                    continue
                if not _special_lock_allows_candidate(
                    locks, semester=semester, co_op_block_pair=pair,
                ):
                    _append_candidate_rejection(
                        candidate,
                        _candidate_rejection(
                            code=STUDENT_ASSIGNMENT_SPECIAL_COMMITMENT_LOCK_BLOCKS_REQUEST,
                            phase="static",
                            blocking_lock_id=min(lock.lock_id for lock in locks),
                        ),
                        static=True,
                    )
                    continue
                choices.append((semester, occupancy, pair))
        commitment_candidates[source_key] = choices
        commitment_metadata[source_key] = (
            request.student_id, "co_op", request.request_id,
            request.course_offering_id, request.course_id,
        )

    for source_key, choices in commitment_candidates.items():
        student_id, _kind, _request_id, _offering_id, _course_id = commitment_metadata[source_key]
        rows = []
        for index, (placement_value, occupancy, pair) in enumerate(choices):
            variable = model.NewBoolVar(f"commitment_{source_key[0]}_{source_key[1]}_{index}")
            commitment_variables[source_key, index] = variable
            rows.append(variable)
            for timeslot_id, segment in occupancy:
                by_student_timeslot[student_id, timeslot_id, segment].append(variable)
        if rows:
            # Each special request has at most one recommendation. It remains
            # optional in the diagnostic model so a conflict becomes a truthful
            # review item rather than an opaque whole-model infeasibility.
            model.Add(sum(rows) <= 1)

    # The time collision index receives both course and commitment variables;
    # add or strengthen its constraints after all candidate families exist.
    for rows in by_student_timeslot.values():
        model.Add(sum(rows) <= 1)

    # Same-year hard prerequisites apply only when both courses are actually
    # assigned in this target year. Prior completion remains deliberately
    # assumed by the accepted first-release decision.
    student_ids = (
        {request.student_id for request in data.requests}
        | {request.student_id for request in data.schedule_commitment_requests}
        | {commitment.student_id for commitment in data.fixed_schedule_commitments}
        | set(fixed_slots)
    )
    # Objective construction below repeatedly needs the same per-student
    # request, fixed-context, commitment, and candidate views. Build those
    # views once so the optional balance objectives do not turn a school-wide
    # model build into repeated scans of every request/candidate pair.
    requests_by_student = defaultdict(list)
    request_course_ids_by_student = defaultdict(set)
    credit_by_student_course = {}
    for request in data.requests:
        requests_by_student[request.student_id].append(request)
        request_course_ids_by_student[request.student_id].add(request.course_id)
        credit_by_student_course[request.student_id, request.course_id] = request.credit_value
    fixed_rows_by_student = defaultdict(list)
    for row in fixed_rows:
        fixed_rows_by_student[row.student_id].append(row)
    active_commitments_by_student = defaultdict(list)
    for commitment in data.fixed_schedule_commitments:
        if commitment.is_active and not commitment.is_historical:
            active_commitments_by_student[commitment.student_id].append(commitment)
    commitment_variables_by_student = defaultdict(list)
    for (source_key, index), variable in commitment_variables.items():
        student_id = commitment_metadata[source_key][0]
        commitment_variables_by_student[student_id].append((
            source_key,
            index,
            variable,
        ))
    candidate_variables_by_student_course_segment = defaultdict(list)
    for request_id, candidates in request_candidates.items():
        request = requests_by_id[request_id]
        for section, variable in candidates:
            for segment in _request_occupied_half_segments(request, section):
                candidate_variables_by_student_course_segment[
                    request.student_id,
                    request.course_id,
                    section.semester,
                    segment,
                ].append(variable)
    fixed_semesters = {
        (student_id, course_id): {row.semester for row in rows}
        for (student_id, course_id), rows in fixed_courses.items()
    }
    hard_sequence_impossible = set()
    for edge in data.hard_prerequisites:
        for student_id in student_ids:
            prerequisite_rows = [
                (semester, variable)
                for semester in (1, 2)
                for variable in by_student_course_semester[
                    student_id, edge.prerequisite_id, semester
                ]
            ]
            dependent_rows = [
                (semester, variable)
                for semester in (1, 2)
                for variable in by_student_course_semester[
                    student_id, edge.course_id, semester
                ]
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

    # The clone below is the shared hard-model boundary for the two-stage
    # architecture.  It is deliberately taken after every source decision and
    # hard rule has been added, but before any soft objective auxiliary exists.
    # A complete seed requires the same sources that make a returned result
    # approvable: mandatory and primary course requests plus every movable
    # requested special commitment.  Fixed context is already an accepted fact,
    # so it is not re-selected by the seed model.
    complete_required_decision_groups = []
    for request in data.requests:
        source_key = ("course", request.request_id)
        if request.delivery_kind == "co_op":
            if source_key not in fixed_commitment_sources:
                complete_required_decision_groups.append([
                    variable
                    for (candidate_source_key, _index), variable in commitment_variables.items()
                    if candidate_source_key == source_key
                ])
            continue
        if not (request.is_mandatory or request.is_primary):
            continue
        if fixed_courses[request.student_id, request.course_id]:
            continue
        complete_required_decision_groups.append([
            variable for _section, variable in request_candidates[request.request_id]
        ])
    for source_key in commitment_candidates:
        if source_key[0] == "commitment" and source_key not in fixed_commitment_sources:
            complete_required_decision_groups.append([
                variable
                for (candidate_source_key, _index), variable in commitment_variables.items()
                if candidate_source_key == source_key
            ])
    if hard_sequence_impossible:
        # Fixed context already violates a same-year prerequisite. No model can
        # turn that into an approvable complete candidate, so the seed must
        # fail closed instead of acting as if source variables could repair it.
        complete_required_decision_groups.append([])
    hard_feasibility_model = model.Clone()

    objectives = []
    mandatory = [
        variable
        for request in data.requests if request.is_mandatory
        for _section, variable in request_candidates[request.request_id]
    ]
    # Study/Focus requests and Co-op's paired outside-school commitment are
    # counselor-recognized requirements. Their optional CP-SAT variables allow
    # useful diagnostics, while this top tier makes fulfillment authoritative.
    mandatory.extend(commitment_variables.values())
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

    # Focus is an intentional absence from this school for a full term. It
    # must remove a student from local term-balance objectives rather than
    # making their ordinary-school semester look artificially overloaded.
    focus_student_ids = {
        request.student_id
        for request in data.schedule_commitment_requests
        if request.commitment_type == "focus"
    } | {
        commitment.student_id
        for commitment in data.fixed_schedule_commitments
        if commitment.is_active
        and not commitment.is_historical
        and commitment.commitment_kind == "focus"
    }

    semester_balance_terms = []
    semester_by_timeslot = {slot.id: slot.semester for slot in data.timeslots}
    for student_id in student_ids:
        if student_id in focus_student_ids:
            continue
        requested_course_ids = request_course_ids_by_student[student_id]
        semester_1 = sum(
            round(credit_by_student_course.get((student_id, course_id), 1.0) * 2) * variable
            for course_id in requested_course_ids
            for variable in by_student_course_semester[student_id, course_id, 1]
        )
        semester_2 = sum(
            round(credit_by_student_course.get((student_id, course_id), 1.0) * 2) * variable
            for course_id in requested_course_ids
            for variable in by_student_course_semester[student_id, course_id, 2]
        )
        semester_1 += sum(
            round(row.credit_value * 2)
            for row in fixed_rows_by_student[student_id] if row.semester == 1
        )
        semester_2 += sum(
            round(row.credit_value * 2)
            for row in fixed_rows_by_student[student_id] if row.semester == 2
        )
        for commitment in active_commitments_by_student[student_id]:
            if commitment.commitment_kind != "co_op":
                continue
            commitment_semesters = {
                semester_by_timeslot.get(timeslot_id)
                for timeslot_id, _segment in commitment.occupancy
            }
            if 1 in commitment_semesters:
                semester_1 += round(commitment.credit_value * 2)
            if 2 in commitment_semesters:
                semester_2 += round(commitment.credit_value * 2)
        # Co-op carries two credits as one linked academic experience. Study
        # and Focus still reserve time, but they are not academic course load.
        for source_key, index, variable in commitment_variables_by_student[student_id]:
            candidate_semester, _occupancy, _pair = commitment_candidates[source_key][index]
            metadata = commitment_metadata[source_key]
            if metadata[1] != "co_op":
                continue
            if candidate_semester == 1:
                semester_1 += 4 * variable
            else:
                semester_2 += 4 * variable
        penalty = model.NewIntVar(
            0,
            2 * (len(data.requests) + len(fixed_rows) + 2),
            f"semester_balance_{student_id}",
        )
        model.AddAbsEquality(penalty, semester_1 - semester_2)
        semester_balance_terms.append(penalty)
    semester_level = IMPORTANCE_LEVELS[data.student_semester_balance_importance]
    if semester_level:
        soft_objectives[semester_level].append(sum(semester_balance_terms or [0]))

    difficulty_balance_terms = []
    difficulty_level = IMPORTANCE_LEVELS[data.difficulty_balance_importance]
    if difficulty_level:
        # Difficulty reflects total annual academic load, not fulfillment or
        # the absolute amount of challenging coursework. Avoid constructing
        # these auxiliary variables when counselors explicitly disable it.
        difficulty_by_course = {
            item.course_id: item.effective_difficulty for item in data.course_difficulties
        }
        semester_by_timeslot = {slot.id: slot.semester for slot in data.timeslots}
        for student_id in student_ids:
            if student_id in focus_student_ids:
                continue
            requested_course_ids = request_course_ids_by_student[student_id]
            semester_1_difficulty = sum(
                round(difficulty_by_course.get(course_id, 0) * credit_by_student_course.get((student_id, course_id), 1.0)) * variable
                for course_id in requested_course_ids
                for variable in by_student_course_semester[student_id, course_id, 1]
            ) + sum(
                round(difficulty_by_course.get(row.course_id, 0) * row.credit_value)
                for row in fixed_rows_by_student[student_id] if row.semester == 1
            )
            semester_2_difficulty = sum(
                round(difficulty_by_course.get(course_id, 0) * credit_by_student_course.get((student_id, course_id), 1.0)) * variable
                for course_id in requested_course_ids
                for variable in by_student_course_semester[student_id, course_id, 2]
            ) + sum(
                round(difficulty_by_course.get(row.course_id, 0) * row.credit_value)
                for row in fixed_rows_by_student[student_id] if row.semester == 2
            )
            for commitment in active_commitments_by_student[student_id]:
                semesters = {
                    semester_by_timeslot.get(timeslot_id)
                    for timeslot_id, _segment in commitment.occupancy
                }
                if commitment.commitment_kind == "study":
                    contribution = 1
                elif commitment.commitment_kind == "co_op":
                    contribution = round(
                        difficulty_by_course.get(commitment.course_id, 0)
                        * commitment.credit_value
                    )
                else:
                    continue
                if 1 in semesters:
                    semester_1_difficulty += contribution
                if 2 in semesters:
                    semester_2_difficulty += contribution
            for source_key, index, variable in commitment_variables_by_student[student_id]:
                candidate_semester, _occupancy, _pair = commitment_candidates[source_key][index]
                metadata = commitment_metadata[source_key]
                if metadata[1] == "co_op":
                    contribution = round(
                        difficulty_by_course.get(metadata[4], 0)
                        * next(
                            request.credit_value
                            for request in (requests_by_id[metadata[2]],)
                        )
                    )
                elif metadata[1] == "study":
                    # Study is not an academic course, but a requested study
                    # block should make its term slightly lighter in this
                    # counselor-facing balance signal rather than neutral.
                    contribution = 1
                else:
                    continue
                if candidate_semester == 1:
                    semester_1_difficulty += contribution * variable
                else:
                    semester_2_difficulty += contribution * variable
            penalty = model.NewIntVar(
                0,
                (len(requested_course_ids) + len(fixed_rows)) * 100,
                f"difficulty_balance_{student_id}",
            )
            model.AddAbsEquality(penalty, semester_1_difficulty - semester_2_difficulty)
            difficulty_balance_terms.append(penalty)
        soft_objectives[difficulty_level].append(sum(difficulty_balance_terms or [0]))

    category_diversity_terms = []
    category_diversity_level = IMPORTANCE_LEVELS[data.course_category_diversity_importance]
    if category_diversity_level:
        # A category has no artificial ordinal position. Equal categories are
        # always fully similar; only explicit catalog relationship rows add a
        # cross-category affinity. Missing/unknown categories are neutral.
        category_by_course = {item.course_id: item.category for item in data.course_difficulties}
        category_similarity = {
            tuple(sorted((item.category_a, item.category_b))): item.similarity_score
            for item in data.course_category_relationships
        }

        def category_pair_similarity(left_course_id, right_course_id):
            left_category = category_by_course.get(left_course_id, "")
            right_category = category_by_course.get(right_course_id, "")
            if not left_category or not right_category:
                return 0
            if left_category == right_category:
                return 100
            return category_similarity.get(tuple(sorted((left_category, right_category))), 0)

        course_segment_presence = {}
        fixed_courses_by_student_segment = defaultdict(set)
        for row in fixed_rows:
            for segment in _occupied_half_segments(row.half_semester_segment):
                fixed_courses_by_student_segment[
                    row.student_id, row.semester, segment
                ].add(row.course_id)
        for student_id in student_ids:
            if student_id in focus_student_ids:
                continue
            course_ids = request_course_ids_by_student[student_id] | {
                row.course_id for row in fixed_rows_by_student[student_id]
            }
            for course_id in course_ids:
                for semester in (1, 2):
                    for segment in HALF_SEMESTER_SEGMENTS:
                        key = student_id, course_id, semester, segment
                        if course_id in fixed_courses_by_student_segment[
                            student_id, semester, segment
                        ]:
                            course_segment_presence[key] = 1
                            continue
                        variables_for_course = (
                            candidate_variables_by_student_course_segment[
                                student_id,
                                course_id,
                                semester,
                                segment,
                            ]
                        )
                        if not variables_for_course:
                            course_segment_presence[key] = 0
                            continue
                        present = model.NewBoolVar(
                            f"category_present_{student_id}_{course_id}_{semester}_{segment}"
                        )
                        model.Add(sum(variables_for_course) == present)
                        course_segment_presence[key] = present
        for student_id in student_ids:
            if student_id in focus_student_ids:
                continue
            course_ids = sorted({
                request.course_id for request in requests_by_student[student_id]
            } | {
                row.course_id for row in fixed_rows_by_student[student_id]
            })
            for index, left_course_id in enumerate(course_ids):
                for right_course_id in course_ids[index + 1:]:
                    similarity = category_pair_similarity(left_course_id, right_course_id)
                    if not similarity:
                        continue
                    for semester in (1, 2):
                        shared_halves = []
                        for segment in HALF_SEMESTER_SEGMENTS:
                            left = course_segment_presence[
                                student_id, left_course_id, semester, segment
                            ]
                            right = course_segment_presence[
                                student_id, right_course_id, semester, segment
                            ]
                            if (isinstance(left, int) and left == 0) or (
                                isinstance(right, int) and right == 0
                            ):
                                continue
                            if isinstance(left, int) and left == 1:
                                shared_halves.append(right)
                            elif isinstance(right, int) and right == 1:
                                shared_halves.append(left)
                            else:
                                shared = model.NewBoolVar(
                                    f"category_concentration_{student_id}_{left_course_id}_{right_course_id}_{semester}_{segment}"
                                )
                                model.AddBoolAnd((left, right)).OnlyEnforceIf(shared)
                                model.AddBoolOr((left.Not(), right.Not(), shared))
                                shared_halves.append(shared)
                        if shared_halves:
                            # A full/full overlap remains the legacy penalty of
                            # ``similarity``. A full/half overlap costs half;
                            # sequential first/second-half courses cost zero.
                            penalty = model.NewIntVar(
                                0,
                                similarity,
                                f"category_penalty_{student_id}_{left_course_id}_{right_course_id}_{semester}",
                            )
                            model.AddDivisionEquality(
                                penalty, similarity * sum(shared_halves), 2
                            )
                            category_diversity_terms.append(penalty)
        soft_objectives[category_diversity_level].append(sum(category_diversity_terms or [0]))

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

    (
        hard_feasibility_seed_model,
        hard_feasibility_seed_solver,
        hard_feasibility_source_variable_indexes,
        _hard_feasibility_outcome,
    ) = _solve_complete_hard_feasibility_seed(
        hard_feasibility_model,
        complete_required_decision_groups,
        (
            max(
                data.time_limit_seconds,
                STUDENT_ASSIGNMENT_HARD_FEASIBILITY_TIME_LIMIT_SECONDS,
            )
            if use_hard_feasibility_bootstrap
            else data.time_limit_seconds
        ),
        worker_count=STUDENT_ASSIGNMENT_HARD_FEASIBILITY_WORKER_COUNT,
    )
    validated_seed_solver = _validate_complete_hard_feasibility_seed(
        model,
        hard_feasibility_seed_model,
        hard_feasibility_seed_solver,
        hard_feasibility_source_variable_indexes,
        (
            max(
                data.time_limit_seconds,
                STUDENT_ASSIGNMENT_HARD_FEASIBILITY_VALIDATION_TIME_LIMIT_SECONDS,
            )
            if use_hard_feasibility_bootstrap
            else data.time_limit_seconds
        ),
        worker_count=STUDENT_ASSIGNMENT_HARD_FEASIBILITY_VALIDATION_WORKER_COUNT,
    )
    # Retain the existing independent-request hint as the documented fallback
    # when CP-SAT cannot produce a complete hard-feasibility seed in its
    # bounded stage.  A validated CP-SAT seed always takes precedence.
    initial_assignment_hints = (
        {} if validated_seed_solver is not None else _build_initial_assignment_hints(
            data=data,
            request_candidates=request_candidates,
            fixed_by_section=fixed_by_section,
            fixed_slots=fixed_slots,
            group_locks=group_locks,
        )
    )
    solver, outcome = _solve_lexicographically(
        model,
        objectives,
        data.time_limit_seconds,
        initial_assignment_hints=initial_assignment_hints,
        validated_seed_solver=validated_seed_solver,
        worker_count=STUDENT_ASSIGNMENT_OPTIMIZATION_WORKER_COUNT,
    )
    optimization_facts = _optimization_facts(
        hard_feasibility_outcome=_hard_feasibility_outcome,
        required_group_count=len(complete_required_decision_groups),
        hard_seed_solver=hard_feasibility_seed_solver,
        validated_seed_solver=validated_seed_solver,
        final_solver=solver,
        final_outcome=outcome,
        objectives=objectives,
    )
    if solver is None:
        # CP-SAT ``UNKNOWN`` means the bounded solve ended without a proof or a
        # usable candidate. It is not evidence that the scheduling facts are
        # mathematically infeasible, so preserve that distinction for review
        # and for the target-scale benchmark.
        result_status = "infeasible" if outcome == cp_model.INFEASIBLE else "failed"
        failed_unmet = tuple(
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
        )
        result = StudentAssignmentResultDTO(
            status=result_status,
            solver_outcome=_outcome_name(outcome),
            assignments=(),
            unmet_requests=failed_unmet,
            diagnostics=({"code": NO_COMPLETE_STUDENT_ASSIGNMENT},),
            objective_components={},
            optimization_facts=optimization_facts,
            sequence_outcomes=(),
            candidate_ledger=(
                _build_candidate_ledger(
                    data=data,
                    entries=candidate_ledger_entries,
                    sections=sections,
                    fixed_rows=fixed_rows,
                    fixed_by_section=fixed_by_section,
                    selected_by_section=defaultdict(list),
                    assignments=(),
                    commitment_assignments=(),
                    unmet_requests=failed_unmet,
                    review_items=(),
                    has_solution=False,
                )
                if include_candidate_ledger else ()
            ),
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
                    section_id=(
                        None if request.delivery_kind == "online" and section.section_id < 0
                        else section.section_id
                    ),
                    course_offering_id=request.course_offering_id,
                    course_id=request.course_id,
                    semester=section.semester,
                    timeslot_id=section.timeslot_id,
                    assignment_basis=request.assignment_basis,
                    backup_resolution_snapshot=request.backup_resolution_snapshot,
                    previous_enrollment_id=previous.enrollment_id if previous else None,
                    previous_section_id=(
                        previous.section_id
                        if previous and previous.section_id > 0
                        else None
                    ),
                    previous_online_supervision_session_id=(
                        -previous.section_id
                        if previous and previous.section_id < 0
                        else None
                    ),
                    online_supervision_session_id=(
                        -section.section_id
                        if request.delivery_kind == "online" and section.section_id < 0
                        else None
                    ),
                    half_semester_segment=(
                        request.half_semester_segment
                        if request.delivery_kind == "online"
                        else section.half_semester_segment
                    ),
                )
                assignments.append(assignment)
                selected_by_section[section.section_id].append(assignment)
                assigned_request_ids.add(request.request_id)
                break

    commitment_assignments = []
    review_items = []
    assigned_commitment_sources = set()
    for (source_key, index), variable in sorted(commitment_variables.items()):
        if not solver.Value(variable):
            continue
        placement_value, occupancy, pair = commitment_candidates[source_key][index]
        student_id, kind, course_request_id, course_offering_id, course_id = commitment_metadata[source_key]
        commitment_assignments.append(StudentScheduleCommitmentAssignmentDTO(
            request_id=source_key[1],
            student_id=student_id,
            commitment_kind=kind,
            course_request_id=course_request_id,
            course_offering_id=course_offering_id,
            occupancy=occupancy,
        ))
        assigned_commitment_sources.add(source_key)
        if course_request_id is not None:
            assigned_request_ids.add(course_request_id)

    # A half-semester course can be a legitimate unpaired request. The engine
    # schedules it if possible, then tells the counselor that its other half is
    # intentionally unallocated rather than silently inventing Study time.
    assignments_by_student_course = {
        (item.student_id, item.course_id): item for item in assignments
    }
    unpaired_half_occupancies = set()
    for request in data.requests:
        if request.duration != "half_semester":
            continue
        assignment = assignments_by_student_course.get((request.student_id, request.course_id))
        if assignment is None:
            continue
        partner = assignments_by_student_course.get(
            (request.student_id, request.paired_half_course_id)
        )
        paired = (
            partner is not None
            and partner.semester == assignment.semester
            and partner.timeslot_id == assignment.timeslot_id
            and partner.half_semester_segment != assignment.half_semester_segment
        )
        if not paired:
            if request.delivery_kind != "online":
                # The normal half-course review already identifies this precise
                # missing half. Suppress the broader empty-time item for the
                # same slot so counselors receive one truthful explanation.
                unpaired_half_occupancies.add((
                    request.student_id,
                    assignment.timeslot_id,
                    "second_half"
                    if assignment.half_semester_segment == "first_half"
                    else "first_half",
                ))
            review_items.append(StudentAssignmentReviewItemDTO(
                code=STUDENT_ASSIGNMENT_HALF_SEMESTER_UNALLOCATED_OPPOSITE_HALF,
                student_id=request.student_id,
                request_id=request.request_id,
                course_id=request.course_id,
                detail={
                    "semester": assignment.semester,
                    "timeslot_id": assignment.timeslot_id,
                    "allocated_half": assignment.half_semester_segment,
                },
            ))
        if request.delivery_kind == "online" and not paired:
            review_items.append(StudentAssignmentReviewItemDTO(
                code=STUDENT_ASSIGNMENT_ONLINE_HALF_SEMESTER_UNUSED_SUPERVISION_HALF,
                student_id=request.student_id,
                request_id=request.request_id,
                course_id=request.course_id,
                detail={
                    "supervision_session_id": assignment.online_supervision_session_id,
                    "unused_half": (
                        "second_half"
                        if assignment.half_semester_segment == "first_half"
                        else "first_half"
                    ),
                },
            ))

    for source_key, choices in commitment_candidates.items():
        if source_key in assigned_commitment_sources or source_key in fixed_commitment_sources:
            continue
        student_id, kind, course_request_id, _offering_id, course_id = commitment_metadata[source_key]
        if kind == "study":
            code = STUDENT_ASSIGNMENT_SPECIAL_COMMITMENT_LOCK_BLOCKS_REQUEST if any(
                _special_lock_candidates(data.special_commitment_locks, lock_type="study_time", request_id=source_key[1])
            ) else STUDENT_ASSIGNMENT_UNALLOCATED_SCHOOL_TIME
        elif kind == "focus":
            code = STUDENT_ASSIGNMENT_NO_VALID_FOCUS_SEMESTER
        else:
            code = STUDENT_ASSIGNMENT_NO_VALID_CO_OP_BLOCK_PAIR
        review_items.append(StudentAssignmentReviewItemDTO(
            code=code,
            student_id=student_id,
            request_id=source_key[1],
            course_id=course_id,
            detail={"commitment_kind": kind, "candidate_count": len(choices)},
        ))

    _append_unallocated_school_time_review_items(
        data=data,
        sections=sections,
        requests_by_id=requests_by_id,
        fixed_slots=fixed_slots,
        fixed_rows=fixed_rows,
        assignments=assignments,
        commitment_assignments=commitment_assignments,
        unpaired_half_occupancies=unpaired_half_occupancies,
        review_items=review_items,
    )

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
    unsatisfied_commitments = [
        source_key
        for source_key in commitment_candidates
        if source_key not in assigned_commitment_sources and source_key not in fixed_commitment_sources
    ]
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
        status=(
            "complete"
            if not required_unmet and not hard_sequence_impossible and not unsatisfied_commitments
            else "partial"
        ),
        solver_outcome=_outcome_name(outcome),
        assignments=tuple(assignments),
        unmet_requests=tuple(unmet),
        diagnostics=tuple(diagnostics),
        objective_components={
            "mandatory_fulfilled": float(sum(
                1 for request in data.requests
                if request.request_id in assigned_request_ids and request.is_mandatory
            )),
            "priority_primary_fulfilled": float(sum(
                1 for request_id in assigned_request_ids if request_id in priority_request_ids
            )),
            "primary_fulfilled": float(sum(
                1 for request_id in assigned_request_ids if requests_by_id[request_id].is_primary
            )),
            "approved_backup_fulfilled": float(sum(
                1 for request_id in assigned_request_ids if not requests_by_id[request_id].is_primary
            )),
            "section_utilization_balance_penalty": float(sum(solver.Value(item) for item in section_balance_terms)),
            "student_semester_balance_penalty": float(sum(solver.Value(item) for item in semester_balance_terms)),
            "difficulty_balance_penalty": float(sum(solver.Value(item) for item in difficulty_balance_terms)),
            "course_category_diversity_penalty": float(sum(solver.Value(item) for item in category_diversity_terms)),
            "schedule_preservation_move_penalty": float(sum(solver.Value(item) for item in preservation_terms)),
            "soft_sequence_preferences_satisfied": float(sum(item["satisfied"] for item in sequence_outcomes)),
        },
        optimization_facts=optimization_facts,
        sequence_outcomes=tuple(sequence_outcomes),
        seat_contention=tuple(seat_contention),
        section_balance_facts=tuple(section_balance_facts),
        commitment_assignments=tuple(commitment_assignments),
        review_items=tuple(review_items),
        candidate_ledger=(
            _build_candidate_ledger(
                data=data,
                entries=candidate_ledger_entries,
                sections=sections,
                fixed_rows=fixed_rows,
                fixed_by_section=fixed_by_section,
                selected_by_section=selected_by_section,
                assignments=assignments,
                commitment_assignments=commitment_assignments,
                unmet_requests=unmet,
                review_items=review_items,
                has_solution=True,
            )
            if include_candidate_ledger else ()
        ),
    )
    return replace(result, lock_costs=_build_lock_costs(data, result)) if include_lock_costs else result
