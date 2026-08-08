"""Shared public helpers for section-count planning stages.

The section-budget, staffing-count, and legacy section-count planners all solve
slightly different counselor questions, but they share a few domain primitives:
planning offerings, predicted demand, count candidates, lexicographic solving,
and remaining teacher capacity.  Keeping those helpers here prevents one solver
stage from importing another stage's private implementation details.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from math import ceil
from typing import Iterable

from ortools.sat.python import cp_model

from .demand_analyzer import analyze_demand
from .dto import CourseRequestDTO, PlanningOfferingDTO, SchedulingInputDTO


@dataclass(frozen=True)
class BudgetCandidate:
    """One meaningful physical-section count for a delivery offering."""

    count: int
    served_students: int
    unmet_students: int
    soft_violation: int
    target_distance: int
    below_hard_min_review_required: bool = False


def synthetic_planning_offerings(data: SchedulingInputDTO) -> tuple[PlanningOfferingDTO, ...]:
    """Create one-course offerings for fixtures and legacy DTO callers."""

    return tuple(
        PlanningOfferingDTO(
            id=course.id,
            member_course_ids=(course.id,),
            member_course_codes=(course.course_code,),
            capacity_profile_id=course.capacity_profile_id,
            hard_min=course.hard_min,
            soft_min=course.soft_min,
            target_capacity=course.target_capacity,
            soft_max=course.soft_max,
            hard_max=course.hard_max,
            allowed_semester=course.allowed_semester,
            priority_tier=course.priority_tier,
        )
        for course in data.courses
    )


def planning_offerings(data: SchedulingInputDTO) -> tuple[PlanningOfferingDTO, ...]:
    """Return explicit delivery groups, falling back to standalone course units."""

    return data.planning_offerings or synthetic_planning_offerings(data)


def course_conversion_ratios(data: SchedulingInputDTO) -> dict[int, float]:
    """Return the historical conversion ratio used by every count planner."""

    return {
        summary.course_id: (
            1.0
            if summary.historical_conversion_ratio is None
            else summary.historical_conversion_ratio
        )
        for summary in analyze_demand(data).summaries
    }


def predicted_enrollment_by_course(
    data: SchedulingInputDTO,
    effective_requests: Iterable[CourseRequestDTO],
) -> dict[int, float]:
    """Calculate demand after any workflow-specific request substitution."""

    counts = Counter(request.course_id for request in effective_requests)
    ratios = course_conversion_ratios(data)
    return {
        course.id: counts[course.id] * ratios.get(course.id, 1.0)
        for course in data.courses
    }


def generate_budget_candidates(demand: float, offering: PlanningOfferingDTO) -> tuple[BudgetCandidate, ...]:
    """Create annual count choices for one physical delivery group."""

    rounded_demand = max(0, ceil(demand - 1e-12))
    if rounded_demand == 0:
        return (BudgetCandidate(0, 0, 0, 0, 0),)
    if offering.is_combined:
        # Combined groups are intentionally modeled as one shared physical
        # section.  If that section cannot hold the pooled demand, the planner
        # should explain the conflict instead of silently splitting the group.
        if rounded_demand > offering.hard_max:
            return ()
        counts = (1,)
    else:
        # A positive active offering is never silently cancelled. Candidate
        # counts below full-demand capacity expose honest unmet demand when the
        # school-wide budget or staffing pool is tight.
        maximum_meaningful = max(1, rounded_demand // offering.hard_min)
        counts = range(1, maximum_meaningful + 1)
    candidates = []
    for count in counts:
        served = min(rounded_demand, count * offering.hard_max)
        unmet = rounded_demand - served
        soft_violation = (
            max(0, count * offering.soft_min - rounded_demand)
            + max(0, rounded_demand - count * offering.soft_max)
        )
        candidates.append(BudgetCandidate(
            count=count,
            served_students=served,
            unmet_students=unmet,
            soft_violation=soft_violation,
            target_distance=abs(rounded_demand - count * offering.target_capacity),
            below_hard_min_review_required=rounded_demand < offering.hard_min,
        ))
    return tuple(candidates)


def apply_count_constraints(candidates: Iterable[BudgetCandidate], constraint: dict) -> tuple[BudgetCandidate, ...]:
    """Filter count choices through counselor-supplied hard bounds."""

    values = []
    for candidate in candidates:
        if "exact_sections" in constraint and candidate.count != constraint["exact_sections"]:
            continue
        if candidate.count < constraint.get("min_sections", 0):
            continue
        if candidate.count > constraint.get("max_sections", float("inf")):
            continue
        values.append(candidate)
    return tuple(values)


def solve_with_status_lexicographically(model: cp_model.CpModel, objectives):
    """Optimize objectives in strict priority order and return solver + status."""

    solver = cp_model.CpSolver()
    # Single worker plus fixed seed keeps equal-quality choices reproducible.
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = 0
    for objective in objectives:
        model.Minimize(objective)
        status = solver.Solve(model)
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            return None, status
        # Freeze each priority before solving the next; later quality metrics
        # can never trade away a higher-priority demand outcome.
        model.Add(objective == solver.Value(objective))
    status = solver.Solve(model)
    return solver if status in (cp_model.OPTIMAL, cp_model.FEASIBLE) else None, status


def solve_model_lexicographically(model: cp_model.CpModel, objectives):
    """Optimize ordered objectives and return only the feasible solver."""

    solver, _status = solve_with_status_lexicographically(model, objectives)
    return solver


def remaining_teacher_capacities(data: SchedulingInputDTO, adjustments) -> dict[tuple[int, int], int]:
    """Build effective teacher/semester load limits for a scenario."""

    teachers = {teacher.id: teacher for teacher in data.teachers}
    # Explicit planning rows override profile defaults. Reserved load already
    # includes committed/locked sections assembled by the Django adapter.
    explicit = {
        (item.teacher_id, item.semester): item
        for item in data.teacher_planning_capacities
    }
    values = {}
    for teacher in teachers.values():
        for semester in (1, 2):
            item = explicit.get((teacher.id, semester))
            maximum = item.maximum_sections if item else teacher.max_courses_per_semester
            reserved = item.reserved_sections if item else 0
            values[teacher.id, semester] = max(0, maximum - reserved)
    # Scenario adjustments are temporary reductions only; they never mutate the
    # persisted staffing roster or qualification data.
    for adjustment in adjustments:
        key = (adjustment["teacher_id"], adjustment["semester"])
        if key not in values:
            raise ValueError(
                "Teacher capacity adjustment references an unknown teacher or semester."
            )
        if adjustment.get("excluded"):
            values[key] = 0
        else:
            values[key] = max(0, values[key] - int(adjustment.get("reduce_by", 0)))
    return values
