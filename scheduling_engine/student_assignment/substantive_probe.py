"""Diagnostic-only feasibility probes for student-assignment quality tiers.

This module never participates in the ordinary student-assignment workflow.
The caller supplies the already-built production model, its validated Stage 1
incumbent, and metadata describing the existing objective expressions.  The
probe adds only diagnostic bounds to a clone and therefore cannot change the
production solver's constraints, objective ordering, or returned result.
"""

from __future__ import annotations

from dataclasses import dataclass
from time import monotonic

from ortools.sat.python import cp_model

from .solver import new_solver, outcome_name, set_solver_hints


@dataclass(frozen=True)
class SubstantiveSoftTierProbeContext:
    """The shared full-model facts needed by one diagnostic probe."""

    model: object
    objective_metadata: tuple
    complete_required_decision_groups: tuple
    validated_seed_solver: object | None
    seed_outcome: int
    solver_objective_components: object
    candidate_counts: object
    seed_objective_vector: tuple
    source_decision_fingerprint: object


@dataclass(frozen=True)
class SubstantiveSoftTierProbeResult:
    """Serializable-in-practice facts for one diagnostic-only probe."""

    status: str
    seed_solver_outcome: str
    seed_validated: bool
    baseline_substantive_value: float | None
    requested_threshold: float | None
    elapsed_seconds: float
    model_variable_count: int
    model_constraint_count: int
    conflicts: int
    branches: int
    complete_candidate_found: bool
    candidate_substantive_value: float | None
    seed_component_values: dict
    candidate_component_values: dict
    component_deltas: dict
    seed_objective_vector: tuple
    candidate_objective_vector: tuple
    seed_assignment_count: int
    candidate_assignment_count: int
    changed_source_decision_count: int
    source_decision_deltas: tuple
    neighborhood_radius: int | None
    minimized_component: str | None
    minimized_component_value: float | None
    best_bound: float | None
    model_family_variable_counts: dict


def _model_family_variable_counts(model):
    """Classify model variables for diagnostic accounting only.

    The names are assigned by the existing model builder.  This report is
    deliberately observational: it never changes a variable, constraint, or
    objective.  Unknown names remain visible under ``other`` rather than
    being silently attributed to a quality family.
    """

    counts = {}
    for variable in model.Proto().variables:
        name = variable.name or ""
        if name.startswith("enroll_"):
            family = "course_assignment"
        elif (
            name.startswith("commitment_")
            or name.startswith("study_")
            or name.startswith("focus_")
            or name.startswith("co_op_")
        ):
            family = "special_commitment"
        elif name.startswith("utilization_"):
            family = "section_utilization"
        elif name.startswith("semester_balance_"):
            family = "semester_balance"
        elif name.startswith("difficulty_balance_"):
            family = "difficulty_balance"
        elif name.startswith("category_"):
            family = "category_diversity"
        elif name.startswith("sequence_"):
            family = "sequence_preferences"
        elif "preservation" in name:
            family = "schedule_preservation"
        else:
            family = "other"
        counts[family] = counts.get(family, 0) + 1
    return dict(sorted(counts.items()))


def _expression(model, term_specs):
    """Rebuild a recorded linear expression against a cloned model."""

    return sum(
        coefficient * model.GetIntVarFromProtoIndex(variable_index)
        for variable_index, coefficient in term_specs
    )


def _objective_vector(solver, model, objective_metadata):
    return tuple(
        float(
            sum(
                coefficient * solver.Value(
                    model.GetIntVarFromProtoIndex(variable_index)
                )
                for variable_index, coefficient in metadata["term_specs"]
            )
        )
        for metadata in objective_metadata
    )


