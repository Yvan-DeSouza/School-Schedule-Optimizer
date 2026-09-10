"""Small deterministic tests for research-only within-scope search semantics."""

from dataclasses import replace

import pytest
from ortools.sat.python import cp_model

from scheduling_engine.realistic_student_assignment_validation import (
    build_realistic_quality_tradeoff_fixture,
)
from scheduling_engine.student_assignment.core import (
    run_substantive_soft_tier_probe,
)
from scheduling_engine.student_assignment.objective_semantics import (
    normalize_penalty,
)
from scheduling_engine.student_assignment.substantive_probe import (
    SubstantiveSoftTierProbeContext,
    probe_substantive_soft_tier,
)


OBJECTIVE_KEYS = (
    "section_utilization_balance",
    "student_semester_balance",
    "course_sequence_preferences",
    "difficulty_balance",
    "course_category_diversity",
)


def _research_context():
    """Four students with four binary decisions each and a known seed."""

    model = cp_model.CpModel()
    groups = []
    owners = []
    current_variables = []
    alternatives = []
    for student_id in range(1, 5):
        for decision_index in range(1, 5):
            current = model.NewBoolVar(f"current_{student_id}_{decision_index}")
            alternate = model.NewBoolVar(f"alternate_{student_id}_{decision_index}")
            model.AddExactlyOne(current, alternate)
            groups.append((current, alternate))
            owners.append(student_id)
            current_variables.append(current)
            alternatives.append(alternate)
    model.Maximize(sum(current_variables))
    seed_solver = cp_model.CpSolver()
    assert seed_solver.Solve(model) == cp_model.OPTIMAL
    model.ClearObjective()

    def source_decisions(solver):
        return tuple(
            (
                ("course", index),
                (
                    owners[index - 1],
                    1000 + index if solver.Value(current_variables[index - 1]) else 2000 + index,
                    None,
                    1,
                    index,
                    None,
                ),
            )
            for index in range(1, 17)
        )

    def source_values(solver):
        return {
            variable.Index(): int(solver.Value(variable))
            for group in groups
            for variable in group
        }

    def components(solver):
        value = float(sum(solver.Value(variable) for variable in current_variables))
        return {
            "section_utilization_balance_penalty": value,
            "student_semester_balance_penalty": 0.0,
            "difficulty_balance_penalty": 0.0,
            "course_category_diversity_penalty": 0.0,
            "soft_sequence_preferences_satisfied": 0.0,
        }

    def summary(solver):
        return {
            "hard_valid": True,
            "fulfillment_complete": True,
            "section_loads": {},
        }

    term_specs = tuple((variable.Index(), 1) for variable in current_variables)
    return SubstantiveSoftTierProbeContext(
        model=model,
        objective_metadata=(
            {
                "kind": "soft_tier",
                "importance_level": 10,
                "semantics_version": "v2",
                "term_specs": term_specs,
                "component_specs": {
                    "section_utilization_balance_penalty": term_specs,
                },
            },
        ),
        complete_required_decision_groups=tuple(groups),
        source_decision_owners=tuple(owners),
        validated_seed_solver=seed_solver,
        seed_outcome=cp_model.OPTIMAL,
        solver_objective_components=components,
        candidate_counts=lambda solver: 16,
        seed_objective_vector=(16.0,),
        source_decision_fingerprint=source_decisions,
        source_decision_summary=summary,
        source_decision_variable_values=source_values,
        seed_source_decision_variable_values=source_values,
        candidate_quality_facts=lambda solver: {
            "summary": {"request_fulfillment": {"unmet": 0}},
            "comparison": {},
            "baseline_objective_semantics": {},
            "candidate_objective_semantics": {},
        },
    )


def _probe(context, semantics, **overrides):
    values = dict(
        threshold=None,
        strict_improvement=True,
        time_limit_seconds=1.0,
        worker_count=1,
        target_importance_level=10,
        neighborhood_radius=16,
        max_changed_students=4,
        selected_student_ids=(1, 2, 3, 4),
        search_semantics=semantics,
    )
    values.update(overrides)
    return probe_substantive_soft_tier(context, **values)


def test_first_qualifying_mode_preserves_one_solve_contract():
    result = _probe(_research_context(), "first_qualifying")

    assert result.complete_candidate_found is True
    assert result.search_semantics == "first_qualifying"
    assert result.status == "optimal"
    assert result.status == "optimal"
    assert len(result.solve_rounds) == 1
    assert result.best_bound is None
    assert result.solve_rounds[0]["hint_contract"] == "validated_seed_unchanged"


def test_minimum_coordination_enforces_frozen_lower_and_upper_bounds():
    result = _probe(_research_context(), "minimum_coordination")

    assert result.complete_candidate_found is True
    assert 3 <= result.changed_student_count <= 4
    assert 10 <= result.changed_source_decision_count <= 16
    assert result.min_changed_students == 3
    assert result.min_changed_source_decisions == 10
    assert len(result.solve_rounds) == 1


def test_minimum_coordination_rejects_incoherent_bounds():
    with pytest.raises(ValueError, match="min_changed_students"):
        _probe(
            _research_context(),
            "minimum_coordination",
            min_changed_students=5,
        )
    with pytest.raises(ValueError, match="min_changed_source_decisions"):
        _probe(
            _research_context(),
            "minimum_coordination",
            min_changed_source_decisions=17,
        )


