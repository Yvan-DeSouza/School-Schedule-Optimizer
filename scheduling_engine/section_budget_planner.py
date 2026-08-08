"""Teacher-independent physical-section budgeting and backup-demand resolution."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from math import ceil
from typing import Iterable

from ortools.sat.python import cp_model

from .demand_analyzer import analyze_demand
from .dto import CourseRequestDTO, PlanningOfferingDTO, SchedulingInputDTO


BUDGET_EXACT = "exact"
BUDGET_CEILING = "ceiling"
BACKUP_PROMOTE = "promote_available"
BACKUP_IGNORE = "ignore"
SEMESTER_1_ONLY = "semester_1_only"
SEMESTER_2_ONLY = "semester_2_only"


@dataclass(frozen=True)
class BudgetCandidate:
    """One meaningful physical-section count for an offering."""

    count: int
    served_students: int
    unmet_students: int
    soft_violation: int
    target_distance: int
    below_hard_min_review_required: bool = False


def _synthetic_offerings(data):
    """Keep the engine usable for existing standalone fixtures and callers."""

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


def _offerings(data):
    return data.planning_offerings or _synthetic_offerings(data)


def _course_ratios(data):
    return {
        summary.course_id: (
            1.0
            if summary.historical_conversion_ratio is None
            else summary.historical_conversion_ratio
        )
        for summary in analyze_demand(data).summaries
    }


def _predicted_by_course(data, effective_requests):
    counts = Counter(request.course_id for request in effective_requests)
    ratios = _course_ratios(data)
    return {
        course.id: counts[course.id] * ratios.get(course.id, 1.0)
        for course in data.courses
    }


def _generate_candidates(demand, offering):
    rounded_demand = max(0, ceil(demand - 1e-12))
    if rounded_demand == 0:
        return (BudgetCandidate(0, 0, 0, 0, 0),)
    if offering.is_combined:
        if rounded_demand > offering.hard_max:
            return ()
        counts = (1,)
    else:
        # A positive active offering is never silently cancelled. Candidate
        # counts below full-demand capacity expose honest unmet demand when the
        # school-wide budget is tight.
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


def _apply_constraints(candidates, constraint):
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


def _solve_model(model, objectives):
    solver = cp_model.CpSolver()
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = 0
    for objective in objectives:
        model.Minimize(objective)
        status = solver.Solve(model)
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            return None
        model.Add(objective == solver.Value(objective))
    return solver if solver.Solve(model) in (cp_model.OPTIMAL, cp_model.FEASIBLE) else None


def _semester_split(offerings, counts, constraints=None):
    constraints = constraints or {}
    model = cp_model.CpModel()
    choices = {}
    options = {}
    for offering in offerings:
        offering_options = []
        for semester_one in range(counts[offering.id] + 1):
            semester_two = counts[offering.id] - semester_one
            if offering.allowed_semester == SEMESTER_1_ONLY and semester_two:
                continue
            if offering.allowed_semester == SEMESTER_2_ONLY and semester_one:
                continue
            if (
                "semester_1_count" in constraints.get(offering.id, {})
                and semester_one != constraints[offering.id]["semester_1_count"]
            ):
                continue
            if (
                "semester_2_count" in constraints.get(offering.id, {})
                and semester_two != constraints[offering.id]["semester_2_count"]
            ):
                continue
            offering_options.append((semester_one, semester_two))
        if not offering_options:
            return None
        variables = [
            model.NewBoolVar(f"budget_semester_{offering.id}_{one}_{two}")
            for one, two in offering_options
        ]
        model.AddExactlyOne(variables)
        choices[offering.id] = variables
        options[offering.id] = offering_options
    semester_one_total = sum(
        variable * option[0]
        for offering in offerings
        for variable, option in zip(choices[offering.id], options[offering.id])
    )
    semester_two_total = sum(counts.values()) - semester_one_total
    total_imbalance = model.NewIntVar(0, sum(counts.values()), "total_semester_imbalance")
    model.AddAbsEquality(total_imbalance, semester_one_total - semester_two_total)
    offering_spread = sum(
        variable * abs(one - two)
        for offering in offerings
        for variable, (one, two) in zip(choices[offering.id], options[offering.id])
    )
    solver = _solve_model(model, (total_imbalance, offering_spread))
    if solver is None:
        return None
    return {
        offering.id: next(
            option
            for variable, option in zip(choices[offering.id], options[offering.id])
            if solver.Value(variable)
        )
        for offering in offerings
    }


def plan_section_budget(
    data: SchedulingInputDTO,
    *,
    section_budget: int,
    budget_type: str,
    effective_requests: Iterable[CourseRequestDTO] | None = None,
    offering_constraints: Iterable[dict] = (),
):
    """Allocate an exact/ceiling physical budget without staffing evidence."""

    if budget_type not in (BUDGET_EXACT, BUDGET_CEILING):
        raise ValueError("budget_type must be exact or ceiling.")
    if section_budget < 0:
        raise ValueError("section_budget must be non-negative.")
    offerings = _offerings(data)
    offering_ids = {offering.id for offering in offerings}
    constraints = {item["offering_id"]: dict(item) for item in offering_constraints}
    if not set(constraints) <= offering_ids:
        raise ValueError("An offering constraint references an unknown delivery group.")
    requests = tuple(
        effective_requests
        if effective_requests is not None
        else (request for request in data.course_requests if request.is_primary)
    )
    predicted_by_course = _predicted_by_course(data, requests)
    predicted_by_offering = {
        offering.id: sum(
            predicted_by_course.get(course_id, 0)
            for course_id in offering.member_course_ids
        )
        for offering in offerings
    }
    candidates = {}
    for offering in offerings:
        generated = _generate_candidates(predicted_by_offering[offering.id], offering)
        if not generated:
            return {
                "status": "infeasible",
                "detail": "A combined offering does not fit its one shared section.",
                "diagnostics": [{
                    "code": "combined_offering_over_capacity",
                    "offering_id": offering.id,
                    "member_course_codes": list(offering.member_course_codes),
                    "predicted_enrollment": predicted_by_offering[offering.id],
                    "hard_max": offering.hard_max,
                }],
            }
        filtered = _apply_constraints(generated, constraints.get(offering.id, {}))
        if not filtered:
            return {
                "status": "infeasible",
                "detail": "A counselor constraint has no legal budget candidate.",
                "diagnostics": [{
                    "code": "offering_constraint_no_candidate",
                    "offering_id": offering.id,
                    "available_counts": [item.count for item in generated],
                }],
            }
        candidates[offering.id] = filtered

    model = cp_model.CpModel()
    choices = {}
    for offering in offerings:
        variables = [
            model.NewBoolVar(f"budget_{offering.id}_{candidate.count}_{index}")
            for index, candidate in enumerate(candidates[offering.id])
        ]
        model.AddExactlyOne(variables)
        choices[offering.id] = variables
    total = sum(
        variable * candidate.count
        for offering in offerings
        for variable, candidate in zip(choices[offering.id], candidates[offering.id])
    )
    if budget_type == BUDGET_EXACT:
        model.Add(total == section_budget)
    else:
        model.Add(total <= section_budget)
    objectives = [
        sum(
            variable * candidate.unmet_students
            for offering in offerings if offering.priority_tier == tier
            for variable, candidate in zip(choices[offering.id], candidates[offering.id])
        )
        for tier in (1, 2, 3, 4)
    ]
    objectives.extend([
        sum(
            variable * candidate.soft_violation
            for offering in offerings
            for variable, candidate in zip(choices[offering.id], candidates[offering.id])
        ),
        sum(
            variable * candidate.target_distance
            for offering in offerings
            for variable, candidate in zip(choices[offering.id], candidates[offering.id])
        ),
        total,
    ])
    solver = _solve_model(model, objectives)
    if solver is None:
        minimum = sum(min(item.count for item in values) for values in candidates.values())
        maximum = sum(max(item.count for item in values) for values in candidates.values())
        return {
            "status": "infeasible",
            "detail": "The physical-section budget cannot satisfy the active offerings.",
            "diagnostics": [{
                "code": "section_budget_infeasible",
                "budget_type": budget_type,
                "section_budget": section_budget,
                "minimum_meaningful_sections": minimum,
                "maximum_meaningful_sections": maximum,
            }],
        }
    selected = {
        offering.id: next(
            candidate
            for variable, candidate in zip(choices[offering.id], candidates[offering.id])
            if solver.Value(variable)
        )
        for offering in offerings
    }
    split = _semester_split(
        offerings,
        {offering_id: candidate.count for offering_id, candidate in selected.items()},
        constraints,
    )
    if split is None:
        return {
            "status": "infeasible",
            "detail": "The budget counts have no legal suggested semester split.",
            "diagnostics": [{"code": "budget_semester_split_infeasible"}],
        }
    results = []
    for offering in sorted(offerings, key=lambda item: (item.member_course_codes, item.id)):
        candidate = selected[offering.id]
        semester_one, semester_two = split[offering.id]
        warnings = []
        if candidate.below_hard_min_review_required:
            warnings.append("below_hard_min_review_required")
        if candidate.unmet_students:
            warnings.append("unmet_demand_within_section_budget")
        results.append({
            "offering_id": offering.id,
            "member_course_ids": list(offering.member_course_ids),
            "member_course_codes": list(offering.member_course_codes),
            "is_combined": offering.is_combined,
            "predicted_enrollment": predicted_by_offering[offering.id],
            "annual_count": candidate.count,
            "semester_1_count": semester_one,
            "semester_2_count": semester_two,
            "served_students": candidate.served_students,
            "unmet_demand": candidate.unmet_students,
            "capacity_policy": {
                "hard_min": offering.hard_min,
                "soft_min": offering.soft_min,
                "target": offering.target_capacity,
                "soft_max": offering.soft_max,
                "hard_max": offering.hard_max,
            },
            "warnings": warnings,
        })
    used = sum(item["annual_count"] for item in results)
    return {
        "status": "complete",
        "budget_type": budget_type,
        "section_budget": section_budget,
        "used_sections": used,
        "unused_sections": section_budget - used,
        "offerings": results,
        "diagnostics": [],
    }


def resolve_backup_requests(
    data,
    *,
    cancelled_course_ids,
    available_backup_course_ids,
    default_policy,
    course_overrides=(),
):
    """Return effective demand and per-student cancellation outcomes."""

    if default_policy not in (BACKUP_PROMOTE, BACKUP_IGNORE):
        raise ValueError("Unknown backup policy.")
    cancelled = set(cancelled_course_ids)
    available = set(available_backup_course_ids)
    overrides = {item["course_id"]: item["policy"] for item in course_overrides}
    by_student = defaultdict(list)
    for request in data.course_requests:
        by_student[request.student_id].append(request)
    effective = []
    resolutions = []
    for student_id, requests in sorted(by_student.items()):
        primaries = [request for request in requests if request.is_primary]
        backups = [request for request in requests if not request.is_primary]
        if len(backups) > 1:
            raise ValueError("A student may have only one backup request per academic year.")
        cancelled_primaries = [
            request for request in primaries if request.course_id in cancelled
        ]
        effective.extend(
            request for request in primaries if request.course_id not in cancelled
        )
        if not cancelled_primaries:
            continue
        promotion_candidates = sorted(
            (
                request for request in cancelled_primaries
                if overrides.get(request.course_id, default_policy) == BACKUP_PROMOTE
            ),
            key=lambda item: item.course_id,
        )
        backup = backups[0] if backups else None
        promoted = False
        if not promotion_candidates:
            outcome = "backup_ignored"
        elif backup is None:
            outcome = "no_backup"
        elif backup.course_id in cancelled or backup.course_id not in available:
            outcome = "backup_unavailable"
        else:
            effective.append(CourseRequestDTO(
                student_id=backup.student_id,
                course_id=backup.course_id,
                is_primary=True,
                is_mandatory=backup.is_mandatory,
            ))
            promoted = True
            outcome = "backup_promoted"
        unresolved = max(0, len(cancelled_primaries) - (1 if promoted else 0))
        resolutions.append({
            "student_id": student_id,
            "cancelled_course_ids": sorted(
                request.course_id for request in cancelled_primaries
            ),
            "backup_course_id": backup.course_id if backup else None,
            "outcome": outcome,
            "unresolved_course_count": unresolved,
        })
    return tuple(effective), resolutions


def plan_section_budget_with_backups(
    data,
    *,
    section_budget,
    budget_type,
    cancelled_course_ids,
    backup_policy,
    backup_overrides=(),
    offering_constraints=(),
):
    """Two-pass plan: establish available offerings, then promote backups once."""

    preliminary = plan_section_budget(
        data,
        section_budget=section_budget,
        # A ceiling preview identifies independently available offerings even
        # when backup demand is needed to make an exact high budget meaningful.
        budget_type=BUDGET_CEILING,
        offering_constraints=offering_constraints,
    )
    if preliminary["status"] != "complete":
        preliminary["planning_phase"] = "primary_only_preliminary"
        return preliminary
    available_course_ids = {
        course_id
        for item in preliminary["offerings"] if item["annual_count"] > 0
        for course_id in item["member_course_ids"]
    }
    effective, resolutions = resolve_backup_requests(
        data,
        cancelled_course_ids=cancelled_course_ids,
        available_backup_course_ids=available_course_ids,
        default_policy=backup_policy,
        course_overrides=backup_overrides,
    )
    final = plan_section_budget(
        data,
        section_budget=section_budget,
        budget_type=budget_type,
        effective_requests=effective,
        offering_constraints=offering_constraints,
    )
    final["backup_policy"] = backup_policy
    final["backup_overrides"] = list(backup_overrides)
    final["request_resolutions"] = resolutions
    final["affected_student_count"] = len(resolutions)
    return final
