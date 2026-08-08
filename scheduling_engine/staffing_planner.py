"""Staffing-aware planning for standalone and combined physical deliveries.

This solver proves that section counts can be covered by qualified teacher-load
capacity, but deliberately does not assign named teachers.  A delivery group is
the decision unit: a combined Dance 11/Dance 12 class consumes one section and
its eligible pool is the intersection of the teachers eligible for both codes.
"""

from __future__ import annotations

from typing import Iterable

from ortools.sat.python import cp_model

from .constraint_compiler import compile_constraints
from .dto import SchedulingInputDTO
from .section_budget_planner import (
    _apply_constraints,
    _generate_candidates,
    _offerings,
    _predicted_by_course,
    _solve_model,
)
from .section_planner import _remaining_capacities


SEMESTER_1_ONLY = "semester_1_only"
SEMESTER_2_ONLY = "semester_2_only"


def _semester_options(offering, candidates):
    """Expand annual candidates into every catalog-legal semester split."""

    values = []
    for candidate in candidates:
        for semester_one in range(candidate.count + 1):
            semester_two = candidate.count - semester_one
            if offering.allowed_semester == SEMESTER_1_ONLY and semester_two:
                continue
            if offering.allowed_semester == SEMESTER_2_ONLY and semester_one:
                continue
            values.append((candidate, semester_one, semester_two))
    return tuple(values)


def _eligible_teachers(offering, compiled, teacher_ids):
    """Require one teacher to satisfy every member course's hard rules."""

    eligible = set(teacher_ids)
    for course_id in offering.member_course_ids:
        eligible &= set(compiled.qualified_teacher_ids_by_course[course_id])
    return eligible


def _infeasible_diagnostics(offerings, options, eligible, capacities, total_required):
    """Translate common physical-group staffing failures for counselors."""

    diagnostics = []
    available = sum(capacities.values())
    if total_required is not None and total_required > available:
        diagnostics.append({
            "code": "total_staffing_capacity_shortfall",
            "required_sections": total_required,
            "available_sections": available,
            "shortfall_sections": total_required - available,
            "message": (
                f"The approved budget requires {total_required} sections, but the "
                f"ready roster has capacity for only {available}."
            ),
        })
    for offering in offerings:
        positive_required = min(
            option[0].count for option in options[offering.id]
        ) > 0
        if positive_required and not eligible[offering.id]:
            diagnostics.append({
                "code": "no_eligible_teacher_for_delivery_group",
                "offering_id": offering.id,
                "member_course_codes": list(offering.member_course_codes),
                "message": (
                    "No teacher on the ready roster holds every qualification "
                    "required by this physical delivery group."
                ),
            })
    if not diagnostics:
        diagnostics.append({
            "code": "shared_qualified_staffing_pool_infeasible",
            "message": (
                "The delivery groups individually have eligible teachers, but they "
                "compete for the same limited semester capacity."
            ),
        })
    return diagnostics


