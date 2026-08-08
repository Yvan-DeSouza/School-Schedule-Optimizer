"""Staffing-aware annual and semester section-count planning.

This module is deliberately pure Python: it knows nothing about Django models,
HTTP requests, or database transactions.  The adapter converts the school's
current records into ``SchedulingInputDTO`` objects, and this module returns a
JSON-ready recommendation.  A recommendation is not a timetable and does not
create sections or assign named teachers.

The solve is intentionally staged so counselors can understand the result:

1. Generate legal, inspectable section-count candidates for each course.
2. Choose a demand-only annual baseline without staffing constraints.
3. Re-optimize annual counts against eligible teacher load capacity.
4. Split the annual plan across semesters using the same capacity pool.

The teacher/course load variables below are feasibility witnesses only.  They
prove that some allocation of the proposed section load exists; their values
must never be interpreted as actual teacher assignments.
"""

from dataclasses import dataclass
from math import ceil
from typing import Iterable

from ortools.sat.python import cp_model

from .constraint_compiler import compile_constraints
from .demand_analyzer import analyze_demand
from .dto import SchedulingInputDTO


SEMESTER_1_ONLY = "semester_1_only"
SEMESTER_2_ONLY = "semester_2_only"
SEMESTER_EITHER = "either_semester"


@dataclass(frozen=True)
class SectionCountCandidate:
    """One precomputed annual choice for a course.

    Keeping these metrics on the candidate makes the CP-SAT model small and
    keeps policy calculations testable without inspecting solver internals.
    """

    count: int
    # Students served and unmet are kept explicit so shortage is an honest,
    # selectable outcome rather than an infeasible or fabricated offering.
    served_students: int
    unmet_students: int
    # Soft-min/soft-max and target measures drive later lexicographic passes.
    soft_violation: int
    target_distance: int
    above_target: int
    # Small courses are offered provisionally, but the result must tell a
    # counselor that the hard-minimum exception needs human review.
    below_hard_min_review_required: bool = False


def generate_section_count_candidates(predicted_enrollment: float, course) -> tuple[SectionCountCandidate, ...]:
    """Create testable annual choices before compiling a solver model."""
    # Demand analysis can return a float after applying a historical conversion
    # ratio.  One fractional predicted student still requires a full seat.
    demand = max(0, ceil(predicted_enrollment - 1e-12))
    if demand == 0:
        # Never invent an offering simply because teacher capacity is unused.
        return (SectionCountCandidate(0, 0, 0, 0, 0, 0),)

    # Zero is always a controlled-shortage option.  Without it, a staffing gap
    # would make the whole model infeasible and hide which demand went unmet.
    candidates = [SectionCountCandidate(0, 0, demand, 0, 0, 0)]
    if demand < course.hard_min:
        # A single below-minimum class is retained for counselor review.  The
        # engine does not unilaterally cancel or merge the course.
        counts = (1,)
    else:
        # Every count in this interval can fit all predicted students while
        # respecting both hard bounds.
        minimum = ceil(demand / course.hard_max)
        maximum = demand // course.hard_min
        counts = range(minimum, maximum + 1)
    for count in counts:
        # Violations measure total seats outside the preferred soft band.  Hard
        # bounds were already enforced by candidate generation above.
        soft_violation = max(0, count * course.soft_min - demand) + max(0, demand - count * course.soft_max)
        target_distance = abs(demand - count * course.target_capacity)
        candidates.append(SectionCountCandidate(
            count=count,
            served_students=demand,
            unmet_students=0,
            soft_violation=soft_violation,
            target_distance=target_distance,
            above_target=max(0, demand - count * course.target_capacity),
            below_hard_min_review_required=demand < course.hard_min,
        ))
    return tuple(candidates)


