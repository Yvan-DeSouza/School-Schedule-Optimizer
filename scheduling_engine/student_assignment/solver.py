"""CP-SAT orchestration shared by the two student-assignment stages."""

from __future__ import annotations

from dataclasses import dataclass
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


@dataclass(frozen=True)
class SourceDecisionValidationOutcome:
    """Diagnostic result for validating one semantic source-decision candidate.

    A rejected candidate is not necessarily hard-invalid: a bounded CP-SAT
    validator can also return ``UNKNOWN`` or fail to construct/solve the
    validation model.  Keeping those cases distinct prevents characterization
    reports from turning missing proof into a false constraint explanation.
    The existing caller-facing helper below still returns only the validated
    solver, preserving the established adoption semantics.
    """

    classification: str
    solver: object | None
    solver_outcome: str
    error: str | None = None


def set_solver_hints(model, solver, *, source_model=None):
    """Carry a complete validated candidate into the next lexicographic pass.

    CP-SAT does not automatically retain a prior ``CpSolver`` solution after
    the model gains an equality for that objective. Reapplying all values keeps
    the next pass focused on improvement rather than rediscovering a candidate
    that is already known to satisfy every hard scheduling rule. Read the
    solver response by variable index instead of passing variables from a
    cloned model to ``CpSolver.Value``. OR-Tools may otherwise dereference the
    variable's owning model incorrectly; this is especially unsafe for the
    larger diagnostic neighborhood clones used by continuous sessions.
    """

    model.ClearHints()
    source_model = source_model or model
    response = (
        solver.ResponseProto().solution
        if hasattr(solver, "ResponseProto")
        else ()
    )
    source_variable_count = len(source_model.Proto().variables)
    for index in range(len(model.Proto().variables)):
        variable = model.GetIntVarFromProtoIndex(index)
        if index < len(response):
            value = response[index]
        elif index >= source_variable_count:
            # Diagnostic probe clones may append indicator variables that do
            # not exist in the solved source model. The supplied incumbent has
            # no changed-student indicators set, so zero is the safe hint for
            # these search-only auxiliaries; CP-SAT still validates the model.
            value = 0
        else:
            # Some CP-SAT responses omit values for variables that were
            # eliminated or otherwise unused. When the destination is a
            # clone, query the solver with the variable from the original
            # solved model, never with the clone's variable object.
            source_variable = source_model.GetIntVarFromProtoIndex(index)
            value = solver.Value(source_variable)
        model.AddHint(variable, value)


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


def validate_source_decision_candidate(
    model,
    required_decision_groups,
    source_variable_values,
    time_limit_seconds,
    *,
    worker_count=1,
):
    """Validate a semantic source-decision candidate against the full model.

    Diagnostic alternate-incumbent replays use this boundary to convert a
    source-level candidate back into CP-SAT values.  Every required group is
    made exactly-one, every source variable in the candidate is fixed by the
    validator, and all derived constraints remain owned by the unchanged full
    model.  The returned solver is therefore a valid incumbent, not a
    heuristic schedule.
    """

    outcome = validate_source_decision_candidate_with_status(
        model,
        required_decision_groups,
        source_variable_values,
        time_limit_seconds,
        worker_count=worker_count,
    )
    return outcome.solver if outcome.classification == "validated" else None