def plan_staffing_counts(
    data: SchedulingInputDTO,
    *,
    effective_requests=None,
    offering_constraints: Iterable[dict] = (),
    teacher_capacity_adjustments: Iterable[dict] = (),
    approved_budget_counts: dict[int, int] | None = None,
):
    """Return a physical section plan supported by the ready teacher roster.

    When ``approved_budget_counts`` is supplied, its physical total is fixed but
    counts may move between delivery groups.  The result reports every movement
    explicitly; the budget approval itself remains unchanged.
    """

    offerings = _offerings(data)
    offering_ids = {offering.id for offering in offerings}
    constraints = {item["offering_id"]: dict(item) for item in offering_constraints}
    if not set(constraints) <= offering_ids:
        raise ValueError("An offering constraint references an unknown delivery group.")
    if approved_budget_counts is not None and set(approved_budget_counts) != offering_ids:
        raise ValueError("The linked budget no longer matches the active delivery groups.")

    requests = tuple(
        effective_requests
        if effective_requests is not None
        else (request for request in data.course_requests if request.is_primary)
    )
    predicted_by_course = _predicted_by_course(data, requests)
    predicted = {
        offering.id: sum(predicted_by_course.get(course_id, 0) for course_id in offering.member_course_ids)
        for offering in offerings
    }
    compiled = compile_constraints(data)
    teacher_ids = {teacher.id for teacher in data.teachers}
    eligible = {
        offering.id: _eligible_teachers(offering, compiled, teacher_ids)
        for offering in offerings
    }
    capacities = _remaining_capacities(data, teacher_capacity_adjustments)

    candidates = {}
    options = {}
    for offering in offerings:
        generated = _generate_candidates(predicted[offering.id], offering)
        if not generated:
            return {
                "status": "infeasible",
                "detail": "A combined offering exceeds its one shared section.",
                "diagnostics": [{
                    "code": "combined_offering_over_capacity",
                    "offering_id": offering.id,
                    "member_course_codes": list(offering.member_course_codes),
                    "predicted_enrollment": predicted[offering.id],
                    "hard_max": offering.hard_max,
                }],
            }
        candidates[offering.id] = _apply_constraints(
            generated,
            constraints.get(offering.id, {}),
        )
        if not candidates[offering.id]:
            return {
                "status": "infeasible",
                "detail": "A counselor constraint has no legal staffing candidate.",
                "diagnostics": [{
                    "code": "offering_constraint_no_candidate",
                    "offering_id": offering.id,
                    "available_counts": [candidate.count for candidate in generated],
                }],
            }
        options[offering.id] = tuple(
            option
            for option in _semester_options(offering, candidates[offering.id])
            if (
                "semester_1_count" not in constraints.get(offering.id, {})
                or option[1] == constraints[offering.id]["semester_1_count"]
            )
            and (
                "semester_2_count" not in constraints.get(offering.id, {})
                or option[2] == constraints[offering.id]["semester_2_count"]
            )
        )
        if not options[offering.id]:
            return {
                "status": "infeasible",
                "detail": "A counselor semester split has no legal staffing candidate.",
                "diagnostics": [{
                    "code": "offering_semester_constraint_no_candidate",
                    "offering_id": offering.id,
                }],
            }

    model = cp_model.CpModel()
    choices = {}
    counts = {}
    for offering in offerings:
        variables = [
            model.NewBoolVar(f"staffing_{offering.id}_{one}_{two}_{index}")
            for index, (_, one, two) in enumerate(options[offering.id])
        ]
        model.AddExactlyOne(variables)
        choices[offering.id] = variables
        for semester in (1, 2):
            counts[offering.id, semester] = sum(
                variable * option[semester]
                for variable, option in zip(variables, options[offering.id])
            )

    loads = {}
    for teacher_id in sorted(teacher_ids):
        for semester in (1, 2):
            teacher_loads = []
            for offering in offerings:
                if teacher_id not in eligible[offering.id]:
                    continue
                maximum = max(option[semester] for option in options[offering.id])
                load = model.NewIntVar(
                    0,
                    maximum,
                    f"staffing_load_{teacher_id}_{offering.id}_{semester}",
                )
                loads[teacher_id, offering.id, semester] = load
                teacher_loads.append(load)
            model.Add(sum(teacher_loads) <= capacities[teacher_id, semester])
    for offering in offerings:
        for semester in (1, 2):
            model.Add(sum(
                loads[teacher_id, offering.id, semester]
                for teacher_id in teacher_ids
                if (teacher_id, offering.id, semester) in loads
            ) == counts[offering.id, semester])

    total = sum(counts.values())
    linked_total = None
    if approved_budget_counts is not None:
        linked_total = sum(approved_budget_counts.values())
        model.Add(total == linked_total)

    objectives = [
        sum(
            variable * option[0].unmet_students
            for offering in offerings if offering.priority_tier == tier
            for variable, option in zip(choices[offering.id], options[offering.id])
        )
        for tier in (1, 2, 3, 4)
    ]
    objectives.extend([
        sum(
            variable * option[0].soft_violation
            for offering in offerings
            for variable, option in zip(choices[offering.id], options[offering.id])
        ),
        sum(
            variable * option[0].target_distance
            for offering in offerings
            for variable, option in zip(choices[offering.id], options[offering.id])
        ),
    ])
    if approved_budget_counts is not None:
        objectives.append(sum(
            variable * abs(option[0].count - approved_budget_counts[offering.id])
            for offering in offerings
            for variable, option in zip(choices[offering.id], options[offering.id])
        ))
    else:
        # Once demand and class-size quality are fixed, avoid consuming unused
        # roster capacity merely because it exists.
        objectives.append(total)

    solver = _solve_model(model, objectives)
    if solver is None:
        minimum_required = linked_total if linked_total is not None else sum(
            min(candidate.count for candidate in candidates[offering.id])
            for offering in offerings
        )
        return {
            "status": "infeasible",
            "detail": "No section-count plan fits the ready qualified teacher roster.",
            "diagnostics": _infeasible_diagnostics(
                offerings, options, eligible, capacities, minimum_required
            ),
        }

    required_rooms = compiled.required_room_types_by_course
    results = []
    for offering in sorted(offerings, key=lambda item: (item.member_course_codes, item.id)):
        candidate, semester_one, semester_two = next(
            option
            for variable, option in zip(choices[offering.id], options[offering.id])
            if solver.Value(variable)
        )
        annual = semester_one + semester_two
        budget_count = (
            approved_budget_counts.get(offering.id)
            if approved_budget_counts is not None
            else None
        )
        warnings = []
        if candidate.below_hard_min_review_required:
            warnings.append("below_hard_min_review_required")
        if candidate.unmet_students:
            warnings.append("unmet_demand_within_staffing_plan")
        if budget_count is not None and annual != budget_count:
            warnings.append("staffing_reallocated_approved_budget")
        results.append({
            "offering_id": offering.id,
            "member_course_ids": list(offering.member_course_ids),
            "member_course_codes": list(offering.member_course_codes),
            "is_combined": offering.is_combined,
            "predicted_enrollment": predicted[offering.id],
            "annual_count": annual,
            "semester_1_count": semester_one,
            "semester_2_count": semester_two,
            "unmet_demand": candidate.unmet_students,
            "eligible_teacher_count": len(eligible[offering.id]),
            # Combined groups inherit the union of member room needs.  Placement
            # later must find one room satisfying the complete set.
            "required_room_types": sorted({
                room_type
                for course_id in offering.member_course_ids
                for room_type in required_rooms[course_id]
            }),
            "approved_budget_annual_count": budget_count,
            "budget_count_difference": annual - budget_count if budget_count is not None else None,
            "capacity_policy": {
                "hard_min": offering.hard_min,
                "soft_min": offering.soft_min,
                "target": offering.target_capacity,
                "soft_max": offering.soft_max,
                "hard_max": offering.hard_max,
            },
            "warnings": warnings,
        })
    planned = sum(item["annual_count"] for item in results)
    return {
        "status": "complete",
        "offerings": results,
        "linked_budget_total": linked_total,
        "planned_sections": planned,
        "available_teacher_sections": sum(capacities.values()),
        "unused_teacher_sections": sum(capacities.values()) - planned,
        "diagnostics": [],
    }
