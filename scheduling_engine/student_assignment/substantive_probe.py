"""Diagnostic-only feasibility probes for student-assignment quality tiers.

This module never participates in the ordinary student-assignment workflow.
The caller supplies the already-built production model, its validated Stage 1
incumbent, and metadata describing the existing objective expressions.  The
probe adds only diagnostic bounds to a clone and therefore cannot change the
production solver's constraints, objective ordering, or returned result.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from numbers import Real
from time import monotonic

from ortools.sat.python import cp_model

from .solver import new_solver, outcome_name, set_solver_hints
from .runtime import OperationTimer
from ..constants import VALID_STUDENT_GRADE_LEVELS


@dataclass(frozen=True)
class SubstantiveSoftTierProbeContext:
    """The shared full-model facts needed by one diagnostic probe."""

    model: object
    objective_metadata: tuple
    complete_required_decision_groups: tuple
    source_decision_owners: tuple
    validated_seed_solver: object | None
    seed_outcome: int
    solver_objective_components: object
    candidate_counts: object
    seed_objective_vector: tuple
    source_decision_fingerprint: object
    source_decision_summary: object
    source_decision_variable_values: object
    seed_source_decision_variable_values: object
    candidate_quality_facts: object | None = None
    student_grades: tuple = ()


@dataclass(frozen=True)
class SubstantiveSoftTierProbeResult:
    """Serializable-in-practice facts for one diagnostic-only probe."""

    status: str
    seed_solver_outcome: str
    seed_validated: bool
    baseline_substantive_value: float | None
    requested_threshold: float | None
    elapsed_seconds: float
    solver_wall_time_seconds: float
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
    seed_source_decisions: tuple
    candidate_source_decisions: tuple
    seed_summary: dict
    candidate_summary: dict
    affected_student_ids: tuple
    affected_section_ids: tuple
    section_load_deltas: dict
    candidate_source_variable_values: dict
    seed_source_variable_values: dict
    requested_time_limit_seconds: float | None = None
    timings: dict = field(default_factory=dict)
    candidate_quality_summary: dict = field(default_factory=dict)
    quality_comparison: dict = field(default_factory=dict)
    changed_student_count: int = 0
    max_changed_students: int | None = None
    selected_student_ids: tuple = ()
    eligible_targeted_source_decision_count: int = 0
    effective_neighborhood_radius: int | None = None
    selected_grade: int | None = None


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
    strict_improvement: bool = False,
    max_changed_students: int | None = None,
    selected_student_ids=(),
    selected_grade: int | None = None,
    phase_callback=None,
) -> SubstantiveSoftTierProbeResult:
    """Ask whether the unchanged full model can beat one soft tier.

    Every completion-defining source group receives an exactly-one constraint
    on the clone.  All objective expressions before the requested soft tier
    are fixed to their validated Stage 1 values.  The requested tier is then
    bounded, but not minimized: this is a satisfiability question, so CP-SAT
    remains free to change any source decision while preserving every hard
    rule and higher-priority fulfillment result.
    ``strict_improvement=True`` means a strict-improvement query against the
    validated seed's existing substantive value.  This keeps diagnostic
    callers from depending on an objective-vector index while preserving the
    existing ``threshold=None`` meaning of an unconstrained component probe.
    """

    operation_started = monotonic()

    def _emit_phase(phase, event="completed", **facts):
        """Publish optional live diagnostics without changing probe behavior."""

        if phase_callback is None:
            return
        try:
            phase_callback(str(phase), event=str(event), **facts)
        except Exception:
            # Evidence collection is strictly observational; an unavailable
            # status sink must never affect CP-SAT or candidate acceptance.
            return

    selected_student_ids = tuple(sorted(set(selected_student_ids), key=repr))
    if selected_grade is not None:
        selected_grade = int(selected_grade)
        if selected_grade not in VALID_STUDENT_GRADE_LEVELS:
            raise ValueError(f"Unsupported selected grade: {selected_grade}")
        if selected_student_ids:
            raise ValueError("selected_grade cannot be combined with selected_student_ids")
        if neighborhood_radius is not None or max_changed_students is not None:
            raise ValueError(
                "selected_grade is unrestricted and cannot use a neighborhood bound"
            )
        grade_by_student = dict(context.student_grades)
        if not grade_by_student:
            raise ValueError("selected_grade requires immutable student_grades facts")
        selected_grade_student_ids = {
            student_id for student_id, grade_level in grade_by_student.items()
            if int(grade_level) == selected_grade
        }
        if not selected_grade_student_ids:
            raise ValueError(f"No students have selected grade {selected_grade}")
    else:
        selected_grade_student_ids = set()
    eligible_targeted_source_decision_count = (
        sum(
            owner in selected_grade_student_ids
            for owner in context.source_decision_owners
        )
        if selected_grade is not None else sum(
            owner in selected_student_ids
            for owner in context.source_decision_owners
        )
        if selected_student_ids
        else len(context.complete_required_decision_groups)
    )
    effective_neighborhood_radius = (
        min(int(neighborhood_radius), eligible_targeted_source_decision_count)
        if neighborhood_radius is not None
        else None
    )
    if selected_student_ids and neighborhood_radius is None:
        raise ValueError(
            "selected_student_ids requires a source-decision neighborhood radius"
        )
    seed_solver = context.validated_seed_solver
    seed_validated = seed_solver is not None
    seed_outcome = outcome_name(context.seed_outcome)
    timing = OperationTimer()
    if not seed_validated:
        return SubstantiveSoftTierProbeResult(
            status=("infeasible" if context.seed_outcome == cp_model.INFEASIBLE else "unknown"),
            seed_solver_outcome=seed_outcome,
            seed_validated=False,
            baseline_substantive_value=None,
            requested_threshold=(float(threshold) if threshold is not None else None),
            elapsed_seconds=0.0,
            solver_wall_time_seconds=0.0,
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
            seed_source_decisions=(),
            candidate_source_decisions=(),
            seed_summary={},
            candidate_summary={},
            affected_student_ids=(),
            affected_section_ids=(),
            section_load_deltas={},
            candidate_source_variable_values={},
            seed_source_variable_values={},
            requested_time_limit_seconds=float(time_limit_seconds),
            timings={
                **timing.snapshot(),
                "operation_total_seconds": monotonic() - operation_started,
            },
            max_changed_students=max_changed_students,
            selected_student_ids=selected_student_ids,
            selected_grade=selected_grade,
            eligible_targeted_source_decision_count=eligible_targeted_source_decision_count,
            effective_neighborhood_radius=effective_neighborhood_radius,
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
    effective_threshold = (
        int(seed_objective_value) - 1
        if strict_improvement and threshold is None
        else (int(threshold) if threshold is not None else None)
    )
    seed_assignment_count = int(context.candidate_counts(seed_solver))
    seed_source_decisions = dict(context.source_decision_fingerprint(seed_solver))
    seed_source_variable_values = dict(
        context.seed_source_decision_variable_values(seed_solver)
    )
    seed_summary = dict(context.source_decision_summary(seed_solver))

    attempt_setup_started = monotonic()
    _emit_phase("attempt_preparation", "started")
    probe_phase_started = monotonic()
    _emit_phase("probe_model_clone", "started")
    with timing.measure("model_clone_seconds"):
        probe_model = context.model.Clone()
    _emit_phase(
        "probe_model_clone",
        "completed",
        elapsed_seconds=monotonic() - probe_phase_started,
    )
    probe_phase_started = monotonic()
    _emit_phase("probe_completion_constraints", "started")
    with timing.measure("completion_constraints_seconds"):
        for decision_group in context.complete_required_decision_groups:
            probe_model.AddExactlyOne(
                probe_model.GetIntVarFromProtoIndex(variable.Index())
                for variable in decision_group
            )
    _emit_phase(
        "probe_completion_constraints",
        "completed",
        elapsed_seconds=monotonic() - probe_phase_started,
        constraint_count=len(probe_model.Proto().constraints),
    )

    if selected_grade is not None:
        probe_phase_started = monotonic()
        _emit_phase("probe_grade_scope_constraints", "started")
        with timing.measure("grade_scope_constraints_seconds"):
            for group_index, decision_group in enumerate(
                context.complete_required_decision_groups
            ):
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
                owner = (
                    context.source_decision_owners[group_index]
                    if group_index < len(context.source_decision_owners)
                    else None
                )
                if owner is None or owner not in selected_grade_student_ids:
                    probe_model.Add(
                        probe_model.GetIntVarFromProtoIndex(
                            selected_seed_variable.Index()
                        )
                        == 1
                    )
        _emit_phase(
            "probe_grade_scope_constraints",
            "completed",
            elapsed_seconds=monotonic() - probe_phase_started,
        )
    elif neighborhood_radius is not None:
        probe_phase_started = monotonic()
        _emit_phase("probe_neighborhood_constraints", "started")
        with timing.measure("neighborhood_constraints_seconds"):
            changed_group_terms = []
            changed_literals_by_student = {}
            for group_index, decision_group in enumerate(
                context.complete_required_decision_groups
            ):
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
                owner = (
                    context.source_decision_owners[group_index]
                    if group_index < len(context.source_decision_owners)
                    else None
                )
                if owner is not None:
                    changed_literals_by_student.setdefault(owner, []).append(
                        selected_clone_variable.Not()
                    )
                    if selected_student_ids and owner not in selected_student_ids:
                        # A targeted repair freezes every source decision owned
                        # by a student outside the selected neighborhood.  The
                        # candidate still has to satisfy the unchanged full
                        # model and all objective bounds; this is a diagnostic
                        # restriction, never a production scheduling rule.
                        probe_model.Add(selected_clone_variable == 1)
                elif selected_student_ids:
                    # Untagged required groups are fixed context.  They cannot
                    # be moved by a student-targeted repair.
                    probe_model.Add(selected_clone_variable == 1)
            probe_model.Add(sum(changed_group_terms or [0]) <= neighborhood_radius)
            if max_changed_students is not None:
                changed_student_variables = []
                for student_id, literals in sorted(
                    changed_literals_by_student.items(), key=repr
                ):
                    indicator = probe_model.NewBoolVar(
                        f"changed_student_{student_id}"
                    )
                    changed_student_variables.append(indicator)
                    for literal in literals:
                        probe_model.AddImplication(literal, indicator)
                    probe_model.AddBoolOr(literals).OnlyEnforceIf(indicator)
                    probe_model.AddBoolAnd(
                        [literal.Not() for literal in literals]
                    ).OnlyEnforceIf(indicator.Not())
                probe_model.Add(
                    sum(changed_student_variables or [0])
                    <= max(0, int(max_changed_students))
                )
        _emit_phase(
            "probe_neighborhood_constraints",
            "completed",
            elapsed_seconds=monotonic() - probe_phase_started,
        )
    elif max_changed_students is not None:
        raise ValueError(
            "max_changed_students requires a source-decision neighborhood radius"
        )

    # Preserve every objective that precedes the target tier. This includes
    # all fulfillment tiers and any more important soft tier present in a
    # caller's input. The production lexicographic ordering is untouched.
    probe_phase_started = monotonic()
    _emit_phase("probe_objective_bound_constraints", "started")
    with timing.measure("objective_and_bound_constraints_seconds"):
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
        if effective_threshold is not None:
            probe_model.Add(target_expression <= effective_threshold)
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
    _emit_phase(
        "probe_objective_bound_constraints",
        "completed",
        elapsed_seconds=monotonic() - probe_phase_started,
        constraint_count=len(probe_model.Proto().constraints),
    )
    probe_phase_started = monotonic()
    _emit_phase("probe_hint_application", "started")
    with timing.measure("hint_application_seconds"):
        set_solver_hints(
            probe_model,
            seed_solver,
            source_model=context.model,
        )
    _emit_phase(
        "probe_hint_application",
        "completed",
        elapsed_seconds=monotonic() - probe_phase_started,
    )

    _emit_phase(
        "attempt_preparation",
        "completed",
        elapsed_seconds=monotonic() - attempt_setup_started,
    )

    with timing.measure("solver_creation_seconds"):
        solver = new_solver(
            time_limit_seconds,
            worker_count=worker_count,
        )
    _emit_phase("cp_sat", "started")
    with timing.measure("cp_solver_solve_external_wall_seconds"):
        started = monotonic()
        status_code = solver.Solve(probe_model)
        elapsed = monotonic() - started
    _emit_phase(
        "cp_sat",
        "completed",
        elapsed_seconds=elapsed,
        status=outcome_name(status_code),
    )
    complete_candidate_found = status_code in {cp_model.OPTIMAL, cp_model.FEASIBLE}

    candidate_component_values = {}
    candidate_substantive_value = None
    candidate_assignment_count = 0
    changed_source_decision_count = 0
    changed_student_count = 0
    source_decision_deltas = ()
    candidate_objective_vector = ()
    minimized_component_value = None
    candidate_source_decisions = ()
    candidate_source_variable_values = {}
    candidate_summary = {}
    candidate_quality_summary = {}
    quality_comparison = {}
    if complete_candidate_found:
        _emit_phase("candidate_extraction", "started")
        with timing.measure("candidate_quality_evaluation_seconds"):
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
            if minimize_component is not None:
                minimized_component_value = float(
                    solver.Value(component_expressions[minimize_component])
                )
        with timing.measure("semantic_source_decision_extraction_seconds"):
            candidate_source_decisions = dict(
                context.source_decision_fingerprint(solver)
            )
            candidate_source_decisions = tuple(sorted(
                candidate_source_decisions.items(), key=repr,
            ))
            candidate_source_variable_values = dict(
                context.source_decision_variable_values(solver)
            )
            candidate_source_decision_map = dict(candidate_source_decisions)
            source_keys = set(seed_source_decisions) | set(candidate_source_decision_map)
            changed_source_decision_count = sum(
                seed_source_decisions.get(key) != candidate_source_decision_map.get(key)
                for key in source_keys
            )
            source_decision_deltas = tuple(
                {
                    "source_key": key,
                    "before": seed_source_decisions.get(key),
                    "after": candidate_source_decision_map.get(key),
                }
                for key in sorted(source_keys, key=repr)
                if seed_source_decisions.get(key) != candidate_source_decision_map.get(key)
            )
            changed_student_count = len({
                value[0]
                for delta in source_decision_deltas
                for value in (delta["before"], delta["after"])
                if isinstance(value, tuple) and value
            })
            candidate_objective_vector = _objective_vector(
                solver, probe_model, context.objective_metadata
            )
            candidate_summary = dict(context.source_decision_summary(solver))
            if context.candidate_quality_facts is not None:
                quality_facts = context.candidate_quality_facts(solver)
                candidate_quality_summary = dict(
                    quality_facts.get("summary", {})
                )
                quality_comparison = dict(
                    quality_facts.get("comparison", {})
                )
        _emit_phase("candidate_extraction", "completed")

    component_deltas = (
        {
            key: candidate_component_values.get(key, 0.0) - value
            for key, value in seed_component_values.items()
            if isinstance(value, Real)
            and isinstance(candidate_component_values.get(key), Real)
        }
        if complete_candidate_found
        else {}
    )
    affected_students = set()
    affected_sections = set()
    if complete_candidate_found:
        for delta in source_decision_deltas:
            for value in (delta["before"], delta["after"]):
                if value is None:
                    continue
                if value and isinstance(value, tuple):
                    affected_students.add(value[0])
                    if delta["source_key"][0] == "course":
                        section_id = value[1]
                        if section_id is not None:
                            affected_sections.add(section_id)
        seed_loads = seed_summary.get("section_loads", {})
        candidate_loads = candidate_summary.get("section_loads", {})
        section_load_deltas = {
            section_id: candidate_loads.get(section_id, 0) - seed_loads.get(section_id, 0)
            for section_id in set(seed_loads) | set(candidate_loads)
            if candidate_loads.get(section_id, 0) != seed_loads.get(section_id, 0)
        }
    else:
        section_load_deltas = {}
    return SubstantiveSoftTierProbeResult(
        status=outcome_name(status_code),
        seed_solver_outcome=seed_outcome,
        seed_validated=True,
        baseline_substantive_value=float(seed_objective_value),
        requested_threshold=(
            float(effective_threshold)
            if effective_threshold is not None else None
        ),
        elapsed_seconds=elapsed,
        solver_wall_time_seconds=float(
            solver.WallTime() if hasattr(solver, "WallTime") else elapsed
        ),
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
        seed_source_decisions=tuple(sorted(seed_source_decisions.items(), key=repr)),
        candidate_source_decisions=candidate_source_decisions,
        seed_summary=seed_summary,
        candidate_summary=candidate_summary,
        affected_student_ids=tuple(sorted(affected_students)),
        affected_section_ids=tuple(sorted(affected_sections)),
        section_load_deltas=dict(sorted(section_load_deltas.items())),
        candidate_source_variable_values=candidate_source_variable_values,
        seed_source_variable_values=seed_source_variable_values,
        requested_time_limit_seconds=float(time_limit_seconds),
        timings={
            **timing.snapshot(),
            "solver_reported_wall_time_seconds": float(
                solver.WallTime() if hasattr(solver, "WallTime") else elapsed
            ),
            "operation_total_seconds": monotonic() - operation_started,
        },
        candidate_quality_summary=candidate_quality_summary,
        quality_comparison=quality_comparison,
        changed_student_count=changed_student_count,
        max_changed_students=max_changed_students,
        selected_student_ids=selected_student_ids,
        eligible_targeted_source_decision_count=eligible_targeted_source_decision_count,
        effective_neighborhood_radius=effective_neighborhood_radius,
        selected_grade=selected_grade,
    )
