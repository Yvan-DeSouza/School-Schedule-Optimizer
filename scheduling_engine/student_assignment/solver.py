"""CP-SAT orchestration shared by the two student-assignment stages."""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
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


def model_proto_fingerprint(model):
    """Return a stable identity for one CP-SAT model proto representation."""

    # OR-Tools 9.15 exposes ``CpModelProto`` through its Python helper type,
    # which does not expose protobuf's SerializeToString method. Its text
    # representation preserves ordered variables/constraints and is sufficient
    # for the same-process lineage assertion used by this diagnostic path.
    return sha256(str(model.Proto()).encode("utf-8")).hexdigest()


def new_solver(
    time_limit_seconds,
    *,
    fix_hints=False,
    worker_count=1,
    random_seed=0,
    max_deterministic_time=None,
):
    """Build a bounded CP-SAT configuration for the requested stage.

    ``random_seed`` and ``max_deterministic_time`` are intentionally low-level
    diagnostic controls.  The ordinary engine behavior remains unchanged by
    the defaults: one or caller-selected worker count, wall-clock bounding,
    and random seed zero.  Diagnostic callers may opt into an explicit seed
    or deterministic-work bound without introducing a second solver path.
    """

    if max_deterministic_time is not None and max_deterministic_time <= 0:
        raise ValueError("max_deterministic_time must be positive when supplied")

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_seconds
    solver.parameters.num_search_workers = worker_count
    solver.parameters.random_seed = int(random_seed)
    if max_deterministic_time is not None:
        solver.parameters.max_deterministic_time = float(max_deterministic_time)
    solver.parameters.fix_variables_to_their_hinted_value = fix_hints
    return solver


def _validation_variable_freedom(
    model,
    required_decision_groups,
    source_variable_values,
    *,
    prepared_context=None,
):
    """Describe source variables fixed by validation versus left free.

    This is diagnostic accounting only.  The validator continues to use the
    complete production model; the counts make explicit whether a supplied
    semantic candidate fixes every source variable or only the
    completion-defining groups.
    """

    if prepared_context is not None:
        required_indexes = {
            index
            for group in prepared_context.required_decision_group_indexes
            for index in group
        }
        source_indexes = prepared_context.source_variable_indexes
        fixed_indexes = {
            int(index) for index in (source_variable_values or {})
        }
        return {
            "total_variable_count": len(model.Proto().variables),
            "source_variable_count": len(source_indexes),
            "completion_defining_source_variable_count": len(required_indexes),
            "source_variable_fixed_by_candidate_count": len(
                source_indexes & fixed_indexes
            ),
            "source_variable_not_fixed_by_candidate_count": len(
                source_indexes - fixed_indexes
            ),
            "source_variable_fixed_index_count": len(fixed_indexes),
            "singleton_source_variable_count": (
                prepared_context.singleton_source_variable_count
            ),
            "auxiliary_variable_count": prepared_context.auxiliary_variable_count,
            "hinted_variable_count": prepared_context.hinted_variable_count,
            "required_decision_group_count": len(
                prepared_context.required_decision_group_indexes
            ),
            "empty_required_decision_group_count": (
                prepared_context.empty_required_decision_group_count
            ),
            "family_variable_counts": dict(
                prepared_context.family_variable_counts
            ),
        }

    required_indexes = {
        variable.Index()
        for decision_group in required_decision_groups
        for variable in decision_group
    }
    fixed_indexes = {
        int(index) for index in (source_variable_values or {})
    }
    source_indexes = {
        index
        for index, variable in enumerate(model.Proto().variables)
        if (variable.name or "").startswith(("enroll_", "commitment_"))
    }

    family_prefixes = {
        "objective_related": (
            "utilization_",
            "semester_balance_",
            "difficulty_balance_",
            "category_",
            "sequence_",
            "preservation",
            "tie_break",
        ),
        "utilization": ("utilization_",),
        "semester_balance": ("semester_balance_",),
        "difficulty": ("difficulty_balance_",),
        "category_diversity": ("category_",),
        "sequence": ("sequence_",),
        "schedule_preservation": ("preservation",),
        "online_supervision": ("online_",),
        "half_semester": ("half_",),
        "study": ("study_",),
        "focus": ("focus_",),
        "co_op": ("co_op_",),
    }

    family_counts = {}
    for family, prefixes in family_prefixes.items():
        family_counts[family] = sum(
            any((variable.name or "").startswith(prefix) for prefix in prefixes)
            for variable in model.Proto().variables
        )

    return {
        "total_variable_count": len(model.Proto().variables),
        "source_variable_count": len(source_indexes),
        "completion_defining_source_variable_count": len(required_indexes),
        "source_variable_fixed_by_candidate_count": len(
            source_indexes & fixed_indexes
        ),
        "source_variable_not_fixed_by_candidate_count": len(
            source_indexes - fixed_indexes
        ),
        "source_variable_fixed_index_count": len(fixed_indexes),
        "singleton_source_variable_count": sum(
            index in source_indexes
            and len(variable.domain) == 2
            and variable.domain[0] == variable.domain[1]
            for index, variable in enumerate(model.Proto().variables)
        ),
        "auxiliary_variable_count": len(model.Proto().variables) - len(source_indexes),
        "hinted_variable_count": len(model.Proto().solution_hint.vars),
        "required_decision_group_count": len(required_decision_groups),
        "empty_required_decision_group_count": sum(
            not decision_group for decision_group in required_decision_groups
        ),
        "family_variable_counts": family_counts,
    }


