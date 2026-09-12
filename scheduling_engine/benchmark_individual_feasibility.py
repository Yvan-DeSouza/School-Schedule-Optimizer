"""Exact per-student feasibility checks for detached synthetic benchmarks.

This is a benchmark preflight, not the production student-assignment workflow.
It preserves the candidate semantics of the supplied ``StudentAssignmentInputDTO``
and deliberately ignores competition from other students.
"""

from __future__ import annotations

from collections import defaultdict
from itertools import combinations
from dataclasses import dataclass
from time import perf_counter

from .constants import HALF_SEMESTER_SEGMENTS
from .student_assignment.occupancy import request_occupied_half_segments
from .student_assignment.validation import validate_input


@dataclass(frozen=True)
class IndividualCandidate:
    candidate_kind: str
    identity: tuple[int, ...]
    semester: int
    timeslot_id: int | None
    occupancy: tuple[tuple[int, str], ...]


@dataclass(frozen=True)
class IndividualDecisionGroup:
    source_request_ids: tuple[int, ...]
    source_kind: str
    candidates: tuple[IndividualCandidate, ...]


@dataclass(frozen=True)
class IndividualFeasibilityResult:
    student_id: int
    status: str
    decision_groups: tuple[IndividualDecisionGroup, ...]
    selected_candidates: tuple[IndividualCandidate, ...]
    reason: str | None = None
    conflict_certificate: tuple[tuple[tuple[int, ...], tuple[int, ...]], ...] = ()


def _candidate_sort_key(candidate):
    return (
        candidate.semester,
        candidate.timeslot_id or 0,
        candidate.candidate_kind,
        candidate.identity,
    )


def _deduplicate(candidates):
    selected = {}
    for candidate in candidates:
        key = (
            candidate.candidate_kind,
            candidate.identity,
            candidate.semester,
            candidate.timeslot_id,
            candidate.occupancy,
        )
        selected[key] = candidate
    return tuple(sorted(selected.values(), key=_candidate_sort_key))


def _section_candidates(request, offering_sections):
    """Mirror the core's current detached offering-to-section candidate lookup."""

    return tuple(
        IndividualCandidate(
            candidate_kind=(
                "online_supervision_section"
                if section.section_id < 0 else "instructional_section"
            ),
            identity=(section.section_id,),
            semester=section.semester,
            timeslot_id=section.timeslot_id,
            occupancy=tuple(
                (section.timeslot_id, segment)
                for segment in request_occupied_half_segments(request, section)
            ),
        )
        for section in offering_sections.get(request.course_offering_id, ())
        if section.capacity_max > 0
    )


def _paired_half_candidates(left, right, offering_sections):
    candidates = []
    for left_candidate in _section_candidates(left, offering_sections):
        for right_candidate in _section_candidates(right, offering_sections):
            left_id = left_candidate.identity[0]
            right_id = right_candidate.identity[0]
            left_section = next(
                section for section in offering_sections[left.course_offering_id]
                if section.section_id == left_id
            )
            right_section = next(
                section for section in offering_sections[right.course_offering_id]
                if section.section_id == right_id
            )
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
                continue
            occupancy = tuple(sorted(set(left_candidate.occupancy + right_candidate.occupancy)))
            candidates.append(IndividualCandidate(
                candidate_kind="paired_half_sections",
                identity=(left_section.section_id, right_section.section_id),
                semester=left_section.semester,
                timeslot_id=left_section.timeslot_id,
                occupancy=occupancy,
            ))
    return _deduplicate(candidates)


