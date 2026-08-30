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
import re
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
    # Every source assignment variable grouped by its owning student.  This
    # is used only by the opt-in projected grade diagnostic so optional source
    # decisions are frozen with the same semantic incumbent as required ones.
    source_variable_groups: tuple = ()
    # Optional source-variable family indexes are diagnostic metadata only. The
    # model builder remains the authority for source semantics; the probe uses
    # these indexes only to report overlapping family counts.
    model_family_variable_indexes: tuple = ()


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
    cp_sat_random_seed: int | None = None
    cp_sat_max_deterministic_time_seconds: float | None = None
    projected_grade_scope: bool = False
    projected_active_source_variable_count: int = 0
    projected_frozen_source_variable_count: int = 0
    model_family_constraint_counts: dict = field(default_factory=dict)
    presolve_telemetry: dict = field(default_factory=dict)
    hint_telemetry: dict = field(default_factory=dict)


def _model_family_variable_counts(model, variable_family_indexes=()):
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
    variable_count = len(model.Proto().variables)
    for family, indexes in variable_family_indexes or ():
        counts[str(family)] = sum(
            0 <= int(index) < variable_count for index in set(indexes)
        )
    return dict(sorted(counts.items()))


def _model_family_constraint_counts(model):
    """Count native CP-SAT constraint families without assigning semantics."""

    counts = {}
    for constraint in model.Proto().constraints:
        family = "unknown"
        # OR-Tools 9.15 exposes the generated constraint wrapper's oneof
        # through ``has_*`` methods rather than protobuf ``WhichOneof``.
        # Keep this diagnostic accounting version-tolerant and independent of
        # the model's scheduling semantics.
        for field_name in (
            "linear",
            "bool_or",
            "bool_and",
            "bool_xor",
            "at_most_one",
            "exactly_one",
            "int_prod",
            "lin_max",
            "element",
            "table",
            "automaton",
            "inverse",
            "reservoir",
            "interval",
            "no_overlap",
            "no_overlap_2d",
            "cumulative",
            "circuit",
            "routes",
            "all_diff",
            "int_div",
            "int_mod",
        ):
            has_field = getattr(constraint, f"has_{field_name}", None)
            if has_field is not None and has_field():
                family = field_name
                break
        counts[family] = counts.get(family, 0) + 1
    return dict(sorted(counts.items()))


def _singleton_domain_variable_count(model):
    """Count source/model variables already fixed before CP-SAT presolve."""

    return sum(
        len(variable.domain) == 2 and variable.domain[0] == variable.domain[1]
        for variable in model.Proto().variables
    )


def _parse_cp_sat_model_summaries(log_messages):
    """Parse the supported human-readable model summaries emitted by CP-SAT.

    OR-Tools 9.15 exposes presolve counts through ``log_search_progress`` but
    does not expose a structured presolved-model object through the Python
    API.  This parser retains only bounded aggregate facts; raw native logs
    are never returned in a result payload.
    """

    summaries = []
    current = None
    lines = "\n".join(log_messages or ()).splitlines()
    # CP-SAT formats large counts with apostrophe group separators on the
    # installed OR-Tools build (for example ``110'922``).  Accept both that
    # form and comma-separated output so telemetry remains observational
    # rather than depending on a locale-specific rendering.
    variable_pattern = re.compile(r"#Variables:\s*([0-9,']+)")
    constraint_pattern = re.compile(r"^#(k[A-Za-z0-9_]+):\s*([0-9,']+)")

    def parse_count(value):
        return int(value.replace(",", "").replace("'", ""))

    for raw_line in lines:
        line = raw_line.strip()
        if line.startswith("Initial ") and " model" in line:
            current = {
                "kind": "initial",
                "variable_count": None,
                "constraint_count": 0,
                "constraint_type_counts": {},
            }
            summaries.append(current)
        elif line.startswith("Presolved ") and " model" in line:
            current = {
                "kind": "presolved",
                "variable_count": None,
                "constraint_count": 0,
                "constraint_type_counts": {},
            }
            summaries.append(current)
        if current is None:
            continue
        variable_match = variable_pattern.search(line)
        if variable_match:
            current["variable_count"] = parse_count(variable_match.group(1))
        constraint_match = constraint_pattern.match(line)
        if constraint_match:
            family = constraint_match.group(1)
            count = parse_count(constraint_match.group(2))
            current["constraint_type_counts"][family] = count
            current["constraint_count"] += count
    initial = next(
        (item for item in summaries if item["kind"] == "initial"), None
    )
    presolved = next(
        (item for item in reversed(summaries) if item["kind"] == "presolved"), None
    )
    return initial, presolved