def validate_source_decision_candidate_with_status(
    model,
    required_decision_groups,
    source_variable_values,
    time_limit_seconds,
    *,
    worker_count=1,
):
    """Validate a candidate and preserve the bounded validator outcome.

    ``hard_invalid`` means CP-SAT proved the fixed source decisions
    inconsistent with the unchanged full model.  ``validation_unknown`` means
    the validator did not establish feasibility within its bound.  Model
    construction/solver exceptions and ``MODEL_INVALID`` are reported as
    ``validation_error``.  None of these non-validated outcomes may be
    adopted by the production or diagnostic caller.
    """

    try:
        candidate_model = model.Clone()
        for decision_group in required_decision_groups:
            candidate_model.AddExactlyOne(
                candidate_model.GetIntVarFromProtoIndex(variable.Index())
                for variable in decision_group
            )
        for variable_index, value in source_variable_values.items():
            # This is validation, not search guidance.  Equality constraints
            # avoid CP-SAT's ``fix_variables_to_their_hinted_value``
            # requirement that every auxiliary variable also carry a hint.
            candidate_model.Add(
                candidate_model.GetIntVarFromProtoIndex(variable_index) == int(value)
            )
        validator = new_solver(
            time_limit_seconds,
            worker_count=worker_count,
        )
        status = validator.Solve(candidate_model)
    except Exception as error:  # pragma: no cover - defensive infrastructure path
        return SourceDecisionValidationOutcome(
            classification="validation_error",
            solver=None,
            solver_outcome="error",
            error=f"{type(error).__name__}: {error}",
        )

    if status in {cp_model.OPTIMAL, cp_model.FEASIBLE}:
        return SourceDecisionValidationOutcome(
            classification="validated",
            solver=validator,
            solver_outcome=outcome_name(status),
        )
    if status == cp_model.INFEASIBLE:
        return SourceDecisionValidationOutcome(
            classification="hard_invalid",
            solver=None,
            solver_outcome=outcome_name(status),
        )
    return SourceDecisionValidationOutcome(
        classification=(
            "validation_error"
            if status == cp_model.MODEL_INVALID
            else "validation_unknown"
        ),
        solver=None,
        solver_outcome=outcome_name(status),
    )


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


def _objective_values(solver, objectives):
    """Read the existing ordered objective vector from a solver candidate."""

    return tuple(
        float(objective)
        if isinstance(objective, (int, float))
        else float(solver.Value(objective))
        for objective in objectives
    )


def _candidate_is_lexicographically_better(candidate, incumbent, objectives):
    """Compare complete candidates using the existing objective ordering."""

    if candidate is None:
        return False
    if incumbent is None:
        return True
    return _objective_values(candidate, objectives) < _objective_values(
        incumbent, objectives
    )


