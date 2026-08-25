from dataclasses import replace
from types import SimpleNamespace

from scheduling_engine.realistic_student_assignment_validation import (
    build_realistic_quality_tradeoff_fixture,
)
from scheduling_engine.student_assignment.adaptive_runtime import (
    run_adaptive_local_search_diagnostic,
)
from scheduling_engine.student_assignment.adaptive_search import (
    AdaptiveOperatorAttempt,
    AdaptiveOperatorSpec,
    AdaptiveSearchState,
    DEFAULT_ADAPTIVE_OPERATOR_PORTFOLIO,
    build_adaptive_search_state,
    choose_adaptive_operator,
    replay_adaptive_policy,
)


def _state(*, local_share, utilization_share, history=()):
    return AdaptiveSearchState(
        policy_version="v2-local-allocator-diagnostic-1",
        objective_semantics_version="v2",
        counselor_scores={
            "student_semester_balance": 10,
            "difficulty_balance": 10,
            "course_category_diversity": 10,
            "course_sequence_preferences": 10,
        },
        normalized_components={},
        weighted_contributions={},
        student_local_weighted_total=100.0,
        highest_student_pressure=80.0,
        top_k_pressure={"1": 0.8, "2": 0.9, "5": 1.0, "10": 1.0},
        nonzero_pressure_student_count=5,
        student_local_weighted_share=local_share,
        global_utilization_weighted_share=utilization_share,
        elapsed_seconds=0.0,
        remaining_seconds=60.0,
        operator_history=tuple(history),
    )


def test_policy_prefers_targeted_operator_when_student_pressure_dominates():
    decision = choose_adaptive_operator(
        _state(local_share=0.9, utilization_share=0.1),
        ranked_students=(SimpleNamespace(student_id=7), SimpleNamespace(student_id=8)),
    )
    assert decision.operator.targeted
    assert decision.selected_student_ids
    assert "student_local_pressure_signal" in decision.reasons


def test_policy_prefers_ordinary_operator_when_global_utilization_dominates():
    decision = choose_adaptive_operator(
        _state(local_share=0.1, utilization_share=0.9),
        ranked_students=(SimpleNamespace(student_id=7), SimpleNamespace(student_id=8)),
    )
    assert decision.operator.name == "r2"
    assert decision.selected_student_ids == ()


def test_policy_history_is_explicit_and_replay_is_solver_free():
    attempt = AdaptiveOperatorAttempt(
        operator="targeted_r8_s2",
        status="unknown",
        candidate_found=False,
        candidate_validated=False,
        adopted=False,
        gain=0,
        elapsed_seconds=3,
        unknown=True,
    )
    state = _state(local_share=0.8, utilization_share=0.2, history=(attempt,))
    decision = choose_adaptive_operator(
        state,
        ranked_students=(SimpleNamespace(student_id=7), SimpleNamespace(student_id=8)),
    )
    assert decision.signal_values["attempt_count"] >= 0
    replayed = replay_adaptive_policy(({
        "operator": "targeted_r8_s2",
        "status": "unknown",
        "candidate_found": False,
        "candidate_validated": False,
        "candidate_adopted": False,
        "gain": 0,
        "total_operation_seconds": 3,
    },))
    assert replayed[0].unknown is True


def test_build_state_exposes_pressure_concentration_from_quality_facts():
    data = replace(
        build_realistic_quality_tradeoff_fixture(),
        objective_semantics_version="v2",
        objective_importance_scores={
            "section_utilization_balance": 6,
            "student_semester_balance": 6,
            "course_sequence_preferences": 6,
            "difficulty_balance": 6,
            "course_category_diversity": 6,
        },
    )
    components = {
        name: {"denominator": 100}
        for name in (
            "student_semester_load_balance",
            "difficulty_balance",
            "course_category_diversity",
            "course_sequence_preferences",
        )
    }
    report = {
        "objective_semantics": {
            "components": components,
            "weighted_normalized_contributions": {
                "section_utilization_balance_penalty": 40,
                "difficulty_balance_penalty": 60,
            },
        },
        "student_semester_load_balance": {"entities": {"1": {"absolute_difference": 20}}},
        "difficulty_balance": {"entities": {"1": {"absolute_difference": 40}}},
        "course_category_diversity": {"entities": {"1": {"penalty": 10}}},
        "course_sequence_preferences": {"entities": {}},
    }
    state = build_adaptive_search_state(data, report)
    assert state.highest_student_pressure > 0
    assert state.top_k_pressure["1"] == 1.0
    assert state.global_utilization_weighted_share == 0.4