def _special_candidates(data, request):
    available = tuple(
        slot for slot in data.timeslots
        if slot.is_available and slot.academic_year_id == data.academic_year_id
    )
    if request.commitment_type == "study":
        return tuple(
            IndividualCandidate(
                "study_time", (slot.id,), slot.semester, slot.id,
                tuple((slot.id, segment) for segment in HALF_SEMESTER_SEGMENTS),
            )
            for slot in available
        )
    if request.commitment_type == "focus":
        return tuple(
            IndividualCandidate(
                "focus_semester", (semester,), semester, None,
                tuple(
                    (slot.id, segment)
                    for slot in available if slot.semester == semester
                    for segment in HALF_SEMESTER_SEGMENTS
                ),
            )
            for semester in (1, 2)
            if sum(slot.semester == semester for slot in available) == 4
        )
    raise ValueError(f"Unsupported benchmark commitment type {request.commitment_type!r}.")


def _co_op_candidates(data):
    slots = {
        (slot.semester, slot.block): slot
        for slot in data.timeslots
        if slot.is_available and slot.academic_year_id == data.academic_year_id
    }
    candidates = []
    for semester in (1, 2):
        for pair, blocks in (("a_b", ("A", "B")), ("c_d", ("C", "D"))):
            pair_slots = [slots.get((semester, block)) for block in blocks]
            if any(slot is None for slot in pair_slots):
                continue
            candidates.append(IndividualCandidate(
                "connected_two_credit_co_op",
                (semester, 1 if pair == "a_b" else 2), semester, None,
                tuple(
                    (slot.id, segment)
                    for slot in pair_slots
                    for segment in HALF_SEMESTER_SEGMENTS
                ),
            ))
    return tuple(candidates)


def individual_decision_groups(data, student_id):
    """Build every completion-defining candidate group for one fixture student.

    The benchmark currently has no fixed context, locks, or hard prerequisites.
    Refusing unexpected versions of those facts prevents this checker from
    claiming proof for semantics it does not implement.
    """

    if data.fixed_enrollments or data.fixed_schedule_commitments:
        raise ValueError("Benchmark preflight does not support fixed student context.")
    if data.student_assignment_locks or data.special_commitment_locks:
        raise ValueError("Benchmark preflight does not support student locks.")
    if data.hard_prerequisites:
        raise ValueError("Benchmark preflight does not support hard prerequisites.")
    offering_sections = validate_input(data)
    requests = tuple(
        request for request in data.requests
        if request.student_id == student_id
        and request.is_in_scope
        and (request.is_mandatory or request.is_primary)
    )
    groups = []
    paired_request_ids = set()
    requests_by_course = {request.course_id: request for request in requests}
    for request in requests:
        if request.request_id in paired_request_ids:
            continue
        if request.delivery_kind == "co_op":
            groups.append(IndividualDecisionGroup(
                (request.request_id,), "connected_two_credit_co_op", _co_op_candidates(data),
            ))
            continue
        if request.duration == "half_semester" and request.paired_half_course_id:
            partner = requests_by_course.get(request.paired_half_course_id)
            if partner is None or partner.paired_half_course_id != request.course_id:
                return (), f"unpaired_half_course:{request.request_id}"
            paired_request_ids.update((request.request_id, partner.request_id))
            groups.append(IndividualDecisionGroup(
                tuple(sorted((request.request_id, partner.request_id))),
                "paired_half_course", _paired_half_candidates(request, partner, offering_sections),
            ))
            continue
        groups.append(IndividualDecisionGroup(
            (request.request_id,), "course", _section_candidates(request, offering_sections),
        ))
    for request in data.schedule_commitment_requests:
        if request.student_id == student_id and request.is_in_scope:
            groups.append(IndividualDecisionGroup(
                (request.request_id,), request.commitment_type, _special_candidates(data, request),
            ))
    return tuple(groups), None