def _solve_lexicographically(model, objectives):
    """Optimize objectives in strict priority order without giant weights."""

    solver = cp_model.CpSolver()
    # A single worker plus a fixed seed keeps tests and equal-quality choices
    # reproducible.  Production tuning can change this only with determinism
    # implications understood.
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = 0
    for objective in objectives:
        # Solve one priority, freeze its best value, then move to the next.  This
        # guarantees (for example) that elective class-size quality can never
        # trade away even one student of Tier 1 demand.
        model.Minimize(objective)
        status = solver.Solve(model)
        if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            return None, status
        model.Add(objective == solver.Value(objective))
    # Re-solve after freezing the final objective so the returned assignment
    # satisfies every equality added during the objective passes.
    status = solver.Solve(model)
    return solver if status in (cp_model.OPTIMAL, cp_model.FEASIBLE) else None, status


def _apply_course_constraints(candidates, constraints):
    """Filter generated choices using counselor-supplied hard scenario bounds."""

    constrained = []
    for candidate in candidates:
        exact = constraints.get("exact_sections")
        minimum = constraints.get("min_sections")
        maximum = constraints.get("max_sections")
        if exact is not None and candidate.count != exact:
            continue
        if minimum is not None and candidate.count < minimum:
            continue
        if maximum is not None and candidate.count > maximum:
            continue
        constrained.append(candidate)
    return tuple(constrained)


def _remaining_capacities(data: SchedulingInputDTO, adjustments):
    """Build effective teacher/semester load limits for this scenario."""

    teachers = {teacher.id: teacher for teacher in data.teachers}
    # An explicit planning record overrides the teacher profile's general
    # maximum.  Reserved load already includes committed/locked sections loaded
    # by the Django adapter.
    explicit = {(item.teacher_id, item.semester): item for item in data.teacher_planning_capacities}
    values = {}
    for teacher in teachers.values():
        for semester in (1, 2):
            item = explicit.get((teacher.id, semester))
            maximum = item.maximum_sections if item else teacher.max_courses_per_semester
            reserved = item.reserved_sections if item else 0
            values[teacher.id, semester] = max(0, maximum - reserved)
    # What-if adjustments are reductions only.  They never mutate qualifications
    # and never change the persisted planning-capacity record.
    for adjustment in adjustments:
        key = (adjustment["teacher_id"], adjustment["semester"])
        if key not in values:
            raise ValueError("Teacher capacity adjustment references an unknown teacher or semester.")
        if adjustment.get("excluded"):
            values[key] = 0
        else:
            values[key] = max(0, values[key] - int(adjustment.get("reduce_by", 0)))
    return values


def _course_demand(data: SchedulingInputDTO):
    """Return predicted enrollment plus provenance for every catalog course."""

    summaries = {summary.course_id: summary for summary in analyze_demand(data).summaries}
    values = {}
    for course in data.courses:
        summary = summaries[course.id]
        # New courses deliberately fall back to a neutral 1:1 conversion ratio.
        ratio = 1.0 if summary.historical_conversion_ratio is None else summary.historical_conversion_ratio
        # The legacy staffing planner operates on primary demand. Backup choices
        # are promoted by the explicit cancellation policy workflow, never
        # counted speculatively in every run.
        values[course.id] = (summary.primary_requests * ratio, summary, ratio)
    return values


def _course_diagnostic(course, code, message, **details):
    """Create a stable counselor-facing diagnostic envelope."""

    return {
        "code": code,
        "severity": details.pop("severity", "error"),
        "course_id": course.id,
        "course_code": course.course_code,
        "priority_tier": course.priority_tier,
        "message": message,
        **details,
    }


