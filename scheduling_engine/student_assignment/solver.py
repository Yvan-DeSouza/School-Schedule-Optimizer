"""CP-SAT orchestration shared by the two student-assignment stages."""

from __future__ import annotations

from time import monotonic

from ortools.sat.python import cp_model


def _has_solution(status):
    """Return whether CP-SAT produced a complete model assignment."""

    return status in {cp_model.OPTIMAL, cp_model.FEASIBLE}


def outcome_name(status):
    return {
        cp_model.OPTIMAL: "optimal",
        cp_model.FEASIBLE: "feasible",
        cp_model.INFEASIBLE: "infeasible",
        cp_model.MODEL_INVALID: "model_invalid",
        cp_model.UNKNOWN: "unknown",
    }.get(status, "unknown")


def new_solver(time_limit_seconds, *, fix_hints=False, worker_count=1):
    """Build a bounded CP-SAT configuration for the requested stage."""

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_seconds
    solver.parameters.num_search_workers = worker_count
    solver.parameters.random_seed = 0
    solver.parameters.fix_variables_to_their_hinted_value = fix_hints
    return solver


def set_solver_hints(model, solver):
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


def set_assignment_hints(model, assignment_hints):
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


def solve_complete_hard_feasibility_seed(
    hard_model,
    required_decision_groups,
    time_limit_seconds,
    *,
    worker_count=1,
):
    """Find one complete hard-feasible decision pattern from a shared model.

    ``hard_model`` is cloned only after the production model has added every
    assignment, occupancy, capacity, lock, and prerequisite constraint.  The
    clone adds exactly-one requirements for decisions that define a complete
    result; it deliberately adds no soft objective or heuristic schedule.
    CP-SAT therefore remains the authority for the initial complete candidate.
    """

    seed_model = hard_model.Clone()
    source_variable_indexes = set()
    for decision_group in required_decision_groups:
        variables = [
            seed_model.GetIntVarFromProtoIndex(variable.Index())
            for variable in decision_group
        ]
        # An empty complete-required group is itself the exact hard-feasibility
        # finding: no valid seed may pretend that source is fulfilled.
        seed_model.AddExactlyOne(variables)
        source_variable_indexes.update(variable.Index() for variable in decision_group)

    seed_solver = new_solver(time_limit_seconds, worker_count=worker_count)
    status = seed_solver.Solve(seed_model)
    return (
        # An unsuccessful bounded attempt has no candidate to validate. Do
        # not keep its cloned model alive while the full optimization model
        # is being solved; this is a memory-lifetime optimization only.
        seed_model if _has_solution(status) else None,
        seed_solver if _has_solution(status) else None,
        tuple(sorted(source_variable_indexes)),
        status,
    )


def validate_complete_hard_feasibility_seed(
    model,
    seed_model,
    seed_solver,
    source_variable_indexes,
    time_limit_seconds,
    *,
    worker_count=1,
):
    """Validate a seed's source decisions against the full production model.

    The full model contains the same hard prefix plus all derived soft-objective
    variables and constraints.  Fixing just the source decisions lets CP-SAT
    derive those auxiliary values and catches any accidental difference between
    the feasibility prefix and the production model before the seed is used as
    an optimization incumbent.
    """

    if seed_solver is None:
        return None
    model.ClearHints()
    for index in source_variable_indexes:
        model.AddHint(
            model.GetIntVarFromProtoIndex(index),
            seed_solver.Value(seed_model.GetIntVarFromProtoIndex(index)),
        )
    validator = new_solver(
        time_limit_seconds,
        fix_hints=True,
        worker_count=worker_count,
    )
    status = validator.Solve(model)
    if not _has_solution(status):
        model.ClearHints()
        return None
    return validator


def validated_initial_hint_solver(
    model,
    assignment_hints,
    time_limit_seconds,
    *,
    worker_count=1,
):
    """Return a full-model candidate only after CP-SAT validates the hint.

    The constructor below is search guidance, never a second
    scheduler. Lock, capacity, timeslot, group, and prerequisite constraints
    remain authoritative in the CP-SAT model. Fixing the proposed enrollment
    choices for this bounded preparatory solve lets CP-SAT fill every derived
    variable and rejects a hint that is incompatible with any hard rule.
    """

    if not assignment_hints:
        return None
    set_assignment_hints(model, assignment_hints)
    preparer = new_solver(
        min(time_limit_seconds, 5.0),
        fix_hints=True,
        worker_count=worker_count,
    )
    status = preparer.Solve(model)
    if status not in {cp_model.OPTIMAL, cp_model.FEASIBLE}:
        model.ClearHints()
        return None
    return preparer