def _choose_noncolliding(groups):
    """Exact DFS for the small individual occupancy problem; no CP-SAT needed."""

    occupancy_bits = {}
    for group in groups:
        for candidate in group.candidates:
            for occupancy in candidate.occupancy:
                occupancy_bits.setdefault(occupancy, 1 << len(occupancy_bits))
    candidate_masks = [
        tuple((candidate, sum(occupancy_bits[item] for item in candidate.occupancy)) for candidate in group.candidates)
        for group in groups
    ]
    memo = set()

    def search(remaining, occupied):
        key = (remaining, occupied)
        if key in memo:
            return None
        if not remaining:
            return ()
        ranked = []
        for group_index in remaining:
            legal = [item for item in candidate_masks[group_index] if not occupied & item[1]]
            if not legal:
                memo.add(key)
                return None
            ranked.append((len(legal), group_index, legal))
        _count, group_index, legal = min(ranked, key=lambda item: (item[0], item[1]))
        next_remaining = tuple(item for item in remaining if item != group_index)
        for candidate, mask in legal:
            selected = search(next_remaining, occupied | mask)
            if selected is not None:
                return ((group_index, candidate),) + selected
        memo.add(key)
        return None

    return search(tuple(range(len(groups))), 0)


def _whole_slot_hall_certificate(groups):
    """Return a minimal Hall deficiency when every group uses one whole slot."""

    slot_sets = []
    for group in groups:
        slots = set()
        for candidate in group.candidates:
            candidate_slots = {timeslot_id for timeslot_id, _segment in candidate.occupancy}
            if len(candidate_slots) != 1 or len(candidate.occupancy) != len(HALF_SEMESTER_SEGMENTS):
                return ()
            slots.update(candidate_slots)
        slot_sets.append(slots)
    for size in range(2, len(groups) + 1):
        for indexes in combinations(range(len(groups)), size):
            slots = set().union(*(slot_sets[index] for index in indexes))
            if len(slots) < len(indexes):
                return ((
                    tuple(request_id for index in indexes for request_id in groups[index].source_request_ids),
                    tuple(sorted(slots)),
                ),)
    return ()


def check_individual_feasibility(data, student_id):
    """Return an exact, isolated completion check for one detached student."""

    try:
        groups, unresolved_reason = individual_decision_groups(data, student_id)
    except ValueError as error:
        return IndividualFeasibilityResult(student_id, "unresolved", (), (), str(error))
    if unresolved_reason:
        return IndividualFeasibilityResult(student_id, "unresolved", (), (), unresolved_reason)
    if any(not group.candidates for group in groups):
        return IndividualFeasibilityResult(
            student_id, "infeasible", groups, (), "completion group has no usable candidate",
        )
    selected = _choose_noncolliding(groups)
    if selected is None:
        certificate = _whole_slot_hall_certificate(groups)
        reason = "no collision-free completion assignment"
        if certificate:
            request_ids, slots = certificate[0]
            reason = (
                f"Hall deficiency: completion requests {request_ids} have only "
                f"{len(slots)} whole-block candidates {slots}."
            )
        return IndividualFeasibilityResult(
            student_id, "infeasible", groups, (), reason, certificate,
        )
    return IndividualFeasibilityResult(
        student_id, "feasible", groups,
        tuple(candidate for _group_index, candidate in sorted(selected)),
    )


def preflight_individual_feasibility(data):
    """Check every represented student independently, without global allocation."""

    started = perf_counter()
    student_ids = sorted({request.student_id for request in data.requests} | {
        request.student_id for request in data.schedule_commitment_requests
    })
    results = tuple(check_individual_feasibility(data, student_id) for student_id in student_ids)
    grades = dict(data.student_grades)
    by_status_grade = {}
    for status in ("feasible", "infeasible", "unresolved"):
        by_status_grade[status] = {
            grade: sum(result.status == status and grades.get(result.student_id) == grade for result in results)
            for grade in sorted(set(grades.values()))
        }
    return {
        "student_count": len(results),
        "feasible_count": sum(result.status == "feasible" for result in results),
        "infeasible_count": sum(result.status == "infeasible" for result in results),
        "unresolved_count": sum(result.status == "unresolved" for result in results),
        "by_status_grade": by_status_grade,
        "runtime_seconds": perf_counter() - started,
        "results": results,
    }