def _solver_infeasibility_diagnostics(
    data,
    candidates_by_course,
    capacities,
    compiled,
    *,
    phase,
):
    """Explain common hard-capacity causes after CP-SAT proves infeasibility.

    CP-SAT tells us that the combined model has no solution, but this helper
    translates obvious capacity and eligibility bottlenecks into useful domain
    language.  The final generic diagnostic covers shared-pool conflicts that
    cannot be attributed safely to a single course.
    """

    diagnostics = []
    # The lowest surviving candidate is the minimum forced by counselor
    # constraints.  Unconstrained courses retain zero and therefore do not make
    # an otherwise optional scenario look artificially mandatory.
    minimum_required_by_course = {
        course.id: min(candidate.count for candidate in candidates_by_course[course.id])
        for course in data.courses
    }
    total_required = sum(minimum_required_by_course.values())
    total_available = sum(capacities.values())
    if total_required > total_available:
        # This quick aggregate proof is independent of qualifications: there are
        # simply fewer total load slots than forced sections.
        diagnostics.append({
            "code": "total_staffing_capacity_shortfall",
            "severity": "error",
            "phase": phase,
            "required_sections": total_required,
            "available_sections": total_available,
            "shortfall_sections": total_required - total_available,
            "message": (
                f"The scenario requires at least {total_required} sections but only "
                f"{total_available} teacher section-load slots are available."
            ),
        })

    for course in data.courses:
        required = minimum_required_by_course[course.id]
        if required == 0:
            continue
        eligible = compiled.qualified_teacher_ids_by_course[course.id]
        if not eligible:
            # This is especially important for Grade 11-12 courses, whose
            # normalized qualification requirements fail closed.
            diagnostics.append(_course_diagnostic(
                course,
                "no_eligible_teachers",
                f"{course.course_code} has no legally eligible teacher in the planning pool.",
                phase=phase,
                required_sections=required,
                eligible_teacher_count=0,
            ))
            continue
        eligible_annual_capacity = sum(
            capacities[teacher_id, semester]
            for teacher_id in eligible
            for semester in (1, 2)
        )
        if required > eligible_annual_capacity:
            # Capacity is summed only across teachers legally eligible for this
            # course, not across the entire staff.
            diagnostics.append(_course_diagnostic(
                course,
                "course_staffing_capacity_shortfall",
                (
                    f"{course.course_code} requires at least {required} sections but its "
                    f"eligible teachers have capacity for {eligible_annual_capacity}."
                ),
                phase=phase,
                required_sections=required,
                eligible_capacity_sections=eligible_annual_capacity,
                shortfall_sections=required - eligible_annual_capacity,
                eligible_teacher_count=len(eligible),
            ))

        restricted_semester = None
        if course.allowed_semester == SEMESTER_1_ONLY:
            restricted_semester = 1
        elif course.allowed_semester == SEMESTER_2_ONLY:
            restricted_semester = 2
        if phase == "semester" and restricted_semester is not None:
            # Annual capacity can look sufficient even when every legal slot is
            # in the wrong semester for a restricted course.
            eligible_semester_capacity = sum(
                capacities[teacher_id, restricted_semester]
                for teacher_id in eligible
            )
            if required > eligible_semester_capacity:
                diagnostics.append(_course_diagnostic(
                    course,
                    "semester_staffing_capacity_shortfall",
                    (
                        f"{course.course_code} requires at least {required} Semester "
                        f"{restricted_semester} sections but eligible teachers have capacity "
                        f"for {eligible_semester_capacity}."
                    ),
                    phase=phase,
                    semester=restricted_semester,
                    required_sections=required,
                    eligible_capacity_sections=eligible_semester_capacity,
                    shortfall_sections=required - eligible_semester_capacity,
                    eligible_teacher_count=len(eligible),
                ))
    if not diagnostics:
        # Multiple courses may individually fit but compete for the same small
        # qualified pool.  Avoid claiming one course is the cause when only the
        # combination is provably impossible.
        diagnostics.append({
            "code": "combined_staffing_constraints_infeasible",
            "severity": "error",
            "phase": phase,
            "message": (
                "The selected courses compete for the same eligible teacher capacity, "
                "so their required section counts cannot be staffed together."
            ),
        })
    return diagnostics


