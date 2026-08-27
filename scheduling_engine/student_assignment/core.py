"""Pure student-to-section assignment over fixed accepted schedule context.

This module intentionally has no Django dependency.  It consumes a detached
snapshot, recommends enrollment creation or replacement facts, and never
changes section timing, rooms, teachers, or persisted enrollment records.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import replace
from hashlib import sha256
from time import monotonic

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
    STUDENT_ASSIGNMENT_OPTIMIZATION_TIME_LIMIT_SECONDS,
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
    validate_source_decision_candidate as _validate_source_decision_candidate,
    validate_source_decision_candidate_with_status as _validate_source_decision_candidate_with_status,
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
from .quality import (
    compact_student_assignment_quality as _compact_student_assignment_quality,
    compare_student_assignment_quality as _compare_student_assignment_quality,
    evaluate_student_assignment_quality as _evaluate_student_assignment_quality,
)
from .substantive_probe import (
    SubstantiveSoftTierProbeContext,
    _model_family_variable_counts,
    probe_substantive_soft_tier,
)
from .runtime import (
    MonotonicDeadline,
    ProcessMemoryMonitor,
    ProcessResourceMonitor,
    semantic_student_assignment_input_fingerprint,
)
from .objective_semantics import (
    CANONICAL_IMPORTANCE_MAX,
    IMPORTANCE_LABEL_TO_SCORE,
    NORMALIZED_OBJECTIVE_SCALE,
    OBJECTIVE_SEMANTICS_V1,
    OBJECTIVE_SEMANTICS_V2,
    resolve_importance_scores,
)
from .search_guidance import rank_students_by_quality_pressure
from .utilization_guidance import select_utilization_cluster_targets
from .grade_guidance import build_grade_opportunity_facts
from .operator_session import select_operator_session_targets


def _soft_tier_importance_level(data):
    """Return the metadata level used by diagnostic soft-tier probes.

    V1 has one soft tier per historical label level.  V2 deliberately emits
    one normalized aggregate tier, whose metadata level is the canonical
    maximum score.  This keeps diagnostic wrappers pointed at the same
    objective without changing production optimization semantics.
    """

    return (
        CANONICAL_IMPORTANCE_MAX
        if data.objective_semantics_version == OBJECTIVE_SEMANTICS_V2
        else IMPORTANCE_LEVELS["important"]
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


def _notify_phase(phase_callback, phase, event="completed", **facts):
    """Send optional diagnostic breadcrumbs without affecting the engine."""

    if phase_callback is None:
        return
    try:
        phase_callback(str(phase), event=str(event), **facts)
    except Exception:
        # The callback belongs to offline calibration infrastructure. A
        # telemetry failure must never alter scheduling behavior.
        return


def _candidate_semester_from_occupancy(occupancy, semester_by_timeslot):
    """Return the semester represented by one commitment occupancy.

    Commitment candidate tuples carry an opaque placement value for historical
    reasons.  The authoritative semester is the semester of the occupied
    target-year timeslots, not that overloaded tuple value.  All accepted
    Study and Co-op candidates occupy one semester; fail closed if that
    invariant is ever broken while building the objective model.
    """

    semesters = {
        semester_by_timeslot[timeslot_id]
        for timeslot_id, _segment in occupancy
        if timeslot_id in semester_by_timeslot
    }
    if len(semesters) != 1:
        raise ValueError(
            "A commitment candidate must occupy exactly one semester."
        )
    return next(iter(semesters))


def _extract_solver_candidate(
    *, solver, data, request_candidates, commitment_variables,
    commitment_candidates, commitment_metadata, previous_enrollment_by_request,
):
    """Extract source decisions from any valid full-model solver solution.

    Stage 1 validation and Stage 2 optimization solve the same full variable
    namespace. Keeping extraction in one helper lets the quality evaluator
    inspect both candidates without creating a second solve or changing which
    assignment the production solver returns.
    """

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
    assigned_commitment_sources = set()
    for (source_key, index), variable in sorted(commitment_variables.items()):
        if not solver.Value(variable):
            continue
        _placement_value, occupancy, _pair = commitment_candidates[source_key][index]
        student_id, kind, course_request_id, course_offering_id, _course_id = commitment_metadata[source_key]
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

    return (
        tuple(assignments),
        tuple(commitment_assignments),
        assigned_request_ids,
        selected_by_section,
        assigned_commitment_sources,
    )


def _optimization_facts(
    *,
    hard_feasibility_outcome,
    required_group_count,
    hard_seed_solver,
    validated_seed_solver,
    stage_2_seed_solver=None,
    final_solver,
    final_outcome,
    objectives,
    optimization_time_limit_seconds,
    stage_1_quality=None,
    stage_2_quality=None,
    quality_comparison=None,
    optimization_passes=(),
    stage_1_timings=None,
    input_semantic_fingerprint=None,
    full_model_variable_count=None,
    full_model_constraint_count=None,
    optimization_worker_count=None,
    model_family_variable_counts=None,
    objective_metadata_summary=(),
):
    """Expose stage handoff and quality facts without changing solver logic."""

    seed_values = _objective_values(validated_seed_solver, objectives)
    final_values = _objective_values(final_solver, objectives)
    stage_2_seed_solver = stage_2_seed_solver or validated_seed_solver
    improved = bool(seed_values and final_values and final_values < seed_values)
    facts = {
        "input_semantic_fingerprint": input_semantic_fingerprint,
        "full_model_variable_count": full_model_variable_count,
        "full_model_constraint_count": full_model_constraint_count,
        "model_family_variable_counts": dict(model_family_variable_counts or {}),
        "objective_metadata": list(objective_metadata_summary),
        "stage_1": {
            "solver_outcome": _outcome_name(hard_feasibility_outcome),
            "required_decision_group_count": required_group_count,
            "complete_seed_produced": hard_seed_solver is not None,
            "seed_validated_against_full_model": validated_seed_solver is not None,
            "objective_values": list(seed_values),
            "timings": dict(stage_1_timings or {}),
        },
        "stage_2": {
            "solver_outcome": _outcome_name(final_outcome),
            "worker_count": (
                optimization_worker_count
                if optimization_worker_count is not None
                else STUDENT_ASSIGNMENT_OPTIMIZATION_WORKER_COUNT
            ),
            "time_limit_seconds": optimization_time_limit_seconds,
            "validated_seed_received": validated_seed_solver is not None,
            "alternate_validated_seed_received": (
                stage_2_seed_solver is not None
                and stage_2_seed_solver is not validated_seed_solver
            ),
            "objective_values": list(final_values),
            "improved_over_stage_1": improved,
            "timings": {},
        },
        "optimization_passes": list(optimization_passes),
    }
    if stage_1_quality is not None:
        facts["quality"] = {
            "stage_1": _compact_student_assignment_quality(stage_1_quality),
            "stage_2": (
                _compact_student_assignment_quality(stage_2_quality)
                if stage_2_quality is not None else None
            ),
            "stage_1_vs_stage_2": quality_comparison,
        }
    return facts


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


def run_substantive_soft_tier_probe(
    data: StudentAssignmentInputDTO,
    *,
    threshold,
    time_limit_seconds=1800.0,
    worker_count=8,
    target_importance_level=None,
    neighborhood_radius=None,
    component_bounds=None,
    minimize_component=None,
    alternate_source_decisions=(),
    alternate_source_variable_values=None,
    strict_improvement=False,
    max_changed_students=None,
    hard_feasibility_time_limit_seconds=None,
    hard_feasibility_validation_time_limit_seconds=None,
    hard_feasibility_worker_count=None,
    hard_feasibility_validation_worker_count=None,
    collect_resource_telemetry=False,
):
    """Run a diagnostic-only satisfiability probe against the full model.

    This deliberately does not return a production assignment result.  It
    reuses the ordinary model builder through a private diagnostic branch and
    is intended for focused tests and offline target-scale experiments only.
    """

    if target_importance_level is None:
        target_importance_level = _soft_tier_importance_level(data)
    return _solve_student_assignment(
        data,
        include_lock_costs=False,
        include_candidate_ledger=False,
        use_hard_feasibility_bootstrap=True,
        substantive_soft_tier_probe={
            "threshold": threshold,
            "time_limit_seconds": time_limit_seconds,
            "worker_count": worker_count,
            "target_importance_level": target_importance_level,
            "neighborhood_radius": neighborhood_radius,
            "component_bounds": component_bounds,
            "minimize_component": minimize_component,
            "strict_improvement": strict_improvement,
            "max_changed_students": max_changed_students,
        },
        alternate_source_decisions=alternate_source_decisions,
        alternate_source_variable_values=alternate_source_variable_values,
        hard_feasibility_time_limit_seconds=hard_feasibility_time_limit_seconds,
        hard_feasibility_validation_time_limit_seconds=(
            hard_feasibility_validation_time_limit_seconds
        ),
        hard_feasibility_worker_count=hard_feasibility_worker_count,
        hard_feasibility_validation_worker_count=hard_feasibility_validation_worker_count,
        collect_resource_telemetry=collect_resource_telemetry,
    )


def run_student_assignment_stage2_diagnostic(
    data: StudentAssignmentInputDTO,
    *,
    alternate_source_decisions=(),
    alternate_source_variable_values=None,
    total_time_limit_seconds=None,
    collect_incumbent_timeline=True,
    timeline_max_events=128,
    capture_final_source_decisions=False,
    hard_feasibility_time_limit_seconds=None,
    hard_feasibility_validation_time_limit_seconds=None,
    hard_feasibility_worker_count=None,
    hard_feasibility_validation_worker_count=None,
    optimization_worker_count=None,
    retain_incumbent_on_non_improvement=True,
    collect_resource_telemetry=False,
):
    """Run unchanged Stage 2 with optional diagnostic alternate incumbent."""

    return _solve_student_assignment(
        data,
        include_lock_costs=False,
        include_candidate_ledger=False,
        use_hard_feasibility_bootstrap=True,
        collect_stage2_trace=True,
        alternate_source_decisions=alternate_source_decisions,
        alternate_source_variable_values=alternate_source_variable_values,
        stage_2_total_time_limit_seconds=total_time_limit_seconds,
        retain_incumbent_on_non_improvement=retain_incumbent_on_non_improvement,
        collect_incumbent_timeline=collect_incumbent_timeline,
        timeline_max_events=timeline_max_events,
        capture_final_source_decisions=capture_final_source_decisions,
        hard_feasibility_time_limit_seconds=hard_feasibility_time_limit_seconds,
        hard_feasibility_validation_time_limit_seconds=(
            hard_feasibility_validation_time_limit_seconds
        ),
        hard_feasibility_worker_count=hard_feasibility_worker_count,
        hard_feasibility_validation_worker_count=hard_feasibility_validation_worker_count,
        optimization_worker_count=optimization_worker_count,
        collect_resource_telemetry=collect_resource_telemetry,
    )


def run_student_assignment_local_bootstrap_diagnostic(
    data: StudentAssignmentInputDTO,
    *,
    neighborhood_radius=2,
    time_limit_seconds=240.0,
    total_time_limit_seconds=1800.0,
    worker_count=8,
    hard_feasibility_time_limit_seconds=None,
    hard_feasibility_validation_time_limit_seconds=None,
    hard_feasibility_worker_count=None,
    hard_feasibility_validation_worker_count=None,
    alternate_source_decisions=(),
    alternate_source_variable_values=None,
    collect_incumbent_timeline=True,
    timeline_max_events=128,
    max_changed_students=None,
    capture_final_source_decisions=False,
    collect_resource_telemetry=False,
):
    """Run a bounded local-quality bootstrap before diagnostic Stage 2.

    This is intentionally diagnostic-only.  The bootstrap consumes the
    supplied Stage 2 budget and hands CP-SAT's validated candidate to the
    unchanged lexicographic optimizer; it never manufactures or fixes a
    schedule.  Production callers continue to use ``solve_student_assignment``
    without this path until target-scale budget and repeatability gates pass.
    """

    return _solve_student_assignment(
        data,
        include_lock_costs=False,
        include_candidate_ledger=False,
        use_hard_feasibility_bootstrap=True,
        collect_stage2_trace=True,
        stage_2_local_bootstrap={
            "threshold": None,
            "time_limit_seconds": time_limit_seconds,
            "worker_count": worker_count,
            "target_importance_level": _soft_tier_importance_level(data),
            "neighborhood_radius": neighborhood_radius,
            "max_changed_students": max_changed_students,
            "component_bounds": None,
            "minimize_component": None,
        },
        stage_2_total_time_limit_seconds=total_time_limit_seconds,
        alternate_source_decisions=alternate_source_decisions,
        alternate_source_variable_values=alternate_source_variable_values,
        retain_incumbent_on_non_improvement=True,
        hard_feasibility_time_limit_seconds=hard_feasibility_time_limit_seconds,
        hard_feasibility_validation_time_limit_seconds=(
            hard_feasibility_validation_time_limit_seconds
        ),
        hard_feasibility_worker_count=hard_feasibility_worker_count,
        hard_feasibility_validation_worker_count=hard_feasibility_validation_worker_count,
        collect_incumbent_timeline=collect_incumbent_timeline,
        timeline_max_events=timeline_max_events,
        capture_final_source_decisions=capture_final_source_decisions,
        collect_resource_telemetry=collect_resource_telemetry,
    )


def run_student_assignment_adaptive_local_bootstrap_diagnostic(
    data: StudentAssignmentInputDTO,
    *,
    neighborhood_radii=(2, 4),
    max_iterations=3,
    per_probe_time_limit_seconds=90.0,
    total_time_limit_seconds=1800.0,
    worker_count=8,
    hard_feasibility_time_limit_seconds=None,
    hard_feasibility_validation_time_limit_seconds=None,
    hard_feasibility_worker_count=None,
    hard_feasibility_validation_worker_count=None,
    alternate_source_decisions=(),
    alternate_source_variable_values=None,
    collect_incumbent_timeline=True,
    timeline_max_events=128,
    max_changed_students=None,
    capture_final_source_decisions=False,
    collect_resource_telemetry=False,
):
    """Run diagnostic strict-improvement neighborhoods with shared budgeting.

    Each probe is a CP-SAT satisfiability search around the strongest validated
    incumbent found so far.  A validated improvement restarts at the smallest
    radius; a failed radius may expand once.  This path is deliberately not
    called by ``solve_student_assignment`` until repeatability and production
    promotion gates establish that its budget trade-off is useful.
    """

    return _solve_student_assignment(
        data,
        include_lock_costs=False,
        include_candidate_ledger=False,
        use_hard_feasibility_bootstrap=True,
        collect_stage2_trace=True,
        stage_2_local_bootstrap={
            "adaptive": True,
            "neighborhood_radii": tuple(neighborhood_radii),
            "max_iterations": max_iterations,
            "per_probe_time_limit_seconds": per_probe_time_limit_seconds,
            "worker_count": worker_count,
            "target_importance_level": _soft_tier_importance_level(data),
            "max_changed_students": max_changed_students,
        },
        stage_2_total_time_limit_seconds=total_time_limit_seconds,
        retain_incumbent_on_non_improvement=True,
        alternate_source_decisions=alternate_source_decisions,
        alternate_source_variable_values=alternate_source_variable_values,
        hard_feasibility_time_limit_seconds=hard_feasibility_time_limit_seconds,
        hard_feasibility_validation_time_limit_seconds=(
            hard_feasibility_validation_time_limit_seconds
        ),
        hard_feasibility_worker_count=hard_feasibility_worker_count,
        hard_feasibility_validation_worker_count=hard_feasibility_validation_worker_count,
        collect_incumbent_timeline=collect_incumbent_timeline,
        timeline_max_events=timeline_max_events,
        capture_final_source_decisions=capture_final_source_decisions,
        collect_resource_telemetry=collect_resource_telemetry,
    )


def run_student_assignment_mature_local_search_diagnostic(
    data: StudentAssignmentInputDTO,
    *,
    mature_source_decisions=(),
    mature_source_variable_values=None,
    max_iterations=64,
    per_probe_time_limit_seconds=600.0,
    total_time_limit_seconds=3600.0,
    worker_count=8,
    hard_feasibility_validation_time_limit_seconds=None,
    hard_feasibility_validation_worker_count=None,
    capture_final_source_decisions=True,
    collect_resource_telemetry=True,
):
    """Continue a validated mature incumbent through R2 without Stage 2.

    This diagnostic-only path treats the supplied mature checkpoint as the
    starting incumbent, validates it once against the unchanged full model,
    performs repeated radius-two local probes in one engine operation, and
    returns the strongest complete validated incumbent. The ordinary
    lexicographic optimizer is intentionally skipped here; callers that need
    production Stage 2 must continue using the existing diagnostic/production
    entry points.
    """

    if not mature_source_decisions and mature_source_variable_values is None:
        raise ValueError("mature_source_decisions is required")
    return _solve_student_assignment(
        data,
        include_lock_costs=False,
        include_candidate_ledger=False,
        use_hard_feasibility_bootstrap=True,
        collect_stage2_trace=False,
        stage_2_local_bootstrap={
            "adaptive": True,
            "neighborhood_radii": (2,),
            "max_iterations": max_iterations,
            "per_probe_time_limit_seconds": per_probe_time_limit_seconds,
            "worker_count": worker_count,
            "target_importance_level": _soft_tier_importance_level(data),
        },
        stage_2_total_time_limit_seconds=total_time_limit_seconds,
        retain_incumbent_on_non_improvement=True,
        alternate_source_decisions=mature_source_decisions,
        alternate_source_variable_values=mature_source_variable_values,
        mature_checkpoint_only=True,
        local_only=True,
        hard_feasibility_validation_time_limit_seconds=(
            hard_feasibility_validation_time_limit_seconds
        ),
        hard_feasibility_validation_worker_count=(
            hard_feasibility_validation_worker_count
        ),
        collect_incumbent_timeline=False,
        capture_final_source_decisions=capture_final_source_decisions,
        collect_resource_telemetry=collect_resource_telemetry,
    )


def run_student_assignment_source_decision_validation_diagnostic(
    data: StudentAssignmentInputDTO,
    *,
    source_decisions=(),
    source_variable_values=None,
    time_limit_seconds=None,
    worker_count=1,
    capture_final_source_decisions=True,
    collect_resource_telemetry=False,
):
    """Validate detached semantic decisions against the current full model.

    This is a diagnostic boundary for transparent benchmark branches. It
    skips Stage 1 and all optimization, then uses the same full-model source
    decision validator used by mature diagnostic sessions. The returned
    result is extracted from that CP-SAT-validated incumbent; no heuristic
    repair or replacement schedule is created.
    """

    if not source_decisions and source_variable_values is None:
        raise ValueError("source_decisions is required")
    validation_limit = max(
        0.001,
        float(time_limit_seconds)
        if time_limit_seconds is not None
        else float(data.time_limit_seconds),
    )
    return _solve_student_assignment(
        data,
        include_lock_costs=False,
        include_candidate_ledger=False,
        use_hard_feasibility_bootstrap=True,
        alternate_source_decisions=source_decisions,
        alternate_source_variable_values=source_variable_values,
        mature_checkpoint_only=True,
        local_only=True,
        hard_feasibility_validation_time_limit_seconds=validation_limit,
        hard_feasibility_validation_worker_count=worker_count,
        capture_final_source_decisions=capture_final_source_decisions,
        collect_resource_telemetry=collect_resource_telemetry,
    )


def run_student_assignment_operator_session_diagnostic(
    data: StudentAssignmentInputDTO,
    *,
    operator_family="r2",
    initial_source_decisions=(),
    initial_source_variable_values=None,
    total_time_limit_seconds=600.0,
    max_attempts=10,
    per_attempt_time_limit_seconds=60.0,
    worker_count=8,
    target_policy="dynamic",
    selected_student_ids=(),
    selected_grade=None,
    utilization_cluster_policy="interaction_aware",
    minimum_next_attempt_seconds=1.0,
    hard_feasibility_validation_time_limit_seconds=None,
    hard_feasibility_validation_worker_count=None,
    collect_resource_telemetry=True,
    capture_final_source_decisions=True,
    timeline_max_events=128,
    phase_callback=None,
):
    """Run one operator family continuously in one diagnostic engine call.

    The supplied incumbent is validated once, the immutable production model
    and probe metadata are built once, and each attempt clones only the model
    while rebuilding incumbent-dependent bounds, freezes, hints, and
    validation. This is diagnostic-only and intentionally skips ordinary
    lexicographic Stage 2 between local improvements.
    """

    from .operator_session import ContinuousOperatorSessionConfig

    config = ContinuousOperatorSessionConfig(
        operator_family=operator_family,
        total_time_limit_seconds=total_time_limit_seconds,
        max_attempts=max_attempts,
        per_attempt_time_limit_seconds=per_attempt_time_limit_seconds,
        worker_count=worker_count,
        target_policy=target_policy,
        selected_student_ids=tuple(selected_student_ids),
        selected_grade=selected_grade,
        utilization_cluster_policy=utilization_cluster_policy,
        minimum_next_attempt_seconds=minimum_next_attempt_seconds,
        collect_resource_telemetry=collect_resource_telemetry,
        hard_feasibility_validation_time_limit_seconds=(
            hard_feasibility_validation_time_limit_seconds
        ),
        hard_feasibility_validation_worker_count=(
            hard_feasibility_validation_worker_count
        ),
    )
    if not initial_source_decisions and initial_source_variable_values is None:
        raise ValueError("initial_source_decisions is required")
    return _solve_student_assignment(
        data,
        include_lock_costs=False,
        include_candidate_ledger=False,
        use_hard_feasibility_bootstrap=True,
        collect_stage2_trace=True,
        stage_2_local_bootstrap={
            "adaptive": True,
            "operator_session": True,
            "operator_family": config.operator_family,
            "neighborhood_radii": (config.neighborhood_radius,),
            "max_iterations": config.max_attempts,
            "per_probe_time_limit_seconds": config.per_attempt_time_limit_seconds,
            "worker_count": config.worker_count,
            "target_importance_level": _soft_tier_importance_level(data),
            "neighborhood_radius": config.neighborhood_radius,
            "max_changed_students": config.max_changed_students,
            "target_policy": config.target_policy,
            "selected_student_ids": tuple(config.selected_student_ids),
            "selected_grade": config.selected_grade,
            "utilization_cluster_policy": config.utilization_cluster_policy,
            "minimum_next_attempt_seconds": config.minimum_next_attempt_seconds,
            "source_seed_fingerprint": (
                sha256(
                    repr(tuple(sorted(initial_source_decisions, key=repr))).encode()
                ).hexdigest()
                if initial_source_decisions
                else None
            ),
        },
        stage_2_total_time_limit_seconds=config.total_time_limit_seconds,
        retain_incumbent_on_non_improvement=True,
        alternate_source_decisions=initial_source_decisions,
        alternate_source_variable_values=initial_source_variable_values,
        mature_checkpoint_only=True,
        local_only=True,
        hard_feasibility_validation_time_limit_seconds=(
            config.hard_feasibility_validation_time_limit_seconds
        ),
        hard_feasibility_validation_worker_count=(
            config.hard_feasibility_validation_worker_count
        ),
        collect_incumbent_timeline=False,
        capture_final_source_decisions=capture_final_source_decisions,
        collect_resource_telemetry=config.collect_resource_telemetry,
        phase_callback=phase_callback,
    )


def run_student_assignment_variable_neighborhood_diagnostic(
    data: StudentAssignmentInputDTO,
    *,
    neighborhood_radii=(2, 4, 8),
    max_iterations=12,
    max_attempts_by_radius=None,
    per_probe_time_limit_seconds=90.0,
    total_time_limit_seconds=1800.0,
    worker_count=8,
    hard_feasibility_time_limit_seconds=None,
    hard_feasibility_validation_time_limit_seconds=None,
    hard_feasibility_worker_count=None,
    hard_feasibility_validation_worker_count=None,
    alternate_source_decisions=(),
    alternate_source_variable_values=None,
    collect_incumbent_timeline=True,
    timeline_max_events=128,
    max_changed_students=None,
    capture_final_source_decisions=False,
    collect_resource_telemetry=False,
    phase_callback=None,
):
    """Run bounded R2/R4/R8 CP-SAT local descent for diagnostics only.

    The variable-neighborhood policy is deliberately separate from ordinary
    Stage 2.  It adopts only complete candidates that pass the unchanged full
    model validation, returns to the smallest radius after every adoption, and
    records UNKNOWN separately from a proven infeasible neighborhood.  The
    default attempt limits keep an inconclusive parallel search bounded while
    allowing a target-scale experiment to request a larger explicit budget.
    """

    return _solve_student_assignment(
        data,
        include_lock_costs=False,
        include_candidate_ledger=False,
        use_hard_feasibility_bootstrap=True,
        collect_stage2_trace=True,
        stage_2_local_bootstrap={
            "adaptive": True,
            "variable_neighborhood": True,
            "neighborhood_radii": tuple(neighborhood_radii),
            "max_iterations": max_iterations,
            "max_attempts_by_radius": dict(max_attempts_by_radius or {}),
            "per_probe_time_limit_seconds": per_probe_time_limit_seconds,
            "worker_count": worker_count,
            "target_importance_level": _soft_tier_importance_level(data),
            "max_changed_students": max_changed_students,
        },
        stage_2_total_time_limit_seconds=total_time_limit_seconds,
        retain_incumbent_on_non_improvement=True,
        alternate_source_decisions=alternate_source_decisions,
        alternate_source_variable_values=alternate_source_variable_values,
        # A supplied semantic incumbent is already the comparison starting
        # point. Re-running the independent Stage 1 bootstrap here would spend
        # the diagnostic budget rediscovering the checkpoint instead of
        # measuring the requested neighborhood operator.
        mature_checkpoint_only=bool(
            alternate_source_decisions
            or alternate_source_variable_values is not None
        ),
        hard_feasibility_time_limit_seconds=hard_feasibility_time_limit_seconds,
        hard_feasibility_validation_time_limit_seconds=(
            hard_feasibility_validation_time_limit_seconds
        ),
        hard_feasibility_worker_count=hard_feasibility_worker_count,
        hard_feasibility_validation_worker_count=hard_feasibility_validation_worker_count,
        collect_incumbent_timeline=collect_incumbent_timeline,
        timeline_max_events=timeline_max_events,
        capture_final_source_decisions=capture_final_source_decisions,
        collect_resource_telemetry=collect_resource_telemetry,
        phase_callback=phase_callback,
    )


def run_student_assignment_targeted_repair_diagnostic(
    data: StudentAssignmentInputDTO,
    *,
    selected_student_ids,
    neighborhood_radius=2,
    time_limit_seconds=90.0,
    total_time_limit_seconds=1800.0,
    worker_count=8,
    hard_feasibility_time_limit_seconds=None,
    hard_feasibility_validation_time_limit_seconds=None,
    hard_feasibility_worker_count=None,
    hard_feasibility_validation_worker_count=None,
    alternate_source_decisions=(),
    alternate_source_variable_values=None,
    collect_incumbent_timeline=True,
    timeline_max_events=128,
    capture_final_source_decisions=False,
    collect_resource_telemetry=False,
):
    """Probe a strict v2 improvement while freezing unselected students.

    This is diagnostic-only targeted repair guidance.  ``selected_student_ids``
    restricts which source-decision owners may move inside the existing full
    model; CP-SAT still validates every hard rule and the ordinary full-model
    validator still gates adoption.  No adaptive policy is implied by this
    entry point, and ordinary production Stage 2 does not call it.
    """

    selected_student_ids = tuple(sorted(set(selected_student_ids), key=repr))
    if not selected_student_ids:
        raise ValueError("selected_student_ids must contain at least one student")
    return _solve_student_assignment(
        data,
        include_lock_costs=False,
        include_candidate_ledger=False,
        use_hard_feasibility_bootstrap=True,
        collect_stage2_trace=True,
        stage_2_local_bootstrap={
            "threshold": None,
            "time_limit_seconds": time_limit_seconds,
            "worker_count": worker_count,
            "target_importance_level": _soft_tier_importance_level(data),
            "neighborhood_radius": neighborhood_radius,
            "strict_improvement": True,
            "selected_student_ids": selected_student_ids,
        },
        stage_2_total_time_limit_seconds=total_time_limit_seconds,
        retain_incumbent_on_non_improvement=True,
        local_only=True,
        alternate_source_decisions=alternate_source_decisions,
        alternate_source_variable_values=alternate_source_variable_values,
        hard_feasibility_time_limit_seconds=hard_feasibility_time_limit_seconds,
        hard_feasibility_validation_time_limit_seconds=(
            hard_feasibility_validation_time_limit_seconds
        ),
        hard_feasibility_worker_count=hard_feasibility_worker_count,
        hard_feasibility_validation_worker_count=hard_feasibility_validation_worker_count,
        collect_incumbent_timeline=collect_incumbent_timeline,
        timeline_max_events=timeline_max_events,
        capture_final_source_decisions=capture_final_source_decisions,
        collect_resource_telemetry=collect_resource_telemetry,
    )


def run_student_assignment_targeted_s1_diagnostic(data, *, selected_student_id, **kwargs):
    """Convenience wrapper for a one-student targeted repair probe."""

    return run_student_assignment_targeted_repair_diagnostic(
        data,
        selected_student_ids=(selected_student_id,),
        **kwargs,
    )


def run_student_assignment_targeted_s2_diagnostic(
    data, *, selected_student_ids, **kwargs
):
    """Convenience wrapper for a two-student targeted repair probe."""

    selected_student_ids = tuple(sorted(set(selected_student_ids), key=repr))
    if len(selected_student_ids) != 2:
        raise ValueError("selected_student_ids must contain exactly two students")
    return run_student_assignment_targeted_repair_diagnostic(
        data,
        selected_student_ids=selected_student_ids,
        **kwargs,
    )


def run_student_assignment_ordinary_repair_diagnostic(
    data: StudentAssignmentInputDTO,
    *,
    neighborhood_radius=2,
    max_changed_students=None,
    time_limit_seconds=90.0,
    total_time_limit_seconds=1800.0,
    worker_count=8,
    hard_feasibility_validation_time_limit_seconds=None,
    hard_feasibility_validation_worker_count=None,
    alternate_source_decisions=(),
    alternate_source_variable_values=None,
    collect_incumbent_timeline=True,
    timeline_max_events=128,
    capture_final_source_decisions=False,
    collect_resource_telemetry=False,
):
    """Probe an unselected-student control with the same local-only budget.

    This is the matched control for targeted S1/S2 experiments: CP-SAT may
    choose any changed student, while the full model, strict improvement, and
    full validation remain identical to the targeted operator.
    """

    return _solve_student_assignment(
        data,
        include_lock_costs=False,
        include_candidate_ledger=False,
        use_hard_feasibility_bootstrap=True,
        collect_stage2_trace=True,
        stage_2_local_bootstrap={
            "threshold": None,
            "time_limit_seconds": time_limit_seconds,
            "worker_count": worker_count,
            "target_importance_level": _soft_tier_importance_level(data),
            "neighborhood_radius": neighborhood_radius,
            "max_changed_students": max_changed_students,
            "strict_improvement": True,
        },
        stage_2_total_time_limit_seconds=total_time_limit_seconds,
        retain_incumbent_on_non_improvement=True,
        local_only=True,
        alternate_source_decisions=alternate_source_decisions,
        alternate_source_variable_values=alternate_source_variable_values,
        hard_feasibility_validation_time_limit_seconds=(
            hard_feasibility_validation_time_limit_seconds
        ),
        hard_feasibility_validation_worker_count=(
            hard_feasibility_validation_worker_count
        ),
        collect_incumbent_timeline=collect_incumbent_timeline,
        timeline_max_events=timeline_max_events,
        capture_final_source_decisions=capture_final_source_decisions,
        collect_resource_telemetry=collect_resource_telemetry,
    )


def _solve_student_assignment(
    data,
    *,
    include_lock_costs,
    include_candidate_ledger=True,
    use_hard_feasibility_bootstrap=True,
    substantive_soft_tier_probe=None,
    collect_stage2_trace=False,
    alternate_source_decisions=(),
    alternate_source_variable_values=None,
    mature_checkpoint_only=False,
    local_only=False,
    stage_2_total_time_limit_seconds=None,
    # A complete CP-SAT Stage 1 seed is an approved-quality incumbent.  If a
    # later optimization pass finds a weaker bounded candidate before timing
    # out, retain the complete seed rather than exposing the degraded partial
    # result.  This is the documented two-stage fallback boundary.
    retain_incumbent_on_non_improvement=True,
    stage_2_local_bootstrap=None,
    collect_incumbent_timeline=False,
    timeline_max_events=128,
    hard_feasibility_time_limit_seconds=None,
    hard_feasibility_validation_time_limit_seconds=None,
    hard_feasibility_worker_count=None,
    hard_feasibility_validation_worker_count=None,
    optimization_worker_count=None,
    capture_final_source_decisions=False,
    collect_resource_telemetry=False,
    phase_callback=None,
):
    # This monitor covers the complete diagnostic/engine operation, including
    # input validation, model construction, Stage 1, Stage 2, extraction, and
    # quality facts.  The existing local monitor remains intentionally
    # separate so local-probe memory can still be compared with whole-operation
    # resource use.  Resource telemetry is observational only.
    engine_operation_started = monotonic()
    operation_resource_monitor = ProcessResourceMonitor(
        enabled=collect_resource_telemetry
    ).start()
    _notify_phase(phase_callback, "student_assignment_input", "started")
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
    _notify_phase(
        phase_callback,
        "student_assignment_input",
        "completed",
        request_count=len(data.requests),
        section_count=len(data.sections),
    )
    if data.objective_semantics_version not in {
        OBJECTIVE_SEMANTICS_V1,
        OBJECTIVE_SEMANTICS_V2,
    }:
        raise ValueError(
            f"Unsupported student-assignment objective semantics version: "
            f"{data.objective_semantics_version!r}."
        )
    objective_semantics_v2 = data.objective_semantics_version == OBJECTIVE_SEMANTICS_V2
    if not objective_semantics_v2 and data.objective_importance_scores:
        raise ValueError(
            "Explicit 0-10 scores require objective_semantics_version='v2'."
        )
    objective_importance_scores = resolve_importance_scores(
        labels={
            "section_utilization_balance": data.section_utilization_balance_importance,
            "student_semester_balance": data.student_semester_balance_importance,
            "course_sequence_preferences": data.course_sequence_preferences_importance,
            "difficulty_balance": data.difficulty_balance_importance,
            "course_category_diversity": data.course_category_diversity_importance,
        },
        scores=data.objective_importance_scores if objective_semantics_v2 else None,
    )
    input_semantic_fingerprint = semantic_student_assignment_input_fingerprint(data)
    model = cp_model.CpModel()
    model_build_started = monotonic()
    _notify_phase(phase_callback, "model_construction", "started")
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

    quality_fixed_schedule_commitments = tuple(
        commitment
        for commitment in data.fixed_schedule_commitments
        if _candidate_source_from_commitment(commitment)
        in fixed_commitment_sources
    )

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
                # The first tuple value is retained as the candidate's
                # placement identity, but objective code must derive semester
                # from occupancy.  A timeslot ID is not a semester ID.
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
    complete_required_decision_source_keys = []
    for request in data.requests:
        source_key = ("course", request.request_id)
        if request.delivery_kind == "co_op":
            if source_key not in fixed_commitment_sources:
                complete_required_decision_groups.append([
                    variable
                    for (candidate_source_key, _index), variable in commitment_variables.items()
                    if candidate_source_key == source_key
                ])
                complete_required_decision_source_keys.append(source_key)
            continue
        if not (request.is_mandatory or request.is_primary):
            continue
        if fixed_courses[request.student_id, request.course_id]:
            continue
        complete_required_decision_groups.append([
            variable for _section, variable in request_candidates[request.request_id]
        ])
        complete_required_decision_source_keys.append(source_key)
    for source_key in commitment_candidates:
        if source_key[0] == "commitment" and source_key not in fixed_commitment_sources:
            complete_required_decision_groups.append([
                variable
                for (candidate_source_key, _index), variable in commitment_variables.items()
                if candidate_source_key == source_key
            ])
            complete_required_decision_source_keys.append(source_key)
    if hard_sequence_impossible:
        # Fixed context already violates a same-year prerequisite. No model can
        # turn that into an approvable complete candidate, so the seed must
        # fail closed instead of acting as if source variables could repair it.
        complete_required_decision_groups.append([])
        complete_required_decision_source_keys.append(None)
    hard_feasibility_model = (
        None if mature_checkpoint_only else model.Clone()
    )

    objectives = []
    # Keep a parallel, engine-internal description of each objective's source
    # variables.  CP-SAT expressions belong to one model instance, so the
    # diagnostic probe uses these stable proto indexes to rebuild the same
    # expressions on a clone without relying on objective-list positions.
    objective_metadata = []

    def _term_specs(variables, coefficient=1):
        return tuple((variable.Index(), coefficient) for variable in variables)

    def _append_objective(expression, term_specs, *, kind, **metadata):
        objectives.append(expression)
        objective_metadata.append({
            "kind": kind,
            "term_specs": tuple(term_specs),
            **metadata,
        })
    mandatory = [
        variable
        for request in data.requests if request.is_mandatory
        for _section, variable in request_candidates[request.request_id]
    ]
    # Study/Focus requests and Co-op's paired outside-school commitment are
    # counselor-recognized requirements. Their optional CP-SAT variables allow
    # useful diagnostics, while this top tier makes fulfillment authoritative.
    mandatory.extend(commitment_variables.values())
    _append_objective(
        -sum(mandatory or [0]),
        _term_specs(mandatory, -1),
        kind="fulfillment",
        name="mandatory",
    )
    priority_request_ids = set(data.priority_request_ids)
    priority_rows = [
        variable
        for request in data.requests
        if request.request_id in priority_request_ids and request.is_primary and not request.is_mandatory
        for _section, variable in request_candidates[request.request_id]
    ]
    _append_objective(
        -sum(priority_rows or [0]),
        _term_specs(priority_rows, -1),
        kind="fulfillment",
        name="nominated_priority",
    )
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
        _append_objective(
            -sum(rows or [0]),
            _term_specs(rows, -1),
            kind="fulfillment",
            name=f"primary_priority_{priority_tier}",
        )
    approved_backups = [
        variable
        for request in data.requests if not request.is_primary
        for _section, variable in request_candidates[request.request_id]
    ]
    _append_objective(
        -sum(approved_backups or [0]),
        _term_specs(approved_backups, -1),
        kind="fulfillment",
        name="approved_backup",
    )

    soft_objectives = defaultdict(list)
    # v2 records one input-derived denominator per raw objective.  The v1
    # branch below remains byte-for-byte in mathematical behavior; these
    # values are consumed only when an input explicitly selects v2.
    utilization_denominator = 0
    semester_denominator = 0
    difficulty_denominator = 0
    category_denominator = 0
    v2_normalized_variables = {}
    v2_component_scales = {}
    # Reward the requested soft sequence only when both related courses are
    # applicable to the student.  Applicability is broader than the preferred
    # orientation: if the only legal arrangement is later-course Semester 1
    # and earlier-course Semester 2, the opportunity must still be recorded as
    # unsatisfied rather than disappearing from the objective.
    sequence_satisfied = []
    for preference in data.soft_sequence_preferences:
        for student_id in student_ids:
            early_s1 = list(by_student_course_semester[student_id, preference.earlier_course_id, 1])
            later_s2 = list(by_student_course_semester[student_id, preference.later_course_id, 2])
            early_fixed = 1 if 1 in fixed_semesters.get((student_id, preference.earlier_course_id), ()) else 0
            later_fixed = 1 if 2 in fixed_semesters.get((student_id, preference.later_course_id), ()) else 0
            early_applicable = bool(fixed_semesters.get(
                (student_id, preference.earlier_course_id), ()
            )) or any(
                by_student_course_semester[student_id, preference.earlier_course_id, semester]
                for semester in (1, 2)
            )
            later_applicable = bool(fixed_semesters.get(
                (student_id, preference.later_course_id), ()
            )) or any(
                by_student_course_semester[student_id, preference.later_course_id, semester]
                for semester in (1, 2)
            )
            early_expression = sum(early_s1) + early_fixed
            later_expression = sum(later_s2) + later_fixed
            if not early_applicable or not later_applicable:
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
                utilization_denominator += max(left.capacity_max, right.capacity_max)
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
            _placement_value, occupancy, _pair = commitment_candidates[source_key][index]
            candidate_semester = _candidate_semester_from_occupancy(
                occupancy, semester_by_timeslot,
            )
            metadata = commitment_metadata[source_key]
            if metadata[1] != "co_op":
                continue
            if candidate_semester == 1:
                semester_1 += 4 * variable
            else:
                semester_2 += 4 * variable
        semester_denominator += sum(
            round(request.credit_value * 2)
            for request in requests_by_student[student_id]
            if request.delivery_kind != "co_op"
        )
        semester_denominator += sum(
            round(row.credit_value * 2)
            for row in fixed_rows_by_student[student_id]
        )
        semester_denominator += sum(
            4
            for source_key, _index, _variable in commitment_variables_by_student[student_id]
            if commitment_metadata[source_key][1] == "co_op"
        )
        semester_denominator += sum(
            round(commitment.credit_value * 2)
            for commitment in active_commitments_by_student[student_id]
            if commitment.commitment_kind == "co_op"
        )
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
            # The penalty domain must be large enough to represent every
            # legal annual load difference.  The former course-count * 100
            # bound omitted Study's small contribution and could therefore
            # force a valid Study choice to zero when it shared a semester
            # with a high-difficulty course.  Sum the maximum contribution of
            # each independently selectable source; this is a safe bound, not
            # a change to the objective expression.
            difficulty_upper_bound = sum(
                abs(round(
                    difficulty_by_course.get(request.course_id, 0)
                    * request.credit_value
                ))
                for request in requests_by_student[student_id]
                if request.delivery_kind != "co_op"
            )
            difficulty_upper_bound += sum(
                abs(round(
                    difficulty_by_course.get(row.course_id, 0)
                    * row.credit_value
                ))
                for row in fixed_rows_by_student[student_id]
            )
            difficulty_upper_bound += sum(
                abs(
                    1
                    if commitment.commitment_kind == "study"
                    else round(
                        difficulty_by_course.get(commitment.course_id, 0)
                        * commitment.credit_value
                    )
                )
                for commitment in active_commitments_by_student[student_id]
                if commitment.commitment_kind in {"study", "co_op"}
            )
            for source_key, _index, _variable in commitment_variables_by_student[student_id]:
                metadata = commitment_metadata[source_key]
                if metadata[1] == "study":
                    difficulty_upper_bound += 1
                elif metadata[1] == "co_op":
                    difficulty_upper_bound += abs(round(
                        difficulty_by_course.get(metadata[4], 0)
                        * requests_by_id[metadata[2]].credit_value
                    ))
            difficulty_denominator += difficulty_upper_bound
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
                _placement_value, occupancy, _pair = commitment_candidates[source_key][index]
                candidate_semester = _candidate_semester_from_occupancy(
                    occupancy, semester_by_timeslot,
                )
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
                max(1, difficulty_upper_bound),
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
                            category_denominator += similarity
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
    if preservation_level and not objective_semantics_v2:
        # A stronger counselor choice both promotes this objective above lower
        # soft tiers and scales its internal penalty without exposing numeric
        # weights through the public contract.
        soft_objectives[preservation_level].append(
            preservation_level * sum(preservation_terms or [0])
        )
    if objective_semantics_v2:
        # v2 uses one linear weighted soft tier after the unchanged fulfillment
        # objectives.  Each raw component is first converted to the common
        # bounded integer scale, then multiplied by its canonical 0-10 score.
        # This keeps raw magnitude from silently deciding counselor authority.
        v2_component_inputs = {
            "section_utilization_balance_penalty": (
                sum(section_balance_terms or [0]), utilization_denominator,
                objective_importance_scores["section_utilization_balance"],
            ),
            "student_semester_balance_penalty": (
                sum(semester_balance_terms or [0]), semester_denominator,
                objective_importance_scores["student_semester_balance"],
            ),
            "difficulty_balance_penalty": (
                sum(difficulty_balance_terms or [0]), difficulty_denominator,
                objective_importance_scores["difficulty_balance"],
            ),
            "course_category_diversity_penalty": (
                sum(category_diversity_terms or [0]), category_denominator,
                objective_importance_scores["course_category_diversity"],
            ),
            "course_sequence_preferences_penalty": (
                len(sequence_satisfied) - sum(
                    item[2] for item in sequence_satisfied
                ),
                len(sequence_satisfied),
                objective_importance_scores["course_sequence_preferences"],
            ),
        }
        weighted_terms = []
        component_specs = {}
        for name, (raw_expression, denominator, importance_score) in v2_component_inputs.items():
            denominator = int(denominator)
            normalized = model.NewIntVar(
                0,
                NORMALIZED_OBJECTIVE_SCALE,
                f"v2_normalized_{name}",
            )
            if denominator > 0:
                model.AddDivisionEquality(
                    normalized,
                    raw_expression * NORMALIZED_OBJECTIVE_SCALE,
                    denominator,
                )
            else:
                model.Add(normalized == 0)
            v2_normalized_variables[name] = normalized
            v2_component_scales[name] = {
                "denominator": denominator,
                "normalized_scale": NORMALIZED_OBJECTIVE_SCALE,
                "importance_score": int(importance_score),
            }
            component_term = ((normalized.Index(), int(importance_score)),)
            component_specs[name] = component_term
            weighted_terms.extend(component_term)
        _append_objective(
            sum(
                int(importance_score) * variable
                for name, variable in v2_normalized_variables.items()
                for importance_score in (v2_component_scales[name]["importance_score"],)
            ) if weighted_terms else 0,
            weighted_terms,
            kind="soft_tier",
            name="normalized_soft_preferences",
            importance_level=10,
            semantics_version=OBJECTIVE_SEMANTICS_V2,
            normalized_scale=NORMALIZED_OBJECTIVE_SCALE,
            component_specs=component_specs,
            component_scales=v2_component_scales,
        )
        if preservation_level:
            _append_objective(
                preservation_level * sum(preservation_terms or [0]),
                _term_specs(preservation_terms, preservation_level),
                kind="preservation",
                name="schedule_preservation",
                semantics_version=OBJECTIVE_SEMANTICS_V2,
                preservation_level=data.schedule_preservation_level,
            )
    if objective_semantics_v2:
        # The legacy component-by-component tiers were populated above for
        # shared raw-term construction, but v2 has already emitted its single
        # normalized weighted tier.
        soft_objectives.clear()
    for level in sorted(soft_objectives, reverse=True):
        level_term_specs = []
        level_component_specs = {}
        # The existing same-priority tier is the sum of its component
        # expressions.  Record only their source variables so a diagnostic
        # clone can constrain the exact aggregate without changing production
        # objective construction.
        if level == sequence_level and sequence_level:
            sequence_terms = _term_specs(
                [item[2] for item in sequence_satisfied], -1
            )
            level_term_specs.extend(sequence_terms)
            level_component_specs["soft_sequence_preferences_satisfied"] = sequence_terms
        if level == utilization_level and utilization_level:
            utilization_terms = _term_specs(section_balance_terms)
            level_term_specs.extend(utilization_terms)
            level_component_specs["section_utilization_balance_penalty"] = utilization_terms
        if level == semester_level and semester_level:
            semester_terms = _term_specs(semester_balance_terms)
            level_term_specs.extend(semester_terms)
            level_component_specs["student_semester_balance_penalty"] = semester_terms
        if level == difficulty_level and difficulty_level:
            difficulty_terms = _term_specs(difficulty_balance_terms)
            level_term_specs.extend(difficulty_terms)
            level_component_specs["difficulty_balance_penalty"] = difficulty_terms
        if level == category_diversity_level and category_diversity_level:
            category_terms = _term_specs(category_diversity_terms)
            level_term_specs.extend(category_terms)
            level_component_specs["course_category_diversity_penalty"] = category_terms
        if level == preservation_level and preservation_level:
            preservation_terms_spec = _term_specs(
                preservation_terms, preservation_level
            )
            level_term_specs.extend(preservation_terms_spec)
            level_component_specs["schedule_preservation_move_penalty"] = preservation_terms_spec
        _append_objective(
            sum(soft_objectives[level]),
            level_term_specs,
            kind="soft_tier",
            importance_level=level,
            component_specs=level_component_specs,
        )
    # A final opaque-ID objective makes equivalent recommendations stable.
    final_tie_break_terms = tuple(
        (variable.Index(), request_id * 100000 + section_id)
        for (request_id, section_id), variable in variables.items()
        if any(
            candidate_section.section_id == section_id and candidate_variable is variable
            for candidate_section, candidate_variable in request_candidates[request_id]
        )
    )
    _append_objective(
        sum(
            coefficient * model.GetIntVarFromProtoIndex(variable_index)
            for variable_index, coefficient in final_tie_break_terms
        ) if final_tie_break_terms else 0,
        final_tie_break_terms,
        kind="tie_break",
        name="opaque_source_order",
    )

    def _solver_objective_components(candidate_solver):
        """Read exact aggregate objective facts from one CP-SAT candidate."""

        components = {
            "section_utilization_balance_penalty": float(
                sum(candidate_solver.Value(item) for item in section_balance_terms)
            ),
            "student_semester_balance_penalty": float(
                sum(candidate_solver.Value(item) for item in semester_balance_terms)
            ),
            "difficulty_balance_penalty": float(
                sum(candidate_solver.Value(item) for item in difficulty_balance_terms)
            ),
            "course_category_diversity_penalty": float(
                sum(candidate_solver.Value(item) for item in category_diversity_terms)
            ),
            "schedule_preservation_move_penalty": float(
                sum(candidate_solver.Value(item) for item in preservation_terms)
            ),
            "soft_sequence_preferences_satisfied": float(
                sum(candidate_solver.Value(item[2]) for item in sequence_satisfied)
            ),
        }
        if objective_semantics_v2:
            components["objective_semantics_version"] = OBJECTIVE_SEMANTICS_V2
            components["normalized_components"] = {
                name: float(candidate_solver.Value(variable))
                for name, variable in v2_normalized_variables.items()
            }
            components["weighted_normalized_contributions"] = {
                name: float(
                    candidate_solver.Value(variable)
                    * v2_component_scales[name]["importance_score"]
                )
                for name, variable in v2_normalized_variables.items()
            }
            components["normalization"] = {
                name: dict(scale)
                for name, scale in v2_component_scales.items()
            }
        return components

    def _source_variable_values(source_decisions):
        """Translate semantic source decisions back to required variables."""

        decisions = dict(source_decisions or ())
        values = {}
        for source_key, decision_group in zip(
            complete_required_decision_source_keys,
            complete_required_decision_groups,
        ):
            if source_key is None or not decision_group:
                return None, {
                    "reason": "missing_required_source_decision",
                    "source_key": source_key,
                    "decision_group_size": len(decision_group),
                }
            decision_key = source_key
            if source_key[0] == "course":
                request = requests_by_id[source_key[1]]
                if request.delivery_kind == "co_op":
                    # Co-op is requested through a CourseRequest but is
                    # extracted into the commitment namespace. Preserve the
                    # semantic distinction when a detached seed is mapped
                    # back to this model's source variables.
                    decision_key = ("commitment", source_key[1])
            if decision_key not in decisions:
                return None, {
                    "reason": "missing_required_source_decision",
                    "source_key": decision_key,
                    "model_source_key": source_key,
                    "decision_group_size": len(decision_group),
                }
            for variable in decision_group:
                values[variable.Index()] = 0
            target = decisions[decision_key]
            selected_variable = None
            if source_key[0] == "course":
                request = requests_by_id[source_key[1]]
                if request.delivery_kind == "co_op":
                    target_occupancy = target[3]
                    choices = commitment_candidates[source_key]
                    for index, (_placement, occupancy, _pair) in enumerate(choices):
                        if occupancy == target_occupancy:
                            selected_variable = commitment_variables[source_key, index]
                            break
                else:
                    target_section_id = target[1]
                    if target_section_id is None and target[2] is not None:
                        target_section_id = -target[2]
                    for section, variable in request_candidates[source_key[1]]:
                        if (
                            section.section_id == target_section_id
                            and section.semester == target[3]
                            and section.timeslot_id == target[4]
                            and (
                                request.delivery_kind == "online"
                                or section.half_semester_segment == target[5]
                            )
                        ):
                            selected_variable = variable
                            break
            else:
                target_occupancy = target[3]
                choices = commitment_candidates[source_key]
                for index, (_placement, occupancy, _pair) in enumerate(choices):
                    if occupancy == target_occupancy:
                        selected_variable = commitment_variables[source_key, index]
                        break
            if selected_variable is None:
                return None, {
                    "reason": "source_decision_does_not_match_candidate",
                    "source_key": source_key,
                    "target": target,
                }
            values[selected_variable.Index()] = 1
        return values, None

    _notify_phase(
        phase_callback,
        "model_construction",
        "completed",
        elapsed_seconds=monotonic() - model_build_started,
        variable_count=len(model.Proto().variables),
        constraint_count=len(model.Proto().constraints),
    )

    stage_1_seed_time_limit = (
        max(
            data.time_limit_seconds,
            (
                hard_feasibility_time_limit_seconds
                if hard_feasibility_time_limit_seconds is not None
                else STUDENT_ASSIGNMENT_HARD_FEASIBILITY_TIME_LIMIT_SECONDS
            ),
        )
        if use_hard_feasibility_bootstrap
        else data.time_limit_seconds
    )
    stage_1_validation_time_limit = (
        max(
            data.time_limit_seconds,
            (
                hard_feasibility_validation_time_limit_seconds
                if hard_feasibility_validation_time_limit_seconds is not None
                else STUDENT_ASSIGNMENT_HARD_FEASIBILITY_VALIDATION_TIME_LIMIT_SECONDS
            ),
        )
        if use_hard_feasibility_bootstrap
        else data.time_limit_seconds
    )
    if mature_checkpoint_only:
        if not alternate_source_decisions and alternate_source_variable_values is None:
            raise ValueError(
                "mature_checkpoint_only requires a supplied semantic checkpoint"
            )
        hard_feasibility_seed_model = None
        hard_feasibility_seed_solver = None
        hard_feasibility_source_variable_indexes = ()
        _hard_feasibility_outcome = cp_model.FEASIBLE
        validated_seed_solver = None
        stage_1_timings = {
            "model_construction_wall_time_seconds": monotonic() - model_build_started,
            "seed_skipped": True,
            "validation_skipped": True,
            "operation_wall_time_seconds": 0.0,
        }
    else:
        stage_1_seed_started = monotonic()
        _notify_phase(phase_callback, "hard_feasibility_seed", "started")
        (
            hard_feasibility_seed_model,
            hard_feasibility_seed_solver,
            hard_feasibility_source_variable_indexes,
            _hard_feasibility_outcome,
        ) = _solve_complete_hard_feasibility_seed(
            hard_feasibility_model,
            complete_required_decision_groups,
            stage_1_seed_time_limit,
            worker_count=(
                hard_feasibility_worker_count
                if hard_feasibility_worker_count is not None
                else STUDENT_ASSIGNMENT_HARD_FEASIBILITY_WORKER_COUNT
            ),
        )
        stage_1_seed_elapsed = monotonic() - stage_1_seed_started
        _notify_phase(
            phase_callback,
            "hard_feasibility_seed",
            "completed",
            elapsed_seconds=stage_1_seed_elapsed,
            outcome=_outcome_name(_hard_feasibility_outcome),
        )
        stage_1_validation_started = monotonic()
        _notify_phase(phase_callback, "hard_feasibility_validation", "started")
        validated_seed_solver = _validate_complete_hard_feasibility_seed(
            model,
            hard_feasibility_seed_model,
            hard_feasibility_seed_solver,
            hard_feasibility_source_variable_indexes,
            stage_1_validation_time_limit,
            worker_count=(
                hard_feasibility_validation_worker_count
                if hard_feasibility_validation_worker_count is not None
                else STUDENT_ASSIGNMENT_HARD_FEASIBILITY_VALIDATION_WORKER_COUNT
            ),
        )
        stage_1_validation_elapsed = monotonic() - stage_1_validation_started
        _notify_phase(
            phase_callback,
            "hard_feasibility_validation",
            "completed",
            elapsed_seconds=stage_1_validation_elapsed,
            validated=validated_seed_solver is not None,
        )
        stage_1_timings = {
            "model_construction_wall_time_seconds": monotonic() - model_build_started,
            "seed_requested_time_limit_seconds": float(stage_1_seed_time_limit),
            "seed_worker_count": int(
                hard_feasibility_worker_count
                if hard_feasibility_worker_count is not None
                else STUDENT_ASSIGNMENT_HARD_FEASIBILITY_WORKER_COUNT
            ),
            "seed_external_wall_time_seconds": stage_1_seed_elapsed,
            "seed_solver_wall_time_seconds": float(
                hard_feasibility_seed_solver.WallTime()
                if hard_feasibility_seed_solver is not None
                and hasattr(hard_feasibility_seed_solver, "WallTime")
                else 0.0
            ),
            "validation_requested_time_limit_seconds": float(stage_1_validation_time_limit),
            "validation_worker_count": int(
                hard_feasibility_validation_worker_count
                if hard_feasibility_validation_worker_count is not None
                else STUDENT_ASSIGNMENT_HARD_FEASIBILITY_VALIDATION_WORKER_COUNT
            ),
            "validation_external_wall_time_seconds": stage_1_validation_elapsed,
            "validation_solver_wall_time_seconds": float(
                validated_seed_solver.WallTime()
                if validated_seed_solver is not None
                and hasattr(validated_seed_solver, "WallTime")
                else 0.0
            ),
            "operation_wall_time_seconds": stage_1_seed_elapsed + stage_1_validation_elapsed,
        }
    # Validation transfers the source values into a solver backed by the full
    # production model. The feasibility clone is no longer part of the
    # handoff, so release it before Stage 2 to keep a failed or successful
    # bootstrap from doubling the large optimization model's memory footprint.
    hard_feasibility_model = None
    hard_feasibility_seed_model = None

    stage_2_seed_solver = validated_seed_solver
    alternate_seed_validated = False
    alternate_seed_resolution_failure = None
    alternate_seed_materialization_elapsed = 0.0
    alternate_seed_validation_elapsed = 0.0

    if alternate_source_decisions:
        if alternate_source_variable_values is not None:
            alternate_values = dict(alternate_source_variable_values)
        else:
            alternate_materialization_started = monotonic()
            _notify_phase(
                phase_callback,
                "mature_seed_materialization",
                "started",
            )
            alternate_values, alternate_seed_resolution_failure = (
                _source_variable_values(alternate_source_decisions)
            )
            alternate_seed_materialization_elapsed = (
                monotonic() - alternate_materialization_started
            )
            _notify_phase(
                phase_callback,
                "mature_seed_materialization",
                "completed",
                elapsed_seconds=alternate_seed_materialization_elapsed,
            )
        if alternate_values is not None:
            alternate_validation_time_limit = max(
                data.time_limit_seconds,
                (
                    hard_feasibility_validation_time_limit_seconds
                    if hard_feasibility_validation_time_limit_seconds is not None
                    else STUDENT_ASSIGNMENT_HARD_FEASIBILITY_VALIDATION_TIME_LIMIT_SECONDS
                ),
            )
            if stage_2_total_time_limit_seconds is not None:
                alternate_validation_time_limit = min(
                    alternate_validation_time_limit,
                    stage_2_total_time_limit_seconds,
                )
            alternate_validation_started = monotonic()
            _notify_phase(
                phase_callback,
                "mature_seed_validation",
                "started",
            )
            stage_2_seed_solver = _validate_source_decision_candidate(
                model,
                complete_required_decision_groups,
                alternate_values,
                alternate_validation_time_limit,
                worker_count=(
                    hard_feasibility_validation_worker_count
                    if hard_feasibility_validation_worker_count is not None
                    else STUDENT_ASSIGNMENT_HARD_FEASIBILITY_VALIDATION_WORKER_COUNT
                ),
            )
            alternate_seed_validation_elapsed = (
                monotonic() - alternate_validation_started
            )
            _notify_phase(
                phase_callback,
                "mature_seed_validation",
                "completed",
                elapsed_seconds=alternate_seed_validation_elapsed,
                validated=stage_2_seed_solver is not None,
            )
            alternate_seed_validated = stage_2_seed_solver is not None
            if not alternate_seed_validated:
                stage_2_seed_solver = validated_seed_solver

    if mature_checkpoint_only:
        if not alternate_seed_validated:
            raise ValueError(
                "The supplied mature checkpoint failed full-model validation"
            )
        # The checkpoint is now the validated incumbent for every downstream
        # diagnostic step. It deliberately does not masquerade as a newly
        # generated Stage 1 seed in ordinary scheduling facts.
        validated_seed_solver = stage_2_seed_solver
        stage_1_timings.update({
            "mature_seed_materialization_wall_time_seconds": (
                alternate_seed_materialization_elapsed
            ),
            "mature_seed_validation_wall_time_seconds": (
                alternate_seed_validation_elapsed
            ),
            "operation_wall_time_seconds": (
                alternate_seed_materialization_elapsed
                + alternate_seed_validation_elapsed
            ),
        })

    sequence_opportunities = tuple(
        (student_id, preference.earlier_course_id, preference.later_course_id)
        for preference, student_id, _variable in sequence_satisfied
    )
    stage_1_quality = None
    stage_1_quality_elapsed = 0.0
    if validated_seed_solver is not None:
        stage_1_quality_started = monotonic()
        (
            stage_1_assignments,
            stage_1_commitment_assignments,
            _stage_1_assigned_request_ids,
            _stage_1_selected_by_section,
            _stage_1_assigned_commitment_sources,
        ) = _extract_solver_candidate(
            solver=validated_seed_solver,
            data=data,
            request_candidates=request_candidates,
            commitment_variables=commitment_variables,
            commitment_candidates=commitment_candidates,
            commitment_metadata=commitment_metadata,
            previous_enrollment_by_request=previous_enrollment_by_request,
        )
        stage_1_quality = _evaluate_student_assignment_quality(
            data,
            assignments=stage_1_assignments,
            commitment_assignments=stage_1_commitment_assignments,
            sequence_opportunities=sequence_opportunities,
            fixed_enrollments=fixed_rows,
            fixed_schedule_commitments=quality_fixed_schedule_commitments,
            solver_objective_components=_solver_objective_components(
                validated_seed_solver
            ),
            include_entity_metrics=True,
        )
        stage_1_quality_elapsed = monotonic() - stage_1_quality_started
    stage_1_timings["quality_extraction_wall_time_seconds"] = stage_1_quality_elapsed
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
    optimization_passes = []
    optimization_trace = []
    incumbent_timeline = []

    def _full_quality_for_solver(candidate_solver):
        """Evaluate one complete candidate without influencing CP-SAT."""

        (
            pass_assignments,
            pass_commitment_assignments,
            _assigned_request_ids,
            _selected_by_section,
            _assigned_commitment_sources,
        ) = _extract_solver_candidate(
            solver=candidate_solver,
            data=data,
            request_candidates=request_candidates,
            commitment_variables=commitment_variables,
            commitment_candidates=commitment_candidates,
            commitment_metadata=commitment_metadata,
            previous_enrollment_by_request=previous_enrollment_by_request,
        )
        return _evaluate_student_assignment_quality(
            data,
            assignments=pass_assignments,
            commitment_assignments=pass_commitment_assignments,
            sequence_opportunities=sequence_opportunities,
            fixed_enrollments=fixed_rows,
            fixed_schedule_commitments=quality_fixed_schedule_commitments,
            solver_objective_components=_solver_objective_components(
                candidate_solver
            ),
            include_entity_metrics=True,
        )

    def _quality_for_solver(candidate_solver):
        """Return bounded quality facts for one completed optimization pass."""

        return _compact_student_assignment_quality(
            _full_quality_for_solver(candidate_solver)
        )

    def _candidate_quality_facts(candidate_solver):
        """Return compact evaluator facts for one diagnostic candidate."""

        candidate_quality = _full_quality_for_solver(candidate_solver)
        # Diagnostic replays may supply a detached alternate seed. Compare
        # against the actual seed used by this probe, not an independently
        # generated equal-objective Stage 1 solver.
        baseline_quality = _full_quality_for_solver(stage_2_seed_solver)
        return {
            "summary": _compact_student_assignment_quality(candidate_quality),
            "comparison": _compare_student_assignment_quality(
                baseline_quality,
                candidate_quality,
            ),
        }

    def _source_decision_fingerprint(candidate_solver):
        assignments, commitments, *_rest = _extract_solver_candidate(
            solver=candidate_solver,
            data=data,
            request_candidates=request_candidates,
            commitment_variables=commitment_variables,
            commitment_candidates=commitment_candidates,
            commitment_metadata=commitment_metadata,
            previous_enrollment_by_request=previous_enrollment_by_request,
        )
        decisions = {}
        for assignment in assignments:
            decisions[("course", assignment.request_id)] = (
                assignment.student_id,
                assignment.section_id,
                assignment.online_supervision_session_id,
                assignment.semester,
                assignment.timeslot_id,
                assignment.half_semester_segment,
            )
        for commitment in commitments:
            decisions[("commitment", commitment.request_id)] = (
                commitment.student_id,
                commitment.commitment_kind,
                commitment.course_request_id,
                commitment.occupancy,
            )
        return tuple(sorted(decisions.items(), key=repr))

    def _source_decision_variable_values(candidate_solver):
        """Return exact required-source values for same-model validation.

        Diagnostic replays in the same DTO/model build can use these values to
        validate a candidate without guessing which internal variable encoded
        a source tuple.  The semantic source map remains the diagnostic
        identity; this mapping is only a same-model validation transport.
        """

        return {
            variable.Index(): int(candidate_solver.Value(variable))
            for decision_group in complete_required_decision_groups
            for variable in decision_group
        }

    def _candidate_count(candidate_solver):
        assignments, commitments, *_rest = _extract_solver_candidate(
            solver=candidate_solver,
            data=data,
            request_candidates=request_candidates,
            commitment_variables=commitment_variables,
            commitment_candidates=commitment_candidates,
            commitment_metadata=commitment_metadata,
            previous_enrollment_by_request=previous_enrollment_by_request,
        )
        return len(assignments) + len(commitments)

    def _source_decision_summary(candidate_solver):
        assignments, commitments, assigned_request_ids, selected_by_section, _ = (
            _extract_solver_candidate(
                solver=candidate_solver,
                data=data,
                request_candidates=request_candidates,
                commitment_variables=commitment_variables,
                commitment_candidates=commitment_candidates,
                commitment_metadata=commitment_metadata,
                previous_enrollment_by_request=previous_enrollment_by_request,
            )
        )
        section_loads = {
            section_id: len(fixed_by_section[section_id]) + len(rows)
            for section_id, rows in selected_by_section.items()
        }
        return {
            "source_decision_count": len(_source_decision_fingerprint(candidate_solver)),
            "assigned_request_count": len(assigned_request_ids),
            "required_request_count": sum(
                request.is_mandatory for request in data.requests
            ),
            "special_commitment_count": len(commitments),
            "section_loads": dict(sorted(section_loads.items())),
            "hard_valid": True,
            "fulfillment_complete": all(
                request.request_id in assigned_request_ids
                for request in data.requests
                if request.is_mandatory
            ),
        }

    probe_source_decision_owners = tuple(
        (
            None
            if source_key is None
            else requests_by_id[source_key[1]].student_id
            if source_key[0] == "course"
            else commitment_metadata.get(source_key, (None,))[0]
        )
        for source_key in complete_required_decision_source_keys
    )
    probe_required_decision_groups = tuple(
        tuple(group) for group in complete_required_decision_groups
    )

    def _build_probe_context(seed_solver):
        # The model, source groups, owner map, and objective metadata are
        # session-static. Only the validated incumbent-dependent fields change
        # after an improvement is adopted. This prevents a continuous session
        # from rebuilding the production model or its static scope per probe.
        return SubstantiveSoftTierProbeContext(
            model=model,
            objective_metadata=tuple(objective_metadata),
            complete_required_decision_groups=probe_required_decision_groups,
            source_decision_owners=probe_source_decision_owners,
            validated_seed_solver=seed_solver,
            seed_outcome=_hard_feasibility_outcome,
            solver_objective_components=_solver_objective_components,
            candidate_counts=_candidate_count,
            source_decision_fingerprint=_source_decision_fingerprint,
            source_decision_summary=_source_decision_summary,
            source_decision_variable_values=_source_decision_variable_values,
            seed_source_decision_variable_values=_source_decision_variable_values,
            candidate_quality_facts=_candidate_quality_facts,
            student_grades=tuple(data.student_grades),
            seed_objective_vector=_objective_values(
                seed_solver, objectives
            ) if seed_solver is not None else (),
        )

    stage_2_budget_seconds = (
        (
            stage_2_total_time_limit_seconds
            or STUDENT_ASSIGNMENT_OPTIMIZATION_TIME_LIMIT_SECONDS
        )
        if use_hard_feasibility_bootstrap
        else None
    )
    local_bootstrap_facts = None
    local_memory_monitor = None
    operator_session_budget = bool(
        stage_2_local_bootstrap
        and stage_2_local_bootstrap.get("operator_session")
    )
    stage_2_deadline = (
        MonotonicDeadline(
            max(0.0, float(stage_2_budget_seconds)),
            started_at=(
                engine_operation_started
                if operator_session_budget
                else monotonic()
            ),
        )
        if stage_2_budget_seconds is not None
        else None
    )
    stage_2_started = (
        stage_2_deadline.started_at
        if stage_2_deadline is not None
        else monotonic()
    )
    if stage_2_local_bootstrap is not None and stage_2_seed_solver is not None:
        local_config = dict(stage_2_local_bootstrap)
        target_level = local_config.get(
            "target_importance_level", _soft_tier_importance_level(data)
        )
        target_metadata = next(
            (
                metadata
                for metadata in objective_metadata
                if metadata.get("kind") == "soft_tier"
                and metadata.get("importance_level") == target_level
            ),
            None,
        )
        if target_metadata is None:
            local_bootstrap_facts = {
                "status": "not_applicable",
                "reason": "substantive_tier_not_present",
            }
        else:
            # Local/VNS probes can run for many minutes.  Keep lightweight
            # native process-memory telemetry beside the diagnostic facts so a
            # promotion decision can distinguish solver behavior from host
            # pressure without adding a runtime dependency.
            local_memory_monitor = ProcessMemoryMonitor().start()
            operator_setup_started = monotonic()
            _notify_phase(phase_callback, "operator_static_setup", "started")
            adaptive = bool(local_config.get("adaptive", False))
            operator_session = bool(local_config.get("operator_session", False))
            iterations = []
            current_seed_value = None
            last_result = None
            any_candidate_validated = False
            any_improvement_adopted = False
            variable_neighborhood = bool(
                local_config.get("variable_neighborhood", False)
            )
            max_attempts_by_radius = dict(
                local_config.get("max_attempts_by_radius", {})
            )
            radius_attempts = {}
            radius_stop_reasons = []
            stopping_reason = None
            local_session_started = monotonic()
            session_target_history = []
            session_target_guidance = []
            selected_student_ids = tuple(local_config.get("selected_student_ids", ()))
            selected_grade = local_config.get("selected_grade")
            grade_bounded = selected_grade is not None

            def _current_substantive_value(candidate_solver):
                return int(sum(
                    coefficient * candidate_solver.Value(
                        model.GetIntVarFromProtoIndex(variable_index)
                    )
                    for variable_index, coefficient in target_metadata["term_specs"]
                ))

            def _remaining_stage2_budget():
                if stage_2_deadline is None:
                    return float(data.time_limit_seconds)
                return max(0.001, stage_2_deadline.remaining())

            def _validate_local_result(local_result, validation_deadline):
                if (
                    not local_result.complete_candidate_found
                    or not local_result.candidate_source_variable_values
                ):
                    return None, 0.0, {
                        "classification": "not_attempted",
                        "solver_outcome": None,
                        "error": None,
                    }
                started = monotonic()
                validation_outcome = _validate_source_decision_candidate_with_status(
                    model,
                    complete_required_decision_groups,
                    local_result.candidate_source_variable_values,
                    max(0.001, validation_deadline.remaining()),
                    worker_count=(
                        hard_feasibility_validation_worker_count
                        if hard_feasibility_validation_worker_count is not None
                        else STUDENT_ASSIGNMENT_HARD_FEASIBILITY_VALIDATION_WORKER_COUNT
                    ),
                )
                return (
                    validation_outcome.solver,
                    monotonic() - started,
                    {
                        "classification": validation_outcome.classification,
                        "solver_outcome": validation_outcome.solver_outcome,
                        "error": validation_outcome.error,
                    },
                )

            _notify_phase(
                phase_callback,
                "operator_static_setup",
                "completed",
                elapsed_seconds=monotonic() - operator_setup_started,
            )

            if adaptive:
                if grade_bounded:
                    radii = (None,)
                else:
                    radii = tuple(
                        int(radius)
                        for radius in local_config.get(
                            "neighborhood_radii",
                            (local_config.get("neighborhood_radius", 2), 2, 4),
                        )
                        if int(radius) >= 0
                    ) or (2,)
                max_iterations = max(1, int(local_config.get("max_iterations", 3)))
                per_probe_limit = float(
                    local_config.get("per_probe_time_limit_seconds", 90.0)
                )
                if operator_session:
                    current_operator_radius = (
                        None
                        if grade_bounded
                        else int(local_config.get("neighborhood_radius", radii[0]))
                    )
                    radii = (current_operator_radius,)
                    variable_neighborhood = False
                radius_index = 0
                iteration_count = 0
                # Keep the final facts well-defined if the shared deadline is
                # already exhausted before the first iteration. Grade-bounded
                # sessions use this same summary path, but their guidance is
                # initialized inside the loop when an attempt starts.
                target_guidance = {}
                while iteration_count < max_iterations and radius_index < len(radii):
                    if (
                        stage_2_deadline is not None
                        and stage_2_deadline.remaining() <= 0.001
                    ):
                        stopping_reason = "shared_budget_exhausted"
                        break
                    if (
                        operator_session
                        and iteration_count > 0
                        and stage_2_deadline is not None
                        and stage_2_deadline.remaining()
                        < float(local_config.get("minimum_next_attempt_seconds", 1.0))
                    ):
                        stopping_reason = "insufficient_budget_for_next_attempt"
                        break
                    current_radius = radii[radius_index]
                    radius_attempts[current_radius] = (
                        radius_attempts.get(current_radius, 0) + 1
                    )
                    current_seed_value = _current_substantive_value(stage_2_seed_solver)
                    iteration_deadline = MonotonicDeadline.start(
                        min(per_probe_limit, _remaining_stage2_budget())
                    )
                    probe_limit = max(0.001, iteration_deadline.remaining())
                    target_preparation_started = monotonic()
                    _notify_phase(
                        phase_callback,
                        "target_preparation",
                        "started",
                        iteration=iteration_count + 1,
                    )
                    selected_student_ids = tuple(
                        local_config.get("selected_student_ids", ())
                    )
                    required_targets = (
                        int(local_config["max_changed_students"])
                        if local_config.get("max_changed_students") is not None
                        else None
                    )
                    target_guidance = {
                        "guidance_only": True,
                        "objective_attribution": False,
                    }
                    if grade_bounded:
                        grade_facts = build_grade_opportunity_facts(
                            data,
                            _source_decision_fingerprint(stage_2_seed_solver),
                            _full_quality_for_solver(stage_2_seed_solver),
                        )
                        target_guidance.update({
                            "policy": "fixed_actual_grade",
                            "selected_grade": int(selected_grade),
                            "grade_opportunities": tuple(
                                item.__dict__ for item in grade_facts
                            ),
                            "grade_opportunity": next(
                                (
                                    item.__dict__
                                    for item in grade_facts
                                    if item.grade_level == int(selected_grade)
                                ),
                                {},
                            ),
                        })
                        session_target_guidance.append(target_guidance)
                    if operator_session and local_config.get("max_changed_students"):
                        utilization_cluster = str(
                            local_config.get("operator_family", "")
                        ).startswith("targeted_utilization_")
                        current_quality = None
                        if (
                            utilization_cluster
                            or local_config.get("target_policy") == "dynamic"
                        ):
                            current_quality = _full_quality_for_solver(
                                stage_2_seed_solver
                            )
                        if utilization_cluster:
                            utilization_selection = (
                                select_utilization_cluster_targets(
                                    data,
                                    current_quality,
                                    _source_decision_fingerprint(stage_2_seed_solver),
                                    target_scope_size=required_targets,
                                    policy=local_config.get(
                                        "utilization_cluster_policy",
                                        "interaction_aware",
                                    ),
                                    fixed_student_ids=(
                                        selected_student_ids
                                        if local_config.get("target_policy") == "fixed"
                                        else ()
                                    ),
                                )
                            )
                            selected_student_ids = (
                                utilization_selection.selected_student_ids
                            )
                            target_guidance = dict(
                                utilization_selection.guidance_facts
                            )
                        else:
                            if local_config.get("target_policy") == "dynamic":
                                ranked_students = rank_students_by_quality_pressure(
                                    data, current_quality
                                )
                                ranked_student_ids = tuple(
                                    item.student_id for item in ranked_students
                                )
                            else:
                                ranked_student_ids = ()
                            selected_student_ids = select_operator_session_targets(
                                local_config.get("operator_family"),
                                target_policy=local_config.get("target_policy", "dynamic"),
                                ranked_student_ids=ranked_student_ids,
                                fixed_student_ids=selected_student_ids,
                            )
                            target_guidance["policy"] = "student_quality_pressure"
                            target_guidance["selected_student_ids"] = selected_student_ids
                        if len(selected_student_ids) != required_targets:
                            stopping_reason = "no_eligible_target"
                            break
                        session_target_guidance.append(target_guidance)
                    session_target_history.append(selected_student_ids)
                    _notify_phase(
                        phase_callback,
                        "target_preparation",
                        "completed",
                        elapsed_seconds=monotonic() - target_preparation_started,
                        iteration=iteration_count + 1,
                        selected_student_ids=selected_student_ids,
                    )
                    probe_selected_student_ids = (
                        selected_student_ids if operator_session else ()
                    )
                    local_result = probe_substantive_soft_tier(
                        _build_probe_context(stage_2_seed_solver),
                        target_importance_level=target_level,
                        threshold=current_seed_value - 1,
                        neighborhood_radius=radii[radius_index],
                        max_changed_students=local_config.get(
                            "max_changed_students"
                        ),
                        selected_student_ids=probe_selected_student_ids,
                        selected_grade=(int(selected_grade) if grade_bounded else None),
                        time_limit_seconds=probe_limit,
                        worker_count=int(local_config.get("worker_count", 8)),
                        phase_callback=phase_callback,
                    )
                    last_result = local_result
                    candidate_before_validation = local_result.candidate_substantive_value
                    validation_started = monotonic()
                    _notify_phase(
                        phase_callback,
                        "candidate_validation",
                        "started",
                        iteration=iteration_count + 1,
                    )
                    (
                        local_validator,
                        validation_elapsed,
                        validation_facts,
                    ) = _validate_local_result(
                        local_result,
                        iteration_deadline,
                    )
                    _notify_phase(
                        phase_callback,
                        "candidate_validation",
                        "completed",
                        elapsed_seconds=monotonic() - validation_started,
                        iteration=iteration_count + 1,
                        validated=local_validator is not None,
                    )
                    candidate_validated = local_validator is not None
                    if candidate_validated:
                        any_candidate_validated = True
                    adopted = bool(
                        candidate_validated
                        and candidate_before_validation is not None
                        and candidate_before_validation < current_seed_value
                    )
                    if adopted:
                        stage_2_seed_solver = local_validator
                        alternate_seed_validated = True
                        any_improvement_adopted = True
                    iterations.append({
                        "iteration": iteration_count + 1,
                        "radius": radii[radius_index],
                        "effective_neighborhood_radius": local_result.effective_neighborhood_radius,
                        "eligible_targeted_source_decision_count": (
                            local_result.eligible_targeted_source_decision_count
                        ),
                        "status": local_result.status,
                        "elapsed_seconds": local_result.elapsed_seconds,
                        "solver_wall_time_seconds": local_result.solver_wall_time_seconds,
                        "probe_timings": dict(local_result.timings),
                        "time_limit_seconds": probe_limit,
                        "iteration_requested_time_limit_seconds": iteration_deadline.requested_seconds,
                        "iteration_elapsed_seconds": iteration_deadline.elapsed(),
                        "iteration_remaining_seconds": iteration_deadline.remaining(),
                        "incumbent_before": current_seed_value,
                        "candidate_value": candidate_before_validation,
                        "candidate_validated": candidate_validated,
                        "validation_classification": validation_facts[
                            "classification"
                        ],
                        "validation_solver_outcome": validation_facts[
                            "solver_outcome"
                        ],
                        "validation_error": validation_facts["error"],
                        "adopted": adopted,
                        "validation_elapsed_seconds": validation_elapsed,
                        "model_variable_count": local_result.model_variable_count,
                        "model_constraint_count": local_result.model_constraint_count,
                        "branches": local_result.branches,
                        "conflicts": local_result.conflicts,
                        "deterministic_time_seconds": local_result.timings.get(
                            "deterministic_time_seconds"
                        ),
                        "changed_source_decision_count": local_result.changed_source_decision_count,
                        "changed_student_count": local_result.changed_student_count,
                        "component_values": dict(local_result.candidate_component_values),
                        "component_deltas": dict(local_result.component_deltas),
                        # These are compact, bounded evaluator facts: no raw
                        # per-entity payload is persisted, but each adopted
                        # candidate still carries the counselor-readable
                        # aggregate and improved/unchanged/worsened counts.
                        "candidate_quality_summary": dict(
                            local_result.candidate_quality_summary
                        ),
                        "quality_comparison": dict(
                            local_result.quality_comparison
                        ),
                        "affected_student_ids": tuple(local_result.affected_student_ids),
                        "affected_section_ids": tuple(local_result.affected_section_ids),
                        "section_load_deltas": dict(local_result.section_load_deltas),
                        "target_guidance": dict(target_guidance),
                        "selected_grade": int(selected_grade) if grade_bounded else None,
                        "best_bound": local_result.best_bound,
                        "attempt_number_for_radius": radius_attempts[current_radius],
                        "cumulative_session_elapsed_seconds": (
                            monotonic() - local_session_started
                        ),
                        "memory": local_memory_monitor.sample(),
                    })
                    iteration_count += 1
                    if adopted:
                        iterations[-1]["transition_reason"] = "adopted_restart_radius_two"
                        radius_index = 0
                        continue
                    if variable_neighborhood:
                        if local_result.status == "infeasible":
                            radius_stop_reasons.append({
                                "radius": current_radius,
                                "reason": "proven_infeasible",
                                "attempts": radius_attempts[current_radius],
                            })
                            iterations[-1]["transition_reason"] = (
                                "neighborhood_proven_exhausted"
                            )
                            radius_index += 1
                        elif local_result.status == "unknown":
                            configured_attempts = max_attempts_by_radius.get(
                                current_radius,
                                max_attempts_by_radius.get(str(current_radius), 1),
                            )
                            max_attempts = max(1, int(configured_attempts))
                            if radius_attempts[current_radius] < max_attempts:
                                iterations[-1]["transition_reason"] = (
                                    "retry_unknown_neighborhood"
                                )
                            else:
                                radius_stop_reasons.append({
                                    "radius": current_radius,
                                    "reason": "unresolved_unknown",
                                    "attempts": radius_attempts[current_radius],
                                })
                                iterations[-1]["transition_reason"] = (
                                    "neighborhood_unresolved_expand"
                                )
                                radius_index += 1
                        else:
                            iterations[-1]["transition_reason"] = (
                                "no_improvement_expand"
                            )
                            radius_index += 1
                    else:
                        # The original adaptive diagnostic expands once after
                        # a failed radius and keeps its historical bounded
                        # iteration semantics unchanged.
                        iterations[-1]["transition_reason"] = "no_improvement_expand"
                        radius_index += 1
                if stopping_reason is None:
                    if last_result is not None and last_result.status == "unknown":
                        stopping_reason = "unresolved_unknown"
                    elif last_result is not None and last_result.status == "infeasible":
                        stopping_reason = (
                            "proven_scope_exhausted"
                            if operator_session
                            else "proven_local_optimum"
                        )
                    elif iteration_count >= max_iterations:
                        stopping_reason = (
                            "attempt_cap_reached"
                            if operator_session
                            else "iteration_budget_exhausted"
                        )
                    elif radius_index >= len(radii):
                        stopping_reason = "neighborhood_sequence_exhausted"
                final_value = _current_substantive_value(stage_2_seed_solver)
                local_bootstrap_facts = {
                    "adaptive": True,
                    "operator_session": operator_session,
                    "operator_family": local_config.get("operator_family"),
                    "source_seed_fingerprint": local_config.get(
                        "source_seed_fingerprint"
                    ),
                    "target_policy": local_config.get("target_policy"),
                    "session_context_reused": operator_session,
                    "static_probe_context_built_once": operator_session,
                    "session_target_history": tuple(session_target_history),
                    "session_target_guidance": tuple(session_target_guidance),
                    "utilization_cluster_policy": local_config.get(
                        "utilization_cluster_policy"
                    ),
                    "variable_neighborhood": variable_neighborhood,
                    "target_importance_level": target_level,
                    "status": last_result.status if last_result is not None else "unknown",
                    "elapsed_seconds": sum(item["elapsed_seconds"] for item in iterations),
                    "cumulative_session_elapsed_seconds": (
                        monotonic() - local_session_started
                    ),
                    "solver_wall_time_seconds": sum(
                        item["solver_wall_time_seconds"] for item in iterations
                    ),
                    "probe_operation_wall_time_seconds": sum(
                        item["probe_timings"].get("operation_total_seconds", 0.0)
                        for item in iterations
                    ),
                    "validation_elapsed_seconds": sum(
                        item["validation_elapsed_seconds"] for item in iterations
                    ),
                    "time_limit_seconds": per_probe_limit,
                    "max_iterations": max_iterations,
                    "deadline_requested_time_limit_seconds": stage_2_budget_seconds,
                    "deadline_elapsed_seconds": stage_2_deadline.elapsed() if stage_2_deadline else None,
                    "deadline_remaining_seconds": stage_2_deadline.remaining() if stage_2_deadline else None,
                    "neighborhood_radius": iterations[-1]["radius"] if iterations else None,
                    "effective_neighborhood_radius": (
                        iterations[-1].get("effective_neighborhood_radius")
                        if iterations else None
                    ),
                    "eligible_targeted_source_decision_count": (
                        iterations[-1].get(
                            "eligible_targeted_source_decision_count"
                        )
                        if iterations else 0
                    ),
                    "baseline_substantive_value": iterations[0]["incumbent_before"] if iterations else current_seed_value,
                    "requested_threshold": iterations[0]["incumbent_before"] - 1 if iterations else None,
                    "candidate_substantive_value": final_value,
                    "changed_source_decision_count": iterations[-1]["changed_source_decision_count"] if iterations else 0,
                    "changed_student_count": iterations[-1].get("changed_student_count") if iterations else 0,
                    "max_changed_students": local_config.get("max_changed_students"),
                    "selected_student_ids": tuple(selected_student_ids),
                    "selected_grade": int(selected_grade) if grade_bounded else None,
                    "grade_opportunity": (
                        target_guidance.get("grade_opportunity", {})
                        if grade_bounded else {}
                    ),
                    "grade_opportunities": (
                        target_guidance.get("grade_opportunities", ())
                        if grade_bounded else ()
                    ),
                    "component_values": dict(last_result.candidate_component_values) if last_result is not None else {},
                    "component_deltas": dict(last_result.component_deltas) if last_result is not None else {},
                    "candidate_found": any_candidate_validated,
                    "candidate_validated": any_candidate_validated,
                    "validation_classification": (
                        iterations[-1].get("validation_classification", "not_attempted")
                        if iterations else "not_attempted"
                    ),
                    "validation_solver_outcome": (
                        iterations[-1].get("validation_solver_outcome")
                        if iterations else None
                    ),
                    "validation_error": (
                        iterations[-1].get("validation_error")
                        if iterations else None
                    ),
                    "improvement_adopted": any_improvement_adopted,
                    "radius_attempts": dict(radius_attempts),
                    "radius_stop_reasons": tuple(radius_stop_reasons),
                    "stopping_reason": stopping_reason,
                    "configured_session_budget_seconds": stage_2_budget_seconds,
                    "session_elapsed_seconds": (
                        stage_2_deadline.elapsed()
                        if stage_2_deadline is not None
                        else monotonic() - local_session_started
                    ),
                    "external_overrun_seconds": max(
                        0.0,
                        (
                            stage_2_deadline.elapsed() - stage_2_budget_seconds
                            if stage_2_deadline is not None
                            and stage_2_budget_seconds is not None
                            else 0.0
                        ),
                    ),
                    "iterations": tuple(iterations),
                    "memory": local_memory_monitor.stop(),
                }
            else:
                seed_substantive_value = _current_substantive_value(stage_2_seed_solver)
                if local_config.get("threshold") is None:
                    local_config["threshold"] = int(seed_substantive_value) - 1
                requested_local_time_limit = float(local_config["time_limit_seconds"])
                remaining_before_bootstrap = _remaining_stage2_budget()
                local_deadline = MonotonicDeadline.start(
                    min(requested_local_time_limit, remaining_before_bootstrap)
                )
                local_config["time_limit_seconds"] = min(
                    float(local_config["time_limit_seconds"]),
                    max(0.001, local_deadline.remaining()),
                )
                local_result = probe_substantive_soft_tier(
                    _build_probe_context(stage_2_seed_solver),
                    **local_config,
                    phase_callback=phase_callback,
                )
                (
                    local_validator,
                    validation_elapsed,
                    validation_facts,
                ) = _validate_local_result(
                    local_result,
                    local_deadline,
                )
                candidate_validated = local_validator is not None
                if candidate_validated:
                    stage_2_seed_solver = local_validator
                    alternate_seed_validated = True
                local_bootstrap_facts = {
                    "adaptive": False,
                    "target_importance_level": local_config.get(
                        "target_importance_level", _soft_tier_importance_level(data)
                    ),
                    "status": local_result.status,
                    "elapsed_seconds": local_result.elapsed_seconds,
                    "solver_wall_time_seconds": local_result.solver_wall_time_seconds,
                    "probe_timings": dict(local_result.timings),
                    "validation_elapsed_seconds": validation_elapsed,
                    "time_limit_seconds": requested_local_time_limit,
                    "deadline_requested_time_limit_seconds": local_deadline.requested_seconds,
                    "deadline_elapsed_seconds": local_deadline.elapsed(),
                    "deadline_remaining_seconds": local_deadline.remaining(),
                    "neighborhood_radius": local_result.neighborhood_radius,
                    "baseline_substantive_value": local_result.baseline_substantive_value,
                    "requested_threshold": local_result.requested_threshold,
                    "candidate_substantive_value": local_result.candidate_substantive_value,
                    "changed_source_decision_count": local_result.changed_source_decision_count,
                    "changed_student_count": local_result.changed_student_count,
                    "max_changed_students": local_config.get("max_changed_students"),
                    "selected_student_ids": tuple(
                        local_config.get("selected_student_ids", ())
                    ),
                    "component_values": dict(local_result.candidate_component_values),
                    "component_deltas": dict(local_result.component_deltas),
                    "affected_student_ids": tuple(local_result.affected_student_ids),
                    "affected_section_ids": tuple(local_result.affected_section_ids),
                    "section_load_deltas": dict(local_result.section_load_deltas),
                    "candidate_found": local_result.complete_candidate_found,
                    "candidate_validated": candidate_validated,
                    "validation_classification": validation_facts[
                        "classification"
                    ],
                    "validation_solver_outcome": validation_facts[
                        "solver_outcome"
                    ],
                    "validation_error": validation_facts["error"],
                    "improvement_adopted": candidate_validated,
                    "model_variable_count": local_result.model_variable_count,
                    "model_constraint_count": local_result.model_constraint_count,
                    "branches": local_result.branches,
                    "conflicts": local_result.conflicts,
                    "deterministic_time_seconds": local_result.timings.get(
                        "deterministic_time_seconds"
                    ),
                    "iterations": (),
                    "stopping_reason": (
                        "improvement_adopted"
                        if candidate_validated
                        else (
                            "proven_infeasible"
                            if local_result.status == "infeasible"
                            else "unresolved_unknown"
                            if local_result.status == "unknown"
                            else "no_improvement"
                        )
                    ),
                    "memory": local_memory_monitor.stop(),
                }

    reference_source_decisions = dict(
        _source_decision_fingerprint(stage_2_seed_solver)
    ) if stage_2_seed_solver is not None else {}

    def _stage2_candidate_trace(candidate_solver):
        source_decisions = _source_decision_fingerprint(candidate_solver)
        source_map = dict(source_decisions)
        changed_source_keys = {
            key
            for key in set(reference_source_decisions) | set(source_map)
            if reference_source_decisions.get(key) != source_map.get(key)
        }
        affected_student_ids = set()
        affected_section_ids = set()
        for key in changed_source_keys:
            for value in (
                reference_source_decisions.get(key),
                source_map.get(key),
            ):
                if not value or not isinstance(value, tuple):
                    continue
                affected_student_ids.add(value[0])
                if key[0] == "course" and value[1] is not None:
                    affected_section_ids.add(value[1])
        assignments, commitments, assigned_request_ids, _selected, _sources = (
            _extract_solver_candidate(
                solver=candidate_solver,
                data=data,
                request_candidates=request_candidates,
                commitment_variables=commitment_variables,
                commitment_candidates=commitment_candidates,
                commitment_metadata=commitment_metadata,
                previous_enrollment_by_request=previous_enrollment_by_request,
            )
        )
        return {
            "objective_vector": _objective_values(candidate_solver, objectives),
            "substantive_components": dict(_solver_objective_components(candidate_solver)),
            "source_decision_fingerprint": sha256(
                repr(source_decisions).encode()
            ).hexdigest(),
            "source_decision_count": len(source_decisions),
            "changed_source_decision_count": len(changed_source_keys),
            "affected_student_ids": tuple(sorted(affected_student_ids)),
            "affected_section_ids": tuple(sorted(affected_section_ids)),
            "assigned_request_count": len(assigned_request_ids),
            "assignment_count": len(assignments),
            "special_commitment_count": len(commitments),
            "hard_valid": True,
            "fulfillment_complete": all(
                request.request_id in assigned_request_ids
                for request in data.requests
                if request.is_mandatory
            ),
        }

    if substantive_soft_tier_probe is not None:
        return probe_substantive_soft_tier(
            _build_probe_context(stage_2_seed_solver),
            **substantive_soft_tier_probe,
            phase_callback=phase_callback,
        )

    solver, outcome = _solve_lexicographically(
        model,
        objectives,
        data.time_limit_seconds,
        initial_assignment_hints=initial_assignment_hints,
        validated_seed_solver=stage_2_seed_solver,
        worker_count=(
            optimization_worker_count
            if optimization_worker_count is not None
            else STUDENT_ASSIGNMENT_OPTIMIZATION_WORKER_COUNT
        ),
        total_time_limit_seconds=(
            max(
                0.001,
                stage_2_deadline.remaining()
                if stage_2_deadline is not None
                else stage_2_budget_seconds - (monotonic() - stage_2_started),
            )
            if stage_2_local_bootstrap is not None
            and stage_2_budget_seconds is not None
            else stage_2_budget_seconds
        ),
        deadline=stage_2_deadline,
        pass_facts=optimization_passes,
        pass_quality_callback=_quality_for_solver,
        pass_trace=optimization_trace if collect_stage2_trace else None,
        pass_candidate_callback=(
            _stage2_candidate_trace if collect_stage2_trace else None
        ),
        retain_incumbent_on_non_improvement=retain_incumbent_on_non_improvement,
        incumbent_timeline=(
            incumbent_timeline if collect_incumbent_timeline else None
        ),
        timeline_candidate_callback=(
            _stage2_candidate_trace if collect_incumbent_timeline else None
        ),
        timeline_max_events=timeline_max_events,
        skip_optimization=local_only,
    )
    optimization_facts = _optimization_facts(
        hard_feasibility_outcome=_hard_feasibility_outcome,
        required_group_count=len(complete_required_decision_groups),
        hard_seed_solver=hard_feasibility_seed_solver,
        validated_seed_solver=validated_seed_solver,
        stage_2_seed_solver=stage_2_seed_solver,
        final_solver=solver,
        final_outcome=outcome,
        objectives=objectives,
        optimization_time_limit_seconds=stage_2_budget_seconds,
        stage_1_quality=stage_1_quality,
        optimization_passes=optimization_passes,
        stage_1_timings=stage_1_timings,
        input_semantic_fingerprint=input_semantic_fingerprint,
        full_model_variable_count=len(model.Proto().variables),
        full_model_constraint_count=len(model.Proto().constraints),
        optimization_worker_count=(
            optimization_worker_count
            if optimization_worker_count is not None
            else STUDENT_ASSIGNMENT_OPTIMIZATION_WORKER_COUNT
        ),
        model_family_variable_counts=_model_family_variable_counts(model),
        objective_metadata_summary=tuple(
            {
                "index": index,
                "kind": metadata.get("kind"),
                "name": metadata.get("name"),
                "importance_level": metadata.get("importance_level"),
                "semantics_version": metadata.get(
                    "semantics_version", data.objective_semantics_version
                ),
                "normalized_scale": metadata.get("normalized_scale"),
                "component_scales": metadata.get("component_scales", {}),
                "term_count": len(metadata.get("term_specs", ())),
                "component_term_counts": {
                    name: len(term_specs)
                    for name, term_specs in metadata.get(
                        "component_specs", {}
                    ).items()
                },
            }
            for index, metadata in enumerate(objective_metadata)
        ),
    )
    optimization_facts["objective_semantics"] = {
        "version": data.objective_semantics_version,
        "importance_scores": dict(objective_importance_scores),
        "label_presets": dict(IMPORTANCE_LABEL_TO_SCORE),
        "normalized_scale": (
            NORMALIZED_OBJECTIVE_SCALE
            if objective_semantics_v2
            else None
        ),
        "normalization": (
            {
                name: dict(scale)
                for name, scale in v2_component_scales.items()
            }
            if objective_semantics_v2
            else {}
        ),
    }
    if collect_incumbent_timeline:
        optimization_facts["stage_2"]["incumbent_timeline"] = tuple(
            incumbent_timeline
        )
    optimization_facts["stage_2"]["alternate_seed_validated"] = alternate_seed_validated
    optimization_facts["stage_2"]["alternate_seed_resolution_failure"] = (
        alternate_seed_resolution_failure
    )
    substantive_pass_wall_time = 0.0
    tie_break_pass_wall_time = 0.0
    substantive_level = _soft_tier_importance_level(data)
    for pass_fact in optimization_passes:
        metadata = objective_metadata[pass_fact["objective_index"]]
        if (
            metadata.get("kind") == "soft_tier"
            and metadata.get("importance_level") == substantive_level
        ):
            substantive_pass_wall_time += pass_fact.get("wall_time_seconds", 0.0)
        elif metadata.get("kind") == "tie_break":
            tie_break_pass_wall_time += pass_fact.get("wall_time_seconds", 0.0)
    optimization_facts["stage_2"]["substantive_pass_wall_time_seconds"] = (
        substantive_pass_wall_time
    )
    optimization_facts["stage_2"]["tie_break_pass_wall_time_seconds"] = (
        tie_break_pass_wall_time
    )
    optimization_facts["stage_2"]["operation_wall_time_seconds"] = (
        monotonic() - stage_2_started
    )
    optimization_facts["stage_2"]["configured_deadline_seconds"] = (
        stage_2_budget_seconds
    )
    optimization_facts["stage_2"]["deadline_remaining_seconds"] = (
        stage_2_deadline.remaining() if stage_2_deadline is not None else None
    )
    if capture_final_source_decisions and solver is not None:
        optimization_facts["stage_2"]["final_source_decisions"] = (
            _source_decision_fingerprint(solver)
        )
    if local_bootstrap_facts is not None:
        if local_bootstrap_facts.get("operator_session"):
            local_bootstrap_facts["external_overrun_seconds"] = max(
                0.0,
                optimization_facts["stage_2"]["operation_wall_time_seconds"]
                - float(local_bootstrap_facts.get(
                    "configured_session_budget_seconds", 0.0
                ) or 0.0),
            )
        optimization_facts["stage_2_local_bootstrap"] = local_bootstrap_facts
        # The shared Stage 2 deadline is started immediately before the local
        # bootstrap, so its elapsed value already includes probe setup,
        # CP-SAT, candidate extraction, and candidate validation.
        local_session_wall_time = local_bootstrap_facts.get(
            "deadline_elapsed_seconds",
            local_bootstrap_facts.get("elapsed_seconds", 0.0),
        )
        optimization_facts["stage_2"]["post_local_optimization_wall_time_seconds"] = max(
            0.0,
            optimization_facts["stage_2"].get("operation_wall_time_seconds", 0.0)
            - local_session_wall_time,
        )
    if collect_stage2_trace:
        for trace in optimization_trace:
            metadata = objective_metadata[trace["objective_index"]]
            trace["objective_kind"] = metadata["kind"]
            trace["objective_name"] = metadata.get("name")
            trace["importance_level"] = metadata.get("importance_level")
        optimization_facts["stage_2_trace"] = optimization_trace
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
        optimization_facts["operation_resource_monitor"] = (
            operation_resource_monitor.stop()
        )
        if include_lock_costs:
            result = replace(result, lock_costs=_build_lock_costs(data, result))
            optimization_facts["operation_resource_monitor"] = (
                operation_resource_monitor.stop()
            )
        if local_bootstrap_facts and local_bootstrap_facts.get("operator_session"):
            final_operation_wall_time = monotonic() - stage_2_started
            optimization_facts["stage_2"]["operation_wall_time_seconds"] = (
                final_operation_wall_time
            )
            local_bootstrap_facts["session_elapsed_seconds"] = (
                final_operation_wall_time
            )
            local_bootstrap_facts["external_overrun_seconds"] = max(
                0.0,
                final_operation_wall_time
                - float(local_bootstrap_facts.get(
                    "configured_session_budget_seconds", 0.0
                ) or 0.0),
            )
        return result

    final_candidate_extraction_started = monotonic()
    (
        assignments,
        commitment_assignments,
        assigned_request_ids,
        selected_by_section,
        assigned_commitment_sources,
    ) = _extract_solver_candidate(
        solver=solver,
        data=data,
        request_candidates=request_candidates,
        commitment_variables=commitment_variables,
        commitment_candidates=commitment_candidates,
        commitment_metadata=commitment_metadata,
        previous_enrollment_by_request=previous_enrollment_by_request,
    )
    final_candidate_extraction_elapsed = monotonic() - final_candidate_extraction_started
    final_quality_started = monotonic()
    stage_2_quality = _evaluate_student_assignment_quality(
        data,
        assignments=assignments,
        commitment_assignments=commitment_assignments,
        sequence_opportunities=sequence_opportunities,
        fixed_enrollments=fixed_rows,
        fixed_schedule_commitments=quality_fixed_schedule_commitments,
        solver_objective_components=_solver_objective_components(solver),
        include_entity_metrics=True,
    )
    final_quality_elapsed = monotonic() - final_quality_started
    if stage_1_quality is not None:
        optimization_facts["quality"] = {
            "stage_1": _compact_student_assignment_quality(stage_1_quality),
            "stage_2": _compact_student_assignment_quality(stage_2_quality),
            "stage_1_vs_stage_2": _compare_student_assignment_quality(
                stage_1_quality, stage_2_quality,
            ),
        }
    review_started = monotonic()
    review_items = []

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

    review_diagnostics_elapsed = monotonic() - review_started
    result_reconstruction_started = monotonic()
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
            **(
                {
                    "objective_semantics_version": OBJECTIVE_SEMANTICS_V2,
                    "normalized_components": {
                        name: float(solver.Value(variable))
                        for name, variable in v2_normalized_variables.items()
                    },
                    "weighted_normalized_contributions": {
                        name: float(
                            solver.Value(variable)
                            * v2_component_scales[name]["importance_score"]
                        )
                        for name, variable in v2_normalized_variables.items()
                    },
                    "normalization": {
                        name: dict(scale)
                        for name, scale in v2_component_scales.items()
                    },
                }
                if objective_semantics_v2
                else {}
            ),
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
    result_reconstruction_elapsed = monotonic() - result_reconstruction_started
    optimization_facts["finalization_timings"] = {
        "candidate_extraction_wall_time_seconds": final_candidate_extraction_elapsed,
        "quality_evaluation_wall_time_seconds": final_quality_elapsed,
        "review_diagnostics_wall_time_seconds": review_diagnostics_elapsed,
        "result_reconstruction_wall_time_seconds": result_reconstruction_elapsed,
    }
    optimization_facts["operation_resource_monitor"] = (
        operation_resource_monitor.stop()
    )
    if include_lock_costs:
        result = replace(result, lock_costs=_build_lock_costs(data, result))
        optimization_facts["operation_resource_monitor"] = (
            operation_resource_monitor.stop()
        )
    if local_bootstrap_facts and local_bootstrap_facts.get("operator_session"):
        final_operation_wall_time = monotonic() - stage_2_started
        optimization_facts["stage_2"]["operation_wall_time_seconds"] = (
            final_operation_wall_time
        )
        local_bootstrap_facts["session_elapsed_seconds"] = final_operation_wall_time
        local_bootstrap_facts["external_overrun_seconds"] = max(
            0.0,
            final_operation_wall_time
            - float(local_bootstrap_facts.get(
                "configured_session_budget_seconds", 0.0
            ) or 0.0),
        )
    return result