def solve_lexicographically(
    model,
    objectives,
    time_limit_seconds,
    *,
    initial_assignment_hints=None,
    validated_seed_solver=None,
    worker_count=1,
    total_time_limit_seconds=None,
    pass_facts=None,
    pass_quality_callback=None,
):
    """Optimize ordered objectives while preserving the last valid candidate.

    Each later pass retains equality constraints for every completed objective.
    If bounded search cannot find a new candidate, the prior candidate already
    satisfies those constraints and remains a safe recommendation. Returning
    it is therefore faithful to the existing lexicographic priorities; only
    the uncompleted lower-priority improvement is omitted.
    """

    # A CP-SAT-validated complete hard-feasibility seed is a real incumbent,
    # not a heuristic assignment.  Retaining it here means an objective-tier
    # timeout cannot erase an already complete, legal recommendation.
    previous_solver = validated_seed_solver
    initial_assignment_hints = initial_assignment_hints or {}
    optimization_started = monotonic()
    remaining_objective_count = sum(
        not isinstance(objective, int) for objective in objectives
    )
    for objective_index, objective in enumerate(objectives):
        # Several stages intentionally add an objective slot even when this
        # input has no rows in that tier. Re-solving a constant objective has
        # no scheduling value and previously could discard an earlier result.
        if isinstance(objective, int):
            continue
        starting_value = (
            float(previous_solver.Value(objective))
            if previous_solver is not None else None
        )
        starting_quality = (
            pass_quality_callback(previous_solver)
            if pass_quality_callback is not None and previous_solver is not None
            else None
        )
        pass_time_limit_seconds = time_limit_seconds
        if total_time_limit_seconds is not None:
            remaining_budget = total_time_limit_seconds - (
                monotonic() - optimization_started
            )
            if remaining_budget <= 0:
                if pass_facts is not None:
                    pass_facts.append({
                        "objective_index": objective_index,
                        "status": "unknown",
                        "allocated_time_seconds": 0.0,
                        "wall_time_seconds": 0.0,
                        "starting_objective_value": starting_value,
                        "ending_objective_value": starting_value,
                        "starting_quality": starting_quality,
                        "ending_quality": starting_quality,
                        "incumbent_improved": False,
                        "remaining_budget_seconds": 0.0,
                    })
                return previous_solver, cp_model.UNKNOWN
            # Recompute the share before every tier. A tier that proves its
            # value early leaves its unused budget available to later tiers;
            # no tier can consume more than the remaining global allowance.
            pass_time_limit_seconds = max(
                0.001,
                remaining_budget / remaining_objective_count,
            )
        remaining_objective_count -= 1
        model.Minimize(objective)
        if previous_solver is not None:
            set_solver_hints(model, previous_solver)
        elif initial_assignment_hints:
            prepared_solver = validated_initial_hint_solver(
                model,
                initial_assignment_hints,
                pass_time_limit_seconds,
                worker_count=worker_count,
            )
            if prepared_solver is not None:
                set_solver_hints(model, prepared_solver)
                # The preparatory candidate is safe even if the first bounded
                # optimization pass later reaches UNKNOWN without an incumbent.
                previous_solver = prepared_solver

        solver = new_solver(pass_time_limit_seconds, worker_count=worker_count)
        status = solver.Solve(model)
        solver_has_solution = status in {cp_model.OPTIMAL, cp_model.FEASIBLE}
        ending_value = (
            float(solver.Value(objective))
            if solver_has_solution else starting_value
        )
        ending_quality = (
            pass_quality_callback(solver)
            if pass_quality_callback is not None and solver_has_solution
            else starting_quality
        )
        if pass_facts is not None:
            remaining_after = (
                max(0.0, total_time_limit_seconds - (monotonic() - optimization_started))
                if total_time_limit_seconds is not None else None
            )
            pass_facts.append({
                "objective_index": objective_index,
                "status": outcome_name(status),
                "allocated_time_seconds": pass_time_limit_seconds,
                "wall_time_seconds": solver.WallTime(),
                "starting_objective_value": starting_value,
                "ending_objective_value": ending_value,
                "starting_quality": starting_quality,
                "ending_quality": ending_quality,
                "best_bound": float(solver.BestObjectiveBound()),
                "conflicts": solver.NumConflicts(),
                "branches": solver.NumBranches(),
                "incumbent_improved": (
                    starting_value is not None
                    and ending_value is not None
                    and ending_value < starting_value
                ),
                "remaining_budget_seconds": remaining_after,
            })
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
        solver = new_solver(time_limit_seconds, worker_count=worker_count)
        status = solver.Solve(model)
        return (
            solver if status in {cp_model.OPTIMAL, cp_model.FEASIBLE} else None,
            status,
        )

    # The last successful pass already satisfies its just-added equality and
    # all higher-priority equalities. A redundant final cold solve can only
    # lose that candidate under a timeout, so it is intentionally omitted.
    return previous_solver, cp_model.FEASIBLE