def probe_substantive_soft_tier(
    context: SubstantiveSoftTierProbeContext,
    *,
    threshold: float | None,
    time_limit_seconds: float,
    worker_count: int,
    target_importance_level: int,
    neighborhood_radius: int | None = None,
    component_bounds=None,
    minimize_component: str | None = None,
) -> SubstantiveSoftTierProbeResult:
    """Ask whether the unchanged full model can beat one soft tier.

    Every completion-defining source group receives an exactly-one constraint
    on the clone.  All objective expressions before the requested soft tier
    are fixed to their validated Stage 1 values.  The requested tier is then
    bounded, but not minimized: this is a satisfiability question, so CP-SAT
    remains free to change any source decision while preserving every hard
    rule and higher-priority fulfillment result.
    """

    seed_solver = context.validated_seed_solver
    seed_validated = seed_solver is not None
    seed_outcome = outcome_name(context.seed_outcome)
    if not seed_validated:
        return SubstantiveSoftTierProbeResult(
            status=("infeasible" if context.seed_outcome == cp_model.INFEASIBLE else "unknown"),
            seed_solver_outcome=seed_outcome,
            seed_validated=False,
            baseline_substantive_value=None,
            requested_threshold=(float(threshold) if threshold is not None else None),
            elapsed_seconds=0.0,
            model_variable_count=0,
            model_constraint_count=0,
            conflicts=0,
            branches=0,
            complete_candidate_found=False,
            candidate_substantive_value=None,
            seed_component_values={},
            candidate_component_values={},
            component_deltas={},
            seed_objective_vector=(),
            candidate_objective_vector=(),
            seed_assignment_count=0,
            candidate_assignment_count=0,
            changed_source_decision_count=0,
            source_decision_deltas=(),
            neighborhood_radius=neighborhood_radius,
            minimized_component=minimize_component,
            minimized_component_value=None,
            best_bound=None,
            model_family_variable_counts={},
        )

    target_entries = [
        (index, metadata)
        for index, metadata in enumerate(context.objective_metadata)
        if metadata.get("kind") == "soft_tier"
        and metadata.get("importance_level") == target_importance_level
    ]
    if len(target_entries) != 1:
        raise ValueError(
            "The substantive probe requires exactly one existing soft tier "
            f"at importance level {target_importance_level}; found {len(target_entries)}."
        )
    target_index, target_metadata = target_entries[0]

    seed_component_values = dict(context.solver_objective_components(seed_solver))
    seed_objective_vector = context.seed_objective_vector
    seed_objective_value = sum(
        coefficient * seed_solver.Value(
            context.model.GetIntVarFromProtoIndex(variable_index)
        )
        for variable_index, coefficient in target_metadata["term_specs"]
    )
    seed_assignment_count = int(context.candidate_counts(seed_solver))
    seed_source_decisions = dict(context.source_decision_fingerprint(seed_solver))

    probe_model = context.model.Clone()
    for decision_group in context.complete_required_decision_groups:
        probe_model.AddExactlyOne(
            probe_model.GetIntVarFromProtoIndex(variable.Index())
            for variable in decision_group
        )

    if neighborhood_radius is not None:
        changed_group_terms = []
        for decision_group in context.complete_required_decision_groups:
            selected_seed_variable = next(
                (
                    variable
                    for variable in decision_group
                    if seed_solver.Value(
                        context.model.GetIntVarFromProtoIndex(variable.Index())
                    )
                ),
                None,
            )
            if selected_seed_variable is None:
                probe_model.AddBoolOr(())
                continue
            selected_clone_variable = probe_model.GetIntVarFromProtoIndex(
                selected_seed_variable.Index()
            )
            changed_group_terms.append(1 - selected_clone_variable)
        probe_model.Add(sum(changed_group_terms or [0]) <= neighborhood_radius)

    # Preserve every objective that precedes the target tier. This includes
    # all fulfillment tiers and any more important soft tier present in a
    # caller's input. The production lexicographic ordering is untouched.
    for index, metadata in enumerate(context.objective_metadata):
        if index >= target_index:
            break
        term_specs = metadata["term_specs"]
        expression = _expression(probe_model, term_specs)
        seed_value = sum(
            coefficient * seed_solver.Value(
                context.model.GetIntVarFromProtoIndex(variable_index)
            )
            for variable_index, coefficient in term_specs
        )
        probe_model.Add(expression == seed_value)

    target_expression = _expression(probe_model, target_metadata["term_specs"])
    if threshold is not None:
        probe_model.Add(target_expression <= int(threshold))
    component_bounds = component_bounds or {}
    component_expressions = {}
    for component_name, bound in component_bounds.items():
        term_specs = target_metadata["component_specs"].get(component_name)
        if term_specs is None:
            raise ValueError(f"Unknown substantive component: {component_name}")
        expression = _expression(probe_model, term_specs)
        component_expressions[component_name] = expression
        probe_model.Add(expression <= int(bound))
    if minimize_component is not None:
        term_specs = target_metadata["component_specs"].get(minimize_component)
        if term_specs is None:
            raise ValueError(f"Unknown substantive component: {minimize_component}")
        component_expressions[minimize_component] = _expression(
            probe_model, term_specs
        )
        probe_model.Minimize(component_expressions[minimize_component])
    set_solver_hints(probe_model, seed_solver)

    solver = new_solver(
        time_limit_seconds,
        worker_count=worker_count,
    )
    started = monotonic()
    status_code = solver.Solve(probe_model)
    elapsed = monotonic() - started
    complete_candidate_found = status_code in {cp_model.OPTIMAL, cp_model.FEASIBLE}

    candidate_component_values = {}
    candidate_substantive_value = None
    candidate_assignment_count = 0
    changed_source_decision_count = 0
    source_decision_deltas = ()
    candidate_objective_vector = ()
    minimized_component_value = None
    if complete_candidate_found:
        candidate_component_values = dict(context.solver_objective_components(solver))
        candidate_substantive_value = float(
            sum(
                coefficient * solver.Value(
                    probe_model.GetIntVarFromProtoIndex(variable_index)
                )
                for variable_index, coefficient in target_metadata["term_specs"]
            )
        )
        candidate_assignment_count = int(context.candidate_counts(solver))
        candidate_source_decisions = dict(
            context.source_decision_fingerprint(solver)
        )
        changed_source_decision_count = sum(
            seed_source_decisions.get(key) != candidate_source_decisions.get(key)
            for key in set(seed_source_decisions) | set(candidate_source_decisions)
        )
        source_decision_deltas = tuple(
            {
                "source_key": key,
                "before": seed_source_decisions.get(key),
                "after": candidate_source_decisions.get(key),
            }
            for key in sorted(
                set(seed_source_decisions) | set(candidate_source_decisions),
                key=repr,
            )
            if seed_source_decisions.get(key) != candidate_source_decisions.get(key)
        )
        candidate_objective_vector = _objective_vector(
            solver, probe_model, context.objective_metadata
        )
        if minimize_component is not None:
            minimized_component_value = float(
                solver.Value(component_expressions[minimize_component])
            )

    component_deltas = (
        {
            key: candidate_component_values.get(key, 0.0) - value
            for key, value in seed_component_values.items()
        }
        if complete_candidate_found
        else {}
    )
    return SubstantiveSoftTierProbeResult(
        status=outcome_name(status_code),
        seed_solver_outcome=seed_outcome,
        seed_validated=True,
        baseline_substantive_value=float(seed_objective_value),
        requested_threshold=(float(threshold) if threshold is not None else None),
        elapsed_seconds=elapsed,
        model_variable_count=len(probe_model.Proto().variables),
        model_constraint_count=len(probe_model.Proto().constraints),
        conflicts=solver.NumConflicts(),
        branches=solver.NumBranches(),
        complete_candidate_found=complete_candidate_found,
        candidate_substantive_value=candidate_substantive_value,
        seed_component_values=seed_component_values,
        candidate_component_values=candidate_component_values,
        component_deltas=component_deltas,
        seed_objective_vector=seed_objective_vector,
        candidate_objective_vector=candidate_objective_vector,
        seed_assignment_count=seed_assignment_count,
        candidate_assignment_count=candidate_assignment_count,
        changed_source_decision_count=changed_source_decision_count,
        source_decision_deltas=source_decision_deltas,
        neighborhood_radius=neighborhood_radius,
        minimized_component=minimize_component,
        minimized_component_value=minimized_component_value,
        best_bound=(
            float(solver.BestObjectiveBound())
            if minimize_component is not None else None
        ),
        model_family_variable_counts=_model_family_variable_counts(probe_model),
    )