def _build_annual_model(data, candidates_by_course, capacities):
    """Compile the annual staffing-feasibility model and ordered objectives."""

    # Qualification matching is centralized in the existing compiler.  The
    # planner consumes the compiled eligible ID sets and does not duplicate any
    # legal/credential rules here.
    compiled = compile_constraints(data)
    model = cp_model.CpModel()
    choices = {}
    selected_count = {}
    for course in data.courses:
        candidates = candidates_by_course[course.id]
        # Exactly one precomputed candidate is chosen for every course, including
        # zero-demand and controlled-shortage choices.
        variables = [model.NewBoolVar(f"annual_{course.id}_{candidate.count}_{index}") for index, candidate in enumerate(candidates)]
        model.AddExactlyOne(variables)
        choices[course.id] = variables
        # Boolean-choice multiplication turns the selected candidate count into
        # a linear expression used by the staffing witness constraints.
        selected_count[course.id] = sum(variable * candidate.count for variable, candidate in zip(variables, candidates))

    loads = {}
    for teacher in data.teachers:
        annual_capacity = capacities[teacher.id, 1] + capacities[teacher.id, 2]
        teacher_loads = []
        for course in data.courses:
            if teacher.id not in compiled.qualified_teacher_ids_by_course[course.id]:
                # Ineligible pairs have no variable at all, which is stronger and
                # smaller than creating a variable and constraining it to zero.
                continue
            # This anonymous load proves that an eligible teacher pool can cover
            # the chosen counts.  It is discarded after solving.
            load = model.NewIntVar(0, max(candidate.count for candidate in candidates_by_course[course.id]), f"annual_load_{teacher.id}_{course.id}")
            loads[teacher.id, course.id] = load
            teacher_loads.append(load)
        model.Add(sum(teacher_loads) <= annual_capacity)
    for course in data.courses:
        # Every selected section must be covered by exactly one unit of eligible
        # anonymous teacher load.
        model.Add(sum(loads[teacher.id, course.id] for teacher in data.teachers if (teacher.id, course.id) in loads) == selected_count[course.id])

    objectives = []
    for tier in (1, 2, 3, 4):
        # Separate expressions become separate lexicographic passes, preserving
        # the administrator-configured priority hierarchy exactly.
        objectives.append(sum(
            variable * candidate.unmet_students
            for course in data.courses if course.priority_tier == tier
            for variable, candidate in zip(choices[course.id], candidates_by_course[course.id])
        ))
    objectives.extend([
        # After demand is protected: prefer the soft class-size band, then the
        # target, then smaller classes when target distance ties.
        sum(variable * candidate.soft_violation for course in data.courses for variable, candidate in zip(choices[course.id], candidates_by_course[course.id])),
        sum(variable * candidate.target_distance for course in data.courses for variable, candidate in zip(choices[course.id], candidates_by_course[course.id])),
        sum(variable * candidate.above_target for course in data.courses for variable, candidate in zip(choices[course.id], candidates_by_course[course.id])),
    ])
    return model, choices, loads, objectives, compiled


def _semester_options(course, candidates):
    """Expand each annual candidate into every legal Semester 1/2 split."""

    options = []
    for candidate in candidates:
        for semester_one in range(candidate.count + 1):
            semester_two = candidate.count - semester_one
            # Course availability is a hard catalog rule, not a preference.
            if course.allowed_semester == SEMESTER_1_ONLY and semester_two:
                continue
            if course.allowed_semester == SEMESTER_2_ONLY and semester_one:
                continue
            options.append((candidate, semester_one, semester_two))
    return options