def _parse_cp_sat_symmetry_facts(log_messages):
    """Return bounded symmetry facts when CP-SAT reports them."""

    lines = "\n".join(log_messages or ()).splitlines()
    graph_pattern = re.compile(
        r"Graph for symmetry has\s+([0-9,']+) nodes and\s+([0-9,']+) arcs"
    )
    done_pattern = re.compile(
        r"Symmetry computation done\. time:\s*([0-9.eE+-]+)"
        r"\s+dtime:\s*([0-9.eE+-]+)"
    )
    graphs = []
    completions = []
    for line in lines:
        graph_match = graph_pattern.search(line)
        if graph_match:
            graphs.append({
                "nodes": int(graph_match.group(1).replace(",", "").replace("'", "")),
                "arcs": int(graph_match.group(2).replace(",", "").replace("'", "")),
            })
        done_match = done_pattern.search(line)
        if done_match:
            completions.append({
                "wall_seconds": float(done_match.group(1)),
                "deterministic_seconds": float(done_match.group(2)),
            })
    return {
        "graph_runs": tuple(graphs),
        "computation_runs": tuple(completions),
    }


def _build_presolve_telemetry(
    model,
    solver,
    log_messages,
    *,
    stopped_after_presolve,
):
    """Build compact solver-native presolve facts for an opt-in audit."""

    initial, presolved = _parse_cp_sat_model_summaries(log_messages)
    original_variable_count = len(model.Proto().variables)
    original_constraint_count = len(model.Proto().constraints)
    presolved_variable_count = (
        presolved["variable_count"] if presolved is not None else None
    )
    presolved_constraint_count = (
        presolved["constraint_count"] if presolved is not None else None
    )
    response = solver.ResponseProto() if hasattr(solver, "ResponseProto") else None
    solver_wall_time = float(solver.WallTime()) if hasattr(solver, "WallTime") else None
    return {
        "enabled": True,
        "stop_after_presolve": bool(stopped_after_presolve),
        "original_variable_count": original_variable_count,
        "original_constraint_count": original_constraint_count,
        "original_singleton_domain_variable_count": _singleton_domain_variable_count(model),
        "initial_log_variable_count": (
            initial["variable_count"] if initial is not None else None
        ),
        "initial_log_constraint_count": (
            initial["constraint_count"] if initial is not None else None
        ),
        "initial_log_constraint_type_counts": (
            dict(initial["constraint_type_counts"])
            if initial is not None else {}
        ),
        "presolved_variable_count": presolved_variable_count,
        "presolved_constraint_count": presolved_constraint_count,
        "presolved_log_constraint_type_counts": (
            dict(presolved["constraint_type_counts"])
            if presolved is not None else {}
        ),
        "variables_removed_by_presolve": (
            original_variable_count - presolved_variable_count
            if presolved_variable_count is not None else None
        ),
        "constraints_removed_by_presolve": (
            original_constraint_count - presolved_constraint_count
            if presolved_constraint_count is not None else None
        ),
        # With stop_after_presolve enabled, CP-SAT's native wall time covers
        # presolve and its immediate response finalization.  It is deliberately
        # not labelled as a pure presolve timer because the Python API exposes
        # no narrower structured duration.
        "presolve_phase_wall_seconds": (
            solver_wall_time if stopped_after_presolve else None
        ),
        "solver_wall_time_seconds": solver_wall_time,
        "deterministic_time_seconds": (
            float(getattr(response, "deterministic_time", 0.0))
            if response is not None else None
        ),
        "symmetry": _parse_cp_sat_symmetry_facts(log_messages),
        "log_summary_available": bool(initial is not None or presolved is not None),
    }