def _native_validation_telemetry(
    model,
    solver,
    log_messages,
    *,
    collect_presolve_telemetry,
    collect_search_start_telemetry,
):
    """Use the existing bounded CP-SAT log parsers for validation audits."""

    # The parser implementation is shared with substantive-probe diagnostics.
    # The deferred import avoids a module-import cycle: substantive_probe
    # already imports solver helpers, while this function runs only after both
    # modules have been initialized.
    from .substantive_probe import (
        _build_presolve_telemetry,
        _parse_cp_sat_search_start_facts,
    )

    facts = _build_presolve_telemetry(
        model,
        solver,
        log_messages,
        stopped_after_presolve=bool(collect_presolve_telemetry),
    )
    if collect_search_start_telemetry:
        facts["search_start"] = _parse_cp_sat_search_start_facts(log_messages)
    return facts


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
    telemetry: dict = field(default_factory=dict)


@dataclass(frozen=True)
class PreparedValidationContext:
    """Candidate-independent state for diagnostic repeated validation.

    The context owns a clone of the unchanged full model with the existing
    completion constraints already added.  It never owns a solver, response,
    source values, or candidate-specific equality.  Each validation still
    clones this prepared model and creates a fresh solver, so the ordinary
    CP-SAT authority and fail-closed classifications remain unchanged.

    This is intentionally an opt-in diagnostic facility.  It is useful only
    when one process validates multiple candidates from the exact same model
    lineage; it is not a cross-process cache or a replacement for the full
    validator.
    """

    model: object
    source_model: object
    base_model_fingerprint: str
    base_model_variable_count: int
    base_model_constraint_count: int
    required_decision_group_indexes: tuple[tuple[int, ...], ...]
    source_variable_indexes: frozenset[int]
    family_variable_counts: dict
    singleton_source_variable_count: int
    auxiliary_variable_count: int
    hinted_variable_count: int
    empty_required_decision_group_count: int
    input_semantic_fingerprint: str | None = None
    model_schema_version: str | None = None
    objective_semantics_version: str | None = None
    configuration_fingerprint: str | None = None
    creation_phase_seconds: dict = field(default_factory=dict)
    diagnostic_metadata_complete: bool = False