def _build_semester_model(data, candidates_by_course, capacities, annual_counts):
    """Compile a semester-aware staffing model around the annual recommendation."""

    compiled = compile_constraints(data)
    model = cp_model.CpModel()
    options_by_course, choices, counts = {}, {}, {}
    for course in data.courses:
        options = _semester_options(course, candidates_by_course[course.id])
        # Each option contains both the annual candidate metrics and its concrete
        # semester split, allowing one Boolean family to drive all objectives.
        variables = [model.NewBoolVar(f"semester_{course.id}_{one}_{two}_{index}") for index, (_, one, two) in enumerate(options)]
        model.AddExactlyOne(variables)
        options_by_course[course.id], choices[course.id] = options, variables
        counts[course.id, 1] = sum(variable * option[1] for variable, option in zip(variables, options))
        counts[course.id, 2] = sum(variable * option[2] for variable, option in zip(variables, options))

    loads = {}
    for teacher in data.teachers:
        for semester in (1, 2):
            teacher_loads = []
            for course in data.courses:
                if teacher.id not in compiled.qualified_teacher_ids_by_course[course.id]:
                    continue
                # Semester witnesses are separate because a teacher can have
                # different maximum/reserved load in each term.
                max_count = max(option[semester] for option in options_by_course[course.id])
                load = model.NewIntVar(0, max_count, f"semester_load_{teacher.id}_{course.id}_{semester}")
                loads[teacher.id, course.id, semester] = load
                teacher_loads.append(load)
            model.Add(sum(teacher_loads) <= capacities[teacher.id, semester])
    for course in data.courses:
        for semester in (1, 2):
            # The anonymous eligible-teacher load must cover the selected count
            # independently in each semester.
            model.Add(sum(loads[teacher.id, course.id, semester] for teacher in data.teachers if (teacher.id, course.id, semester) in loads) == counts[course.id, semester])

    objectives = []
    for tier in (1, 2, 3, 4):
        objectives.append(sum(
            variable * option[0].unmet_students
            for course in data.courses if course.priority_tier == tier
            for variable, option in zip(choices[course.id], options_by_course[course.id])
        ))
    objectives.extend([
        sum(variable * option[0].soft_violation for course in data.courses for variable, option in zip(choices[course.id], options_by_course[course.id])),
        sum(variable * option[0].target_distance for course in data.courses for variable, option in zip(choices[course.id], options_by_course[course.id])),
        sum(variable * option[0].above_target for course in data.courses for variable, option in zip(choices[course.id], options_by_course[course.id])),
        # Only after demand and class-size quality are fixed do we minimize
        # changes from the annual staffing plan.  Semester feasibility is allowed
        # to change that plan when necessary.
        sum(variable * abs(option[0].count - annual_counts[course.id]) for course in data.courses for variable, option in zip(choices[course.id], options_by_course[course.id])),
    ])
    return model, choices, options_by_course, loads, objectives, compiled


