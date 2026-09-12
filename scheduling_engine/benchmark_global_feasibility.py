"""Generic reduced global-feasibility diagnostics for benchmark DTOs.

The implementation is shared with the preserved v2 diagnostic behavior.  The
public names intentionally do not identify a particular detached lineage, so a
real production-placement qualification can use the same mathematical checks
without importing fixture construction or fixture identity.
"""

from __future__ import annotations

from .paul_desmarais_v2_2_diagnostics import (
    capacity_only_matching as _capacity_only_matching,
    collision_diagnostic as _collision_diagnostic,
)
from collections import defaultdict
from time import monotonic

from ortools.sat.python import cp_model

from .student_assignment.occupancy import request_occupied_half_segments
from .student_assignment.validation import validate_input


def capacity_only_matching(data):
    """Check ordinary completion groups against accepted section capacity."""

    return _capacity_only_matching(data)


def reduced_collision_diagnostic(data, *, time_limit_seconds=120.0):
    """Check capacity plus accepted student no-double-booking collisions."""

    result = _collision_diagnostic(data, time_limit_seconds=time_limit_seconds)
    # The preserved diagnostic reports the CP-SAT status directly.  Expose a
    # small boolean alongside it so qualification gates can distinguish a
    # proven feasible/optimal diagnostic from INFEASIBLE or UNKNOWN without
    # changing the underlying model or its lineage semantics.
    result["feasible"] = result.get("status") in {"optimal", "feasible"}
    return result


def ordinary_and_half_pair_diagnostic(data, *, time_limit_seconds=120.0):
    """Prove ordinary plus supported paired-half feasibility without extras.

    This intentionally sits between the ordinary collision guard and the full
    student-assignment hard model. It preserves normal-section capacity,
    half-section capacity, paired physical identity, and every student's
    occupied half-slot collision rule; it deliberately excludes online,
    commitments, locks, fixed context, and prerequisites.
    """

    offerings = validate_input(data)
    requests = tuple(
        request for request in data.requests
        if request.is_in_scope
        and (request.is_mandatory or request.is_primary)
        and request.delivery_kind == "normal_instruction"
        and request.duration in {"full_semester", "half_semester"}
    )
    requests_by_student_course = {
        (request.student_id, request.course_id): request
        for request in requests
    }
    model = cp_model.CpModel()
    by_section = defaultdict(list)
    by_student_occupancy = defaultdict(list)
    paired_request_ids = set()
    group_count = 0
    variable_count = 0

    def add_group(group_requests, candidates):
        nonlocal group_count, variable_count
        variables = []
        for candidate_index, candidate in enumerate(candidates):
            variable = model.NewBoolVar(f"half_layer_{group_count}_{candidate_index}")
            variables.append(variable)
            variable_count += 1
            for section, request in candidate:
                by_section[section.section_id].append(variable)
                for segment in request_occupied_half_segments(request, section):
                    by_student_occupancy[request.student_id, section.timeslot_id, segment].append(variable)
        if not variables:
            model.Add(0 == 1)
        else:
            model.AddExactlyOne(variables)
        group_count += 1

    for request in requests:
        if request.request_id in paired_request_ids:
            continue
        if request.duration == "full_semester":
            add_group(
                (request,),
                tuple(
                    ((section, request),)
                    for section in offerings.get(request.course_offering_id, ())
                ),
            )
            continue
        partner = requests_by_student_course.get(
            (request.student_id, request.paired_half_course_id)
        )
        if (
            request.paired_half_course_id is None
            or partner is None
            or partner.paired_half_course_id != request.course_id
        ):
            add_group((request,), ())
            continue
        paired_request_ids.update((request.request_id, partner.request_id))
        candidates = []
        for left in offerings.get(request.course_offering_id, ()):
            for right in offerings.get(partner.course_offering_id, ()):
                if (
                    left.half_semester_pair_key
                    and left.half_semester_pair_key == right.half_semester_pair_key
                ):
                    candidates.append(((left, request), (right, partner)))
        add_group((request, partner), tuple(candidates))

    sections = {
        section.section_id: section
        for rows in offerings.values() for section in rows
        if section.section_id > 0
    }
    for section_id, variables in by_section.items():
        model.Add(sum(variables) <= sections[section_id].capacity_max)
    for variables in by_student_occupancy.values():
        model.Add(sum(variables) <= 1)
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(time_limit_seconds)
    solver.parameters.num_search_workers = 8
    solver.parameters.random_seed = 0
    started = monotonic()
    status = solver.Solve(model)
    status_name = {
        cp_model.OPTIMAL: "optimal",
        cp_model.FEASIBLE: "feasible",
        cp_model.INFEASIBLE: "infeasible",
        cp_model.MODEL_INVALID: "model_invalid",
    }.get(status, "unknown")
    return {
        "status": status_name,
        "feasible": status_name in {"optimal", "feasible"},
        "required_group_count": group_count,
        "variable_count": variable_count,
        "constraint_count": len(model.Proto().constraints),
        "solver_seconds": solver.WallTime(),
        "external_seconds": monotonic() - started,
    }