def _build_hint_telemetry(
    model,
    context,
    *,
    selected_grade_student_ids,
    selected_grade,
    projected_grade_scope,
):
    """Describe incumbent hints and fixed source structure without identities."""

    source_indexes = {
        int(index)
        for _family, indexes in context.model_family_variable_indexes or ()
        for index in indexes
    }
    hint_indexes = set(model.Proto().solution_hint.vars)
    singleton_indexes = {
        index
        for index, variable in enumerate(model.Proto().variables)
        if len(variable.domain) == 2 and variable.domain[0] == variable.domain[1]
    }
    explicitly_fixed_indexes = set()
    if selected_grade is not None and not projected_grade_scope:
        for group_index, decision_group in enumerate(
            context.complete_required_decision_groups
        ):
            owner = (
                context.source_decision_owners[group_index]
                if group_index < len(context.source_decision_owners)
                else None
            )
            if owner in selected_grade_student_ids:
                continue
            selected_variable = next(
                (
                    variable for variable in decision_group
                    if context.validated_seed_solver.Value(
                        context.model.GetIntVarFromProtoIndex(variable.Index())
                    )
                ),
                None,
            )
            if selected_variable is not None:
                explicitly_fixed_indexes.add(selected_variable.Index())
    frozen_source_indexes = {
        index for index in source_indexes if index in singleton_indexes
    }
    if selected_grade is not None and context.source_variable_groups:
        frozen_source_indexes = {
            index
            for owner, indexes in context.source_variable_groups
            if owner not in selected_grade_student_ids
            for index in indexes
        } if projected_grade_scope else explicitly_fixed_indexes
    return {
        "hint_count": len(hint_indexes),
        "hinted_source_variable_count": len(hint_indexes & source_indexes),
        "hinted_auxiliary_variable_count": len(hint_indexes - source_indexes),
        "source_variable_count": len(source_indexes),
        "outside_grade_source_variable_count": (
            len(frozen_source_indexes)
            if selected_grade is not None else None
        ),
        "explicit_grade_fixed_source_variable_count": len(explicitly_fixed_indexes),
        "singleton_domain_variable_count": len(singleton_indexes),
        "singleton_domain_source_variable_count": len(singleton_indexes & source_indexes),
        "hinted_singleton_domain_variable_count": len(hint_indexes & singleton_indexes),
        "hinted_frozen_source_variable_count": len(hint_indexes & frozen_source_indexes),
        "projected_grade_scope": bool(projected_grade_scope),
    }


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
    projected_grade_scope: bool = False,
    cp_sat_random_seed: int | None = None,
    cp_sat_max_deterministic_time_seconds: float | None = None,
    phase_callback=None,
    collect_presolve_telemetry: bool = False,
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
    if projected_grade_scope and selected_grade is None:
        raise ValueError("projected_grade_scope requires selected_grade")
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
            cp_sat_random_seed=cp_sat_random_seed,
            cp_sat_max_deterministic_time_seconds=(
                cp_sat_max_deterministic_time_seconds
            ),
            projected_grade_scope=bool(projected_grade_scope),
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

    projected_active_source_variable_count = 0
    projected_frozen_source_variable_count = 0
    if selected_grade is not None:
        probe_phase_started = monotonic()
        _emit_phase("probe_grade_scope_constraints", "started")
        if projected_grade_scope:
            # Residualize only source variables whose semantic owner is
            # outside the selected actual grade.  Their incumbent values are
            # substituted as singleton domains; all auxiliary variables and
            # every hard/shared/global constraint remain in the clone.  This
            # is a diagnostic formulation, never a candidate-authority path.
            with timing.measure("grade_scope_constraints_seconds"):
                if not context.source_variable_groups:
                    raise ValueError(
                        "projected_grade_scope requires source variable groups"
                    )
                for owner, variable_indexes in context.source_variable_groups:
                    is_active_owner = owner in selected_grade_student_ids
                    for variable_index in variable_indexes:
                        if is_active_owner:
                            projected_active_source_variable_count += 1
                            continue
                        source_value = int(
                            seed_solver.Value(
                                context.model.GetIntVarFromProtoIndex(variable_index)
                            )
                        )
                        variable_proto = probe_model.Proto().variables[variable_index]
                        variable_proto.domain.clear()
                        variable_proto.domain.extend((source_value, source_value))
                        projected_frozen_source_variable_count += 1
        else:
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
            random_seed=(
                0 if cp_sat_random_seed is None else int(cp_sat_random_seed)
            ),
            max_deterministic_time=cp_sat_max_deterministic_time_seconds,
        )
    presolve_log_messages = []
    if collect_presolve_telemetry:
        # OR-Tools exposes presolved model counts through its supported log
        # stream, not through a structured Python response. This opt-in audit
        # mode captures that stream, suppresses stdout, and stops after
        # presolve so it cannot be mistaken for a quality-search result.
        solver.parameters.log_search_progress = True
        solver.parameters.log_to_stdout = False
        solver.parameters.stop_after_presolve = True
        solver.log_callback = presolve_log_messages.append
    hint_telemetry = {}
    if collect_presolve_telemetry:
        hint_telemetry = _build_hint_telemetry(
            probe_model,
            context,
            selected_grade_student_ids=selected_grade_student_ids,
            selected_grade=selected_grade,
            projected_grade_scope=projected_grade_scope,
        )
    _emit_phase("cp_sat", "started")
    with timing.measure("cp_solver_solve_external_wall_seconds"):
        started = monotonic()
        try:
            status_code = solver.Solve(probe_model)
        finally:
            # The OR-Tools logging callback can retain a native logging
            # thread after Solve returns on Windows.  Detach it immediately
            # after the opt-in presolve audit so a completed test/process is
            # not held open by diagnostic plumbing.
            if collect_presolve_telemetry:
                solver.log_callback = None
        elapsed = monotonic() - started
    _emit_phase(
        "cp_sat",
        "completed",
        elapsed_seconds=elapsed,
        status=outcome_name(status_code),
    )
    presolve_telemetry = (
        _build_presolve_telemetry(
            probe_model,
            solver,
            presolve_log_messages,
            stopped_after_presolve=bool(collect_presolve_telemetry),
        )
        if collect_presolve_telemetry else {}
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
        model_family_variable_counts=_model_family_variable_counts(
            probe_model,
            context.model_family_variable_indexes,
        ),
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
        cp_sat_random_seed=cp_sat_random_seed,
        cp_sat_max_deterministic_time_seconds=(
            cp_sat_max_deterministic_time_seconds
        ),
        projected_grade_scope=bool(projected_grade_scope),
        projected_active_source_variable_count=(
            projected_active_source_variable_count
        ),
        projected_frozen_source_variable_count=(
            projected_frozen_source_variable_count
        ),
        model_family_constraint_counts=_model_family_constraint_counts(probe_model),
        presolve_telemetry=presolve_telemetry,
        hint_telemetry=hint_telemetry,
    )