class _IncumbentTimelineCallback(cp_model.CpSolverSolutionCallback):
    """Bounded diagnostic trace of meaningful CP-SAT incumbents.

    This callback is opt-in.  The ordinary student-assignment path does not
    install it, so recording a timeline cannot affect production search.  A
    diagnostic caller may additionally provide a candidate callback when it
    needs source-decision deltas; that richer extraction is deliberately
    bounded because it is more expensive than reading objective values.
    """

    def __init__(
        self,
        *,
        objective_index,
        objectives,
        sink,
        max_events,
        candidate_callback=None,
        stage_started_at=None,
    ):
        super().__init__()
        self.objective_index = objective_index
        self.objectives = objectives
        self.sink = sink
        self.max_events = max(1, int(max_events))
        self.candidate_callback = candidate_callback
        self.stage_started_at = stage_started_at
        self._last_vector = None

    def on_solution_callback(self):
        if len(self.sink) >= self.max_events:
            return
        vector = _objective_values(self, self.objectives)
        if self._last_vector is not None and vector >= self._last_vector:
            return
        self._last_vector = vector
        event = {
            "objective_index": self.objective_index,
            "elapsed_solver_seconds": float(self.WallTime()),
            "elapsed_stage_2_wall_seconds": (
                float(monotonic() - self.stage_started_at)
                if self.stage_started_at is not None else None
            ),
            "objective_vector": vector,
            "best_bound": float(self.BestObjectiveBound()),
        }
        if self.candidate_callback is not None:
            try:
                event["candidate"] = self.candidate_callback(self)
            except Exception as error:  # pragma: no cover - defensive diagnostics
                event["candidate_error"] = type(error).__name__
        self.sink.append(event)


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
    pass_trace=None,
    pass_candidate_callback=None,
    retain_incumbent_on_non_improvement=False,
    deadline=None,
    incumbent_timeline=None,
    timeline_candidate_callback=None,
    timeline_max_events=128,
    skip_optimization=False,
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
    if skip_optimization:
        # Diagnostic mature-local sessions return the already validated
        # incumbent after their explicit neighborhood phase. This preserves
        # the ordinary optimizer unchanged while preventing an unnecessary
        # post-probe lexicographic pass in that diagnostic path.
        return (
            previous_solver,
            cp_model.FEASIBLE if previous_solver is not None else cp_model.UNKNOWN,
        )
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
        hint_source = "none"
        if total_time_limit_seconds is not None:
            remaining_budget = (
                deadline.remaining()
                if deadline is not None
                else total_time_limit_seconds - (
                    monotonic() - optimization_started
                )
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
            hint_source = "validated_seed" if objective_index == 0 else "prior_pass"
            set_solver_hints(model, previous_solver)
        elif initial_assignment_hints:
            prepared_solver = validated_initial_hint_solver(
                model,
                initial_assignment_hints,
                pass_time_limit_seconds,
                worker_count=worker_count,
            )
            if prepared_solver is not None:
                hint_source = "validated_initial_hint"
                set_solver_hints(model, prepared_solver)
                # The preparatory candidate is safe even if the first bounded
                # optimization pass later reaches UNKNOWN without an incumbent.
                previous_solver = prepared_solver

        entering_candidate = (
            pass_candidate_callback(previous_solver)
            if pass_candidate_callback is not None and previous_solver is not None
            else None
        )
        solver = new_solver(pass_time_limit_seconds, worker_count=worker_count)
        trace_started = monotonic()
        timeline_callback = None
        if incumbent_timeline is not None:
            timeline_callback = _IncumbentTimelineCallback(
                objective_index=objective_index,
                objectives=objectives,
                sink=incumbent_timeline,
                max_events=timeline_max_events,
                candidate_callback=timeline_candidate_callback,
                # Keep this clock anchored to the whole Stage 2 operation.
                # ``elapsed_solver_seconds`` remains pass-local, while the
                # stage wall-clock field must be comparable across passes.
                stage_started_at=optimization_started,
            )
        status = (
            solver.Solve(model, timeline_callback)
            if timeline_callback is not None
            else solver.Solve(model)
        )
        external_solve_wall_time = monotonic() - trace_started
        solver_has_solution = status in {cp_model.OPTIMAL, cp_model.FEASIBLE}
        raw_solver_candidate = solver if solver_has_solution else None
        selected_solver = raw_solver_candidate
        incumbent_retained = False
        if (
            retain_incumbent_on_non_improvement
            and previous_solver is not None
            and raw_solver_candidate is not None
            and not _candidate_is_lexicographically_better(
                raw_solver_candidate,
                previous_solver,
                objectives,
            )
        ):
            selected_solver = previous_solver
            incumbent_retained = True
        returned_candidate_solver = selected_solver or previous_solver
        returned_candidate = (
            pass_candidate_callback(returned_candidate_solver)
            if pass_candidate_callback is not None and returned_candidate_solver is not None
            else None
        )
        if pass_trace is not None:
            pass_trace.append({
                "objective_index": objective_index,
                "hint_source": hint_source,
                "hinted_variable_count": (
                    len(model.Proto().variables) if hint_source != "none" else 0
                ),
                "allocated_time_seconds": pass_time_limit_seconds,
                "wall_time_seconds": solver.WallTime(),
                "external_solve_wall_time_seconds": external_solve_wall_time,
                "trace_wall_time_seconds": external_solve_wall_time,
                "status": outcome_name(status),
                "conflicts": solver.NumConflicts(),
                "branches": solver.NumBranches(),
                "best_bound": float(solver.BestObjectiveBound()),
                "entering_candidate": entering_candidate,
                "returned_candidate": returned_candidate,
                "solver_candidate_found": solver_has_solution,
                "raw_solver_candidate": (
                    pass_candidate_callback(raw_solver_candidate)
                    if pass_candidate_callback is not None and raw_solver_candidate is not None
                    else None
                ),
                "incumbent_retained": incumbent_retained,
            })
        ending_value = (
            float(selected_solver.Value(objective))
            if selected_solver is not None else starting_value
        )
        ending_quality = (
            pass_quality_callback(selected_solver)
            if pass_quality_callback is not None and selected_solver is not None
            else starting_quality
        )
        if pass_facts is not None:
            remaining_after = (
                max(
                    0.0,
                    deadline.remaining()
                    if deadline is not None
                    else total_time_limit_seconds - (
                        monotonic() - optimization_started
                    ),
                )
                if total_time_limit_seconds is not None or deadline is not None
                else None
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
        previous_solver = selected_solver
        model.Add(objective == selected_solver.Value(objective))

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