def prepare_validation_context(
    model,
    required_decision_groups,
    *,
    input_semantic_fingerprint=None,
    model_schema_version=None,
    objective_semantics_version=None,
    configuration_fingerprint=None,
    collect_diagnostic_metadata=False,
):
    """Prepare reusable validation-only model/index state for one lineage.

    The prepared clone contains exactly the same completion ``ExactlyOne``
    constraints the current validator adds for every candidate.  No objective
    or hard rule is removed, and no candidate values are stored.  The original
    model remains untouched and the returned context is safe to reuse only
    with this same in-process model object.
    """

    creation_phase_seconds = {
        "source_model_identity_verification": 0.0,
    }
    phase_started = monotonic()
    prepared_model = model.Clone()
    creation_phase_seconds["model_clone"] = monotonic() - phase_started

    phase_started = monotonic()
    group_indexes = tuple(
        tuple(int(variable.Index()) for variable in decision_group)
        for decision_group in required_decision_groups
    )
    creation_phase_seconds["required_group_index_preparation"] = (
        monotonic() - phase_started
    )

    phase_started = monotonic()
    for group in group_indexes:
        prepared_model.AddExactlyOne(
            prepared_model.GetIntVarFromProtoIndex(index) for index in group
        )
    creation_phase_seconds["completion_constraint_construction"] = (
        monotonic() - phase_started
    )

    phase_started = monotonic()
    base_model_fingerprint = model_proto_fingerprint(model)
    creation_phase_seconds["model_fingerprint"] = monotonic() - phase_started

    source_variable_indexes = frozenset()
    family_variable_counts = {}
    singleton_source_variable_count = 0
    auxiliary_variable_count = 0
    hinted_variable_count = 0
    empty_required_decision_group_count = 0
    if collect_diagnostic_metadata:
        phase_started = monotonic()
        source_variable_indexes = frozenset(
            index
            for index, variable in enumerate(model.Proto().variables)
            if (variable.name or "").startswith(("enroll_", "commitment_"))
        )
        creation_phase_seconds["source_variable_index_preparation"] = (
            monotonic() - phase_started
        )
    else:
        creation_phase_seconds["source_variable_index_preparation"] = 0.0
    family_prefixes = {
        "objective_related": (
            "utilization_", "semester_balance_", "difficulty_balance_",
            "category_", "sequence_", "preservation", "tie_break",
        ),
        "utilization": ("utilization_",),
        "semester_balance": ("semester_balance_",),
        "difficulty": ("difficulty_balance_",),
        "category_diversity": ("category_",),
        "sequence": ("sequence_",),
        "schedule_preservation": ("preservation",),
        "online_supervision": ("online_",),
        "half_semester": ("half_",),
        "study": ("study_",),
        "focus": ("focus_",),
        "co_op": ("co_op_",),
    }
    if collect_diagnostic_metadata:
        phase_started = monotonic()
        family_variable_counts = {
            family: sum(
                any((variable.name or "").startswith(prefix) for prefix in prefixes)
                for variable in model.Proto().variables
            )
            for family, prefixes in family_prefixes.items()
        }
        creation_phase_seconds["static_family_accounting"] = (
            monotonic() - phase_started
        )

        phase_started = monotonic()
        singleton_source_variable_count = sum(
            index in source_variable_indexes
            and len(variable.domain) == 2
            and variable.domain[0] == variable.domain[1]
            for index, variable in enumerate(model.Proto().variables)
        )
        auxiliary_variable_count = (
            len(model.Proto().variables) - len(source_variable_indexes)
        )
        hinted_variable_count = len(model.Proto().solution_hint.vars)
        empty_required_decision_group_count = sum(
            not group for group in group_indexes
        )
        creation_phase_seconds["static_counts_and_hint_accounting"] = (
            monotonic() - phase_started
        )
    else:
        creation_phase_seconds["static_family_accounting"] = 0.0
        creation_phase_seconds["static_counts_and_hint_accounting"] = 0.0
    creation_phase_seconds["total_recorded"] = sum(creation_phase_seconds.values())
    return PreparedValidationContext(
        model=prepared_model,
        source_model=model,
        base_model_fingerprint=base_model_fingerprint,
        base_model_variable_count=len(model.Proto().variables),
        base_model_constraint_count=len(model.Proto().constraints),
        required_decision_group_indexes=group_indexes,
        source_variable_indexes=source_variable_indexes,
        family_variable_counts=family_variable_counts,
        singleton_source_variable_count=singleton_source_variable_count,
        auxiliary_variable_count=auxiliary_variable_count,
        hinted_variable_count=hinted_variable_count,
        empty_required_decision_group_count=empty_required_decision_group_count,
        input_semantic_fingerprint=input_semantic_fingerprint,
        model_schema_version=model_schema_version,
        objective_semantics_version=objective_semantics_version,
        configuration_fingerprint=configuration_fingerprint,
        creation_phase_seconds=creation_phase_seconds,
        diagnostic_metadata_complete=bool(collect_diagnostic_metadata),
    )


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
    random_seed=0,
    max_deterministic_time=None,
    collect_validation_telemetry=False,
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
        random_seed=random_seed,
        max_deterministic_time=max_deterministic_time,
        collect_validation_telemetry=collect_validation_telemetry,
    )
    return outcome.solver if outcome.classification == "validated" else None