def plan_section_counts(data: SchedulingInputDTO, *, course_constraints: Iterable[dict] = (), teacher_capacity_adjustments: Iterable[dict] = ()) -> dict:
    """Return a JSON-ready, explainable annual + semester section plan."""
    # Convert the API's list form into an indexed scenario overlay.  The
    # serializer prevents duplicates; this guard also protects direct callers.
    constraints_by_course = {item["course_id"]: item for item in course_constraints}
    course_ids = {course.id for course in data.courses}
    if not set(constraints_by_course) <= course_ids:
        raise ValueError("A course constraint references an unknown course.")
    capacities = _remaining_capacities(data, teacher_capacity_adjustments)
    demand = _course_demand(data)
    # Compile once up front so candidate-level failures can still report teacher
    # eligibility even though no CP-SAT model will be built.
    compiled_for_diagnostics = compile_constraints(data)
    candidates_by_course = {}
    for course in data.courses:
        # Candidate generation is deliberately visible and deterministic.  A
        # counselor constraint filters choices; it never changes their meaning.
        generated_candidates = generate_section_count_candidates(demand[course.id][0], course)
        candidates = _apply_course_constraints(generated_candidates, constraints_by_course.get(course.id, {}))
        if not candidates:
            # This is a scenario-definition failure, such as fixing a five-student
            # course at three sections when only zero or one are meaningful.
            constraint = constraints_by_course[course.id]
            diagnostic = _course_diagnostic(
                course,
                "course_constraint_no_candidate",
                f"No section-count candidate satisfies the scenario for {course.course_code}.",
                phase="candidate_generation",
                requested_constraint=constraint,
                available_candidate_counts=[candidate.count for candidate in generated_candidates],
                eligible_teacher_count=len(compiled_for_diagnostics.qualified_teacher_ids_by_course[course.id]),
            )
            return {
                "status": "infeasible",
                "detail": diagnostic["message"],
                "diagnostics": [diagnostic],
            }
        candidates_by_course[course.id] = candidates

    # The baseline answers "what would demand and class-size policy prefer if
    # staffing were unlimited?"  It is intentionally not a CP-SAT solve.
    baseline = {}
    for course in data.courses:
        candidates = candidates_by_course[course.id]
        baseline[course.id] = min(candidates, key=lambda item: (item.unmet_students, item.soft_violation, item.target_distance, item.above_target, -item.count)).count

    # Stage 1: prove that the annual counts fit the total eligible teacher pool.
    model, choices, loads, objectives, compiled = _build_annual_model(data, candidates_by_course, capacities)
    solver, status = _solve_lexicographically(model, objectives)
    if solver is None:
        return {
            "status": "infeasible",
            "detail": "No annual staffing-feasible plan exists for the supplied scenario.",
            "diagnostics": _solver_infeasibility_diagnostics(
                data,
                candidates_by_course,
                capacities,
                compiled,
                phase="annual",
            ),
        }
    # Extract only the chosen counts.  Anonymous load values are not persisted as
    # assignments because many equally valid witness allocations may exist.
    annual = {course.id: sum(candidate.count for variable, candidate in zip(choices[course.id], candidates_by_course[course.id]) if solver.Value(variable)) for course in data.courses}

    # Stage 2: respect distinct Semester 1/2 capacities and catalog restrictions.
    model, choices, options, loads, objectives, compiled = _build_semester_model(data, candidates_by_course, capacities, annual)
    solver, status = _solve_lexicographically(model, objectives)
    if solver is None:
        return {
            "status": "infeasible",
            "detail": "The annual plan cannot be split into staffing-feasible semesters.",
            "diagnostics": _solver_infeasibility_diagnostics(
                data,
                candidates_by_course,
                capacities,
                compiled,
                phase="semester",
            ),
        }

    course_results = []
    diagnostics = []
    for course in sorted(data.courses, key=lambda item: (item.course_code, item.id)):
        # Exactly-one constraints guarantee this lookup succeeds after a feasible
        # solve.  Keeping the selected candidate intact preserves all metrics.
        selected = next(option for variable, option in zip(choices[course.id], options[course.id]) if solver.Value(variable))
        candidate, semester_one, semester_two = selected
        predicted, summary, ratio = demand[course.id]
        warnings = []
        reasons = []
        if candidate.below_hard_min_review_required:
            warnings.append("below_hard_min_review_required")
            reasons.append("Predicted demand is below the minimum viable class size; counselor review is required.")
            diagnostics.append(_course_diagnostic(
                course,
                "below_hard_min_review_required",
                f"{course.course_code} is below its hard minimum class size and requires counselor review.",
                severity="warning",
                phase="demand",
                predicted_enrollment=predicted,
                hard_min=course.hard_min,
            ))
        if candidate.unmet_students:
            # Distinguish a legal-eligibility gap from competition for a shared
            # eligible staffing pool; the counselor response differs materially.
            reasons.append("No legally staffable section capacity remained for this course.")
            eligible_teacher_count = len(compiled.qualified_teacher_ids_by_course[course.id])
            if eligible_teacher_count == 0:
                diagnostic_code = "no_eligible_teachers"
                diagnostic_message = f"{course.course_code} has demand but no legally eligible teacher."
            else:
                diagnostic_code = "unmet_demand_after_staffing"
                diagnostic_message = (
                    f"{course.course_code} has {candidate.unmet_students} students of unmet demand "
                    "after applying staffing capacity and course priorities."
                )
            diagnostics.append(_course_diagnostic(
                course,
                diagnostic_code,
                diagnostic_message,
                severity="warning",
                phase="staffing",
                unmet_demand=candidate.unmet_students,
                eligible_teacher_count=eligible_teacher_count,
            ))
        if baseline[course.id] != annual[course.id]:
            # Comparing stages is more informative than presenting the final
            # number as if demand alone selected it.
            diagnostics.append(_course_diagnostic(
                course,
                "staffing_changed_demand_plan",
                (
                    f"Staffing feasibility changed {course.course_code} from "
                    f"{baseline[course.id]} to {annual[course.id]} annual sections."
                ),
                severity="warning",
                phase="annual",
                demand_baseline_annual_count=baseline[course.id],
                staffing_feasible_annual_count=annual[course.id],
            ))
        if annual[course.id] != candidate.count:
            reasons.append("Semester staffing feasibility changed the annual staffing plan.")
            diagnostics.append(_course_diagnostic(
                course,
                "semester_capacity_changed_annual_plan",
                (
                    f"Semester staffing capacity changed {course.course_code} from "
                    f"{annual[course.id]} to {candidate.count} annual sections."
                ),
                severity="warning",
                phase="semester",
                annual_count_before_semester_split=annual[course.id],
                final_annual_count=candidate.count,
            ))
        if course.id in constraints_by_course:
            reasons.append("A counselor scenario constraint was applied.")
        # The result deliberately repeats policy/provenance fields.  Planning
        # runs snapshot this object so later catalog changes cannot rewrite why
        # an old recommendation was made.
        course_results.append({
            "course_id": course.id, "course_code": course.course_code, "predicted_enrollment": predicted,
            "current_requests": summary.primary_requests, "conversion_ratio": ratio,
            "capacity_profile_id": course.capacity_profile_id, "priority_tier": course.priority_tier,
            "priority_profile_id": course.priority_profile_id,
            "allowed_semester": course.allowed_semester, "demand_baseline_annual_count": baseline[course.id],
            "staffing_feasible_annual_count": annual[course.id], "semester_1_count": semester_one,
            "semester_2_count": semester_two, "unmet_demand": candidate.unmet_students,
            "soft_violation": candidate.soft_violation, "target_distance": candidate.target_distance,
            "capacity_policy": {
                "hard_min": course.hard_min, "soft_min": course.soft_min,
                "target": course.target_capacity, "soft_max": course.soft_max, "hard_max": course.hard_max,
            },
            "expected_enrollment_per_section": (predicted / candidate.count) if candidate.count else 0,
            "warnings": warnings, "reasons": reasons,
        })
    # Aggregate the semester witness solely for capacity/slack reporting.  Do not
    # expose this by teacher as an assignment recommendation.
    used_by_teacher_semester = {(teacher_id, semester): sum(solver.Value(load) for (tid, _, sem), load in loads.items() if tid == teacher_id and sem == semester) for teacher_id, semester in capacities}
    return {
        "status": "complete", "courses": course_results, "diagnostics": diagnostics,
        "capacity_summary": {
            "available_sections": sum(capacities.values()),
            "planned_sections": sum(item["semester_1_count"] + item["semester_2_count"] for item in course_results),
            "unused_sections": sum(capacities[key] - used_by_teacher_semester[key] for key in capacities),
        },
        "qualification_bottlenecks": [
            # Counts help counselors spot thin pools without revealing or
            # committing a named-teacher allocation.
            {"course_id": course.id, "course_code": course.course_code, "eligible_teacher_count": len(compiled.qualified_teacher_ids_by_course[course.id])}
            for course in data.courses
        ],
    }