def test_adaptive_runner_adopts_only_validated_strict_improvement(monkeypatch):
    data = replace(
        build_realistic_quality_tradeoff_fixture(),
        objective_semantics_version="v2",
        objective_importance_scores={
            "section_utilization_balance": 6,
            "student_semester_balance": 6,
            "course_sequence_preferences": 6,
            "difficulty_balance": 6,
            "course_category_diversity": 6,
        },
    )
    quality = {
        "objective_semantics": {
            "components": {
                name: {"denominator": 100}
                for name in (
                    "student_semester_load_balance",
                    "difficulty_balance",
                    "course_category_diversity",
                    "course_sequence_preferences",
                )
            }
        },
        "student_semester_load_balance": {"entities": {"1": {"absolute_difference": 20}}},
        "difficulty_balance": {"entities": {"1": {"absolute_difference": 20}}},
        "course_category_diversity": {"entities": {"1": {"penalty": 20}}},
        "course_sequence_preferences": {"entities": {}},
    }
    initial = SimpleNamespace(
        status="complete",
        solver_outcome="optimal",
        unmet_requests=(),
        assignments=(1,),
        commitment_assignments=(),
        objective_components={"weighted_normalized_contributions": {"x": 100}},
        optimization_facts={
            "quality": {"stage_2": quality},
            "stage_1": {"objective_values": (-1,)},
            "stage_2": {"objective_values": (-1,), "final_source_decisions": (("a", 1),)},
        },
    )
    candidate = SimpleNamespace(
        status="complete",
        solver_outcome="feasible",
        unmet_requests=(),
        assignments=(1, 2),
        commitment_assignments=(),
        objective_components={"weighted_normalized_contributions": {"x": 90}},
        optimization_facts={
            "quality": {"stage_2": quality},
            "stage_2_local_bootstrap": {
                "status": "feasible",
                "candidate_found": True,
                "candidate_validated": True,
                "changed_student_count": 1,
                "changed_source_decision_count": 1,
            },
            "stage_2": {"objective_values": (-2,), "final_source_decisions": (("a", 2),)},
        },
    )
    monkeypatch.setattr(
        "scheduling_engine.student_assignment.adaptive_runtime.run_student_assignment_targeted_s1_diagnostic",
        lambda *args, **kwargs: candidate,
    )
    monkeypatch.setattr(
        "scheduling_engine.student_assignment.adaptive_runtime._quality_report",
        lambda *_args: quality,
    )
    result = run_adaptive_local_search_diagnostic(
        data,
        initial_result=initial,
        total_time_limit_seconds=1,
        per_operator_time_limit_seconds=0.1,
        max_iterations=1,
        portfolio=(AdaptiveOperatorSpec("targeted_r8_s1", 8, 1, True, 1, "targeted_repair"),),
    )
    assert result.result is candidate
    assert result.record.attempts[0]["adopted"] is True
    assert result.source_decisions == (("a", 2),)
    assert result.record.final_assignment_count == 2


def test_policy_portfolio_contains_no_grade_or_global_operator():
    assert {item.name for item in DEFAULT_ADAPTIVE_OPERATOR_PORTFOLIO} == {
        "r2",
        "targeted_r8_s1",
        "targeted_r8_s2",
        "targeted_r4_s2",
    }