def test_iterative_refinement_uses_one_budget_and_retains_best_candidate():
    result = _probe(
        _research_context(),
        "iterative_strict_bound_refinement",
        time_limit_seconds=0.5,
    )

    assert result.complete_candidate_found is True
    assert len(result.solve_rounds) >= 2
    assert result.solve_rounds[0]["candidate_found"] is True
    assert result.solve_rounds[0]["active_strict_upper_bound"] == 15
    assert result.first_candidate_substantive_value is not None
    assert result.candidate_substantive_value <= result.first_candidate_substantive_value
    assert result.cumulative_external_solve_wall_seconds == pytest.approx(
        sum(row["external_solve_wall_seconds"] for row in result.solve_rounds)
    )
    assert result.solve_rounds[1]["requested_time_limit_seconds"] < 0.5
    assert result.solve_rounds[1]["active_strict_upper_bound"] == (
        result.solve_rounds[0]["candidate_substantive_value"] - 1
    )
    assert all(
        row["hint_contract"] == "validated_seed_unchanged"
        for row in result.solve_rounds
    )


def test_iterative_refinement_retains_candidate_when_next_bound_is_unknown(monkeypatch):
    import scheduling_engine.student_assignment.substantive_probe as module

    real_factory = module.new_solver
    calls = 0

    class UnknownSolver:
        def Solve(self, _model):
            return cp_model.UNKNOWN

        def WallTime(self):
            return 0.0

        def NumBranches(self):
            return 0

        def NumConflicts(self):
            return 0

    def factory(*args, **kwargs):
        nonlocal calls
        calls += 1
        return real_factory(*args, **kwargs) if calls == 1 else UnknownSolver()

    monkeypatch.setattr(module, "new_solver", factory)
    result = _probe(_research_context(), "iterative_strict_bound_refinement")

    assert result.complete_candidate_found is True
    assert result.solve_rounds[-1]["status"] == "unknown"
    assert result.search_termination_classification == (
        "next_strict_bound_unresolved_with_candidate"
    )


def test_initial_unknown_remains_unknown_and_has_no_candidate(monkeypatch):
    import scheduling_engine.student_assignment.substantive_probe as module

    class UnknownSolver:
        def Solve(self, _model):
            return cp_model.UNKNOWN

        def WallTime(self):
            return 0.0

        def NumBranches(self):
            return 0

        def NumConflicts(self):
            return 0

    monkeypatch.setattr(module, "new_solver", lambda *_args, **_kwargs: UnknownSolver())

    result = _probe(_research_context(), "iterative_strict_bound_refinement")

    assert result.status == "unknown"
    assert result.complete_candidate_found is False
    assert result.search_termination_classification == "unknown"


def _weighted_total(semantics):
    return sum(
        float(row["weighted_normalized_contribution"])
        for row in semantics["components"].values()
    )


@pytest.mark.parametrize("importance_score", (1, 6, 10))
def test_direct_exact_v2_objective_matches_quality_evaluator_for_seed_and_candidate(
    importance_score,
):
    data = replace(
        build_realistic_quality_tradeoff_fixture(),
        objective_semantics_version="v2",
        objective_importance_scores={key: importance_score for key in OBJECTIVE_KEYS},
    )
    result = run_substantive_soft_tier_probe(
        data,
        threshold=None,
        strict_improvement=True,
        time_limit_seconds=2.0,
        worker_count=1,
        neighborhood_radius=16,
        max_changed_students=4,
        search_semantics="direct_exact_v2_optimization",
    )

    assert result.status == "optimal"
    assert result.seed_quality_objective_semantics["version"] == "v2"
    assert result.candidate_quality_objective_semantics["version"] == "v2"
    assert result.baseline_substantive_value == _weighted_total(
        result.seed_quality_objective_semantics
    )
    assert result.candidate_substantive_value == _weighted_total(
        result.candidate_quality_objective_semantics
    )
    assert result.best_bound == result.candidate_substantive_value
    assert result.objective_absolute_gap == 0
    assert result.objective_relative_gap == 0
    assert set(result.candidate_quality_objective_semantics["components"]) == {
        "section_utilization_balance",
        "student_semester_load_balance",
        "course_sequence_preferences",
        "difficulty_balance",
        "course_category_diversity",
    }
    for semantics in (
        result.seed_quality_objective_semantics,
        result.candidate_quality_objective_semantics,
    ):
        for component in semantics["components"].values():
            assert component["normalized_penalty"] == normalize_penalty(
                component["raw_penalty"], component["denominator"]
            )
            assert component["weighted_normalized_contribution"] == (
                component["normalized_penalty"] * component["importance_score"]
            )
    assert result.candidate_summary["hard_valid"] is True
    assert result.candidate_summary["fulfillment_complete"] is True
    assert result.seed_objective_vector[:4] == result.candidate_objective_vector[:4]
    assert result.seed_summary["required_request_count"] == (
        result.candidate_summary["required_request_count"]
    )
    assert result.seed_summary["assigned_request_count"] == (
        result.candidate_summary["assigned_request_count"]
    )
    assert result.changed_source_decision_count <= 16
    assert result.changed_student_count <= 4
    assert result.solve_rounds[0]["hint_contract"] == "validated_seed_unchanged"
    assert result.first_qualifying_latency_seconds is None
    assert result.probe_base_model_fingerprint


def test_direct_optimization_rejects_v1_instead_of_approximating():
    context = _research_context()
    metadata = dict(context.objective_metadata[0])
    metadata["semantics_version"] = "v1"
    context = replace(context, objective_metadata=(metadata,))

    with pytest.raises(ValueError, match="Objective Semantics v2"):
        _probe(context, "direct_exact_v2_optimization")
