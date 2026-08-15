"""Deterministic CP-SAT orchestration shared by student assignment."""

from __future__ import annotations

from ortools.sat.python import cp_model


def outcome_name(status):
    return {
        cp_model.OPTIMAL: "optimal",
        cp_model.FEASIBLE: "feasible",
        cp_model.INFEASIBLE: "infeasible",
        cp_model.MODEL_INVALID: "model_invalid",
        cp_model.UNKNOWN: "unknown",
    }.get(status, "unknown")


def new_solver(time_limit_seconds, *, fix_hints=False):
    """Build the deliberately reproducible CP-SAT configuration for this stage."""

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_seconds
    solver.parameters.num_search_workers = 1
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


def validated_initial_hint_solver(model, assignment_hints, time_limit_seconds):
    """Return a full-model candidate only after CP-SAT validates the hint.

    The deterministic constructor below is search guidance, never a second
    scheduler. Lock, capacity, timeslot, group, and prerequisite constraints
    remain authoritative in the CP-SAT model. Fixing the proposed enrollment
    choices for this bounded preparatory solve lets CP-SAT fill every derived
    variable and rejects a hint that is incompatible with any hard rule.
    """

    if not assignment_hints:
        return None
    set_assignment_hints(model, assignment_hints)
    preparer = new_solver(min(time_limit_seconds, 5.0), fix_hints=True)
    status = preparer.Solve(model)
    if status not in {cp_model.OPTIMAL, cp_model.FEASIBLE}:
        model.ClearHints()
        return None
    return preparer


def solve_lexicographically(
    model, objectives, time_limit_seconds, *, initial_assignment_hints=None,
):
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
            set_solver_hints(model, previous_solver)
        elif initial_assignment_hints:
            prepared_solver = validated_initial_hint_solver(
                model,
                initial_assignment_hints,
                time_limit_seconds,
            )
            if prepared_solver is not None:
                set_solver_hints(model, prepared_solver)
                # The preparatory candidate is safe even if the first bounded
                # optimization pass later reaches UNKNOWN without an incumbent.
                previous_solver = prepared_solver

        solver = new_solver(time_limit_seconds)
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
        solver = new_solver(time_limit_seconds)
        status = solver.Solve(model)
        return (
            solver if status in {cp_model.OPTIMAL, cp_model.FEASIBLE} else None,
            status,
        )

    # The last successful pass already satisfies its just-added equality and
    # all higher-priority equalities. A redundant final cold solve can only
    # lose that candidate under a timeout, so it is intentionally omitted.
    return previous_solver, cp_model.FEASIBLE