def validate_source_decision_candidate_with_status(
    model,
    required_decision_groups,
    source_variable_values,
    time_limit_seconds,
    *,
    worker_count=1,
    random_seed=0,
    max_deterministic_time=None,
    collect_validation_telemetry=False,
    collect_presolve_telemetry=False,
    collect_search_start_telemetry=False,
    base_model_variable_values=None,
    expected_base_model_fingerprint=None,
    prepared_context=None,
    expected_prepared_context_identity=None,
):
    """Validate a candidate and preserve the bounded validator outcome.

    ``hard_invalid`` means CP-SAT proved the fixed source decisions
    inconsistent with the unchanged full model.  ``validation_unknown`` means
    the validator did not establish feasibility within its bound.  Model
    construction/solver exceptions and ``MODEL_INVALID`` are reported as
    ``validation_error``.  None of these non-validated outcomes may be
    adopted by the production or diagnostic caller.
    """

    if collect_presolve_telemetry and collect_search_start_telemetry:
        raise ValueError(
            "Validation presolve and search-start telemetry modes are mutually exclusive"
        )

    validation_started = monotonic()
    telemetry = {
        "model_variable_count_before": None,
        "model_constraint_count_before": None,
        "candidate_model_variable_count_after_clone": None,
        "candidate_model_constraint_count_after_clone": None,
        "candidate_model_variable_count_after_fixes": None,
        "candidate_model_constraint_count_after_fixes": None,
        "required_decision_group_count": len(required_decision_groups),
        "source_variable_value_count": len(source_variable_values),
        "clone_wall_time_seconds": None,
        "completion_constraint_wall_time_seconds": None,
        "source_fix_constraint_wall_time_seconds": None,
        "model_fingerprint_wall_time_seconds": None,
        "variable_freedom_accounting_wall_time_seconds": None,
        "result_classification_wall_time_seconds": None,
        "solver_creation_wall_time_seconds": None,
        "cp_sat_solve_external_wall_time_seconds": None,
        "cp_sat_solver_wall_time_seconds": None,
        "variable_freedom": None,
        "native_cp_sat": {},
        "witness": {
            "enabled": base_model_variable_values is not None,
            "model_fingerprint": None,
            "expected_model_fingerprint": expected_base_model_fingerprint,
            "base_model_variable_count": None,
            "fixed_variable_count": 0,
            "missing_variable_count": 0,
            "extra_variable_count": 0,
            "source_witness_mismatch_count": 0,
        },
        "validation_wall_time_seconds": None,
        "prepared_context": {
            "used": prepared_context is not None,
            "identity_valid": None,
            "completion_constraints_prepared": False,
        },
    }

    try:
        if prepared_context is not None:
            prepared_context_identity = (
                prepared_context.input_semantic_fingerprint,
                prepared_context.model_schema_version,
                prepared_context.objective_semantics_version,
                prepared_context.configuration_fingerprint,
                prepared_context.base_model_fingerprint,
            )
            if (
                expected_prepared_context_identity is not None
                and tuple(expected_prepared_context_identity)
                != prepared_context_identity
            ):
                raise ValueError(
                    "Prepared validation context identity does not match"
                )
            if model is not prepared_context.source_model:
                raise ValueError(
                    "Prepared validation context belongs to a different model object"
                )
            if (
                len(model.Proto().variables)
                != prepared_context.base_model_variable_count
                or len(model.Proto().constraints)
                != prepared_context.base_model_constraint_count
            ):
                raise ValueError(
                    "Prepared validation context model shape no longer matches"
                )
            requested_group_indexes = tuple(
                tuple(int(variable.Index()) for variable in decision_group)
                for decision_group in required_decision_groups
            )
            if requested_group_indexes != prepared_context.required_decision_group_indexes:
                raise ValueError(
                    "Prepared validation context completion groups do not match"
                )
            base_model_fingerprint = prepared_context.base_model_fingerprint
            telemetry["prepared_context"]["identity_valid"] = True
            telemetry["prepared_context"]["completion_constraints_prepared"] = True
            telemetry["model_variable_count_before"] = (
                prepared_context.base_model_variable_count
            )
            telemetry["model_constraint_count_before"] = (
                prepared_context.base_model_constraint_count
            )
        else:
            telemetry["model_variable_count_before"] = len(model.Proto().variables)
            telemetry["model_constraint_count_before"] = len(model.Proto().constraints)
            # A fingerprint is authority-critical only when a full auxiliary
            # witness is being checked or when the caller explicitly requests
            # diagnostic telemetry. Ordinary source-fixed validation does not
            # need to scan the immutable model merely to classify CP-SAT's
            # result.
            if collect_validation_telemetry or base_model_variable_values is not None:
                phase_started = monotonic()
                base_model_fingerprint = model_proto_fingerprint(model)
                telemetry["model_fingerprint_wall_time_seconds"] = (
                    monotonic() - phase_started
                )
            else:
                base_model_fingerprint = None
        telemetry["witness"]["model_fingerprint"] = base_model_fingerprint
        telemetry["witness"]["base_model_variable_count"] = (
            telemetry["model_variable_count_before"]
        )
        if (
            base_model_variable_values is not None
            and expected_base_model_fingerprint is not None
            and base_model_fingerprint != expected_base_model_fingerprint
        ):
            raise ValueError(
                "Candidate witness model fingerprint does not match validation model"
            )

        phase_started = monotonic()
        candidate_model = (
            prepared_context.model.Clone()
            if prepared_context is not None
            else model.Clone()
        )
        telemetry["clone_wall_time_seconds"] = monotonic() - phase_started
        telemetry["candidate_model_variable_count_after_clone"] = len(
            candidate_model.Proto().variables
        )
        telemetry["candidate_model_constraint_count_after_clone"] = len(
            candidate_model.Proto().constraints
        )

        if prepared_context is not None:
            telemetry["completion_constraint_wall_time_seconds"] = 0.0
        else:
            phase_started = monotonic()
            for decision_group in required_decision_groups:
                candidate_model.AddExactlyOne(
                    candidate_model.GetIntVarFromProtoIndex(variable.Index())
                    for variable in decision_group
                )
            telemetry["completion_constraint_wall_time_seconds"] = (
                monotonic() - phase_started
            )

        phase_started = monotonic()
        for variable_index, value in source_variable_values.items():
            # This is validation, not search guidance.  Equality constraints
            # avoid CP-SAT's ``fix_variables_to_their_hinted_value``
            # requirement that every auxiliary variable also carry a hint.
            candidate_model.Add(
                candidate_model.GetIntVarFromProtoIndex(variable_index) == int(value)
            )
        if base_model_variable_values is not None:
            expected_indexes = set(range(len(model.Proto().variables)))
            witness_indexes = {
                int(index) for index in base_model_variable_values
            }
            missing_indexes = expected_indexes - witness_indexes
            extra_indexes = witness_indexes - expected_indexes
            telemetry["witness"]["missing_variable_count"] = len(missing_indexes)
            telemetry["witness"]["extra_variable_count"] = len(extra_indexes)
            if missing_indexes or extra_indexes:
                raise ValueError(
                    "Candidate witness must contain exactly one value for every "
                    "base-model variable"
                )
            source_mismatches = sum(
                int(base_model_variable_values[int(index)]) != int(value)
                for index, value in source_variable_values.items()
                if int(index) in base_model_variable_values
            )
            telemetry["witness"]["source_witness_mismatch_count"] = (
                source_mismatches
            )
            if source_mismatches:
                raise ValueError(
                    "Candidate witness source values disagree with semantic source values"
                )
            for variable_index, value in sorted(
                base_model_variable_values.items(), key=lambda item: int(item[0])
            ):
                variable_index = int(variable_index)
                variable = candidate_model.GetIntVarFromProtoIndex(variable_index)
                value = int(value)
                # The unchanged CP-SAT model remains the authority for domain
                # membership and every derived constraint. Avoid duplicating
                # OR-Tools' domain representation here; an equality outside
                # the domain is rejected by the validation solve itself.
                candidate_model.Add(variable == value)
            telemetry["witness"]["fixed_variable_count"] = len(
                base_model_variable_values
            )
        telemetry["source_fix_constraint_wall_time_seconds"] = (
            monotonic() - phase_started
        )
        telemetry["candidate_model_variable_count_after_fixes"] = len(
            candidate_model.Proto().variables
        )
        telemetry["candidate_model_constraint_count_after_fixes"] = len(
            candidate_model.Proto().constraints
        )
        if collect_validation_telemetry and (
            prepared_context is None
            or prepared_context.diagnostic_metadata_complete
        ):
            phase_started = monotonic()
            telemetry["variable_freedom"] = _validation_variable_freedom(
                candidate_model,
                required_decision_groups,
                source_variable_values,
                prepared_context=prepared_context,
            )
            telemetry["variable_freedom_accounting_wall_time_seconds"] = (
                monotonic() - phase_started
            )
        elif collect_validation_telemetry:
            telemetry["variable_freedom_unavailable_reason"] = (
                "prepared_context_diagnostic_metadata_not_collected"
            )

        phase_started = monotonic()
        validator = new_solver(
            time_limit_seconds,
            worker_count=worker_count,
            random_seed=random_seed,
            max_deterministic_time=max_deterministic_time,
        )
        telemetry["solver_creation_wall_time_seconds"] = monotonic() - phase_started

        native_log_messages = []
        collect_native_log = (
            collect_presolve_telemetry or collect_search_start_telemetry
        )
        if collect_native_log:
            # OR-Tools 9.15 exposes presolve/search milestones through its
            # supported progress log rather than a structured Python API.
            # Keep this entirely opt-in so ordinary validation retains its
            # established configuration and logging behavior.
            validator.parameters.log_search_progress = True
            validator.parameters.log_to_stdout = False
            if collect_presolve_telemetry:
                validator.parameters.stop_after_presolve = True
            validator.log_callback = native_log_messages.append

        phase_started = monotonic()
        try:
            status = validator.Solve(candidate_model)
        finally:
            if collect_native_log:
                # Native logging can retain a worker thread after Solve on
                # Windows; detach the callback immediately after the audit.
                validator.log_callback = None
        telemetry["cp_sat_solve_external_wall_time_seconds"] = (
            monotonic() - phase_started
        )
        solver_wall_time = getattr(validator, "WallTime", None)
        telemetry["cp_sat_solver_wall_time_seconds"] = (
            float(solver_wall_time()) if callable(solver_wall_time) else None
        )
        if collect_native_log:
            telemetry["native_cp_sat"] = _native_validation_telemetry(
                candidate_model,
                validator,
                native_log_messages,
                collect_presolve_telemetry=collect_presolve_telemetry,
                collect_search_start_telemetry=collect_search_start_telemetry,
            )
    except Exception as error:  # pragma: no cover - defensive infrastructure path
        telemetry["validation_wall_time_seconds"] = monotonic() - validation_started
        return SourceDecisionValidationOutcome(
            classification="validation_error",
            solver=None,
            solver_outcome="error",
            error=f"{type(error).__name__}: {error}",
            telemetry=telemetry,
        )

    telemetry["validation_wall_time_seconds"] = monotonic() - validation_started

    classification_started = monotonic()
    if status in {cp_model.OPTIMAL, cp_model.FEASIBLE}:
        telemetry["result_classification_wall_time_seconds"] = (
            monotonic() - classification_started
        )
        return SourceDecisionValidationOutcome(
            classification="validated",
            solver=validator,
            solver_outcome=outcome_name(status),
            telemetry=telemetry,
        )
    if status == cp_model.INFEASIBLE:
        telemetry["result_classification_wall_time_seconds"] = (
            monotonic() - classification_started
        )
        return SourceDecisionValidationOutcome(
            classification="hard_invalid",
            solver=None,
            solver_outcome=outcome_name(status),
            telemetry=telemetry,
        )
    telemetry["result_classification_wall_time_seconds"] = (
        monotonic() - classification_started
    )
    return SourceDecisionValidationOutcome(
        classification=(
            "validation_error"
            if status == cp_model.MODEL_INVALID
            else "validation_unknown"
        ),
        solver=None,
        solver_outcome=outcome_name(status),
        telemetry=telemetry,
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
