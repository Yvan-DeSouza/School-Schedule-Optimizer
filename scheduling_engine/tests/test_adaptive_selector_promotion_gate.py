"""Cheap, solver-free promotion gate for the v2 adaptive policy ladder."""

from dataclasses import replace
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

import scheduling_engine.student_assignment.adaptive_runtime as adaptive_runtime
import scheduling_engine.student_assignment.adaptive_search as adaptive_search
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
    build_adaptive_competition_trace,
    choose_adaptive_operator,
)


RANKED_TEN = tuple(SimpleNamespace(student_id=index) for index in range(1, 11))
RANKED_ONE = (SimpleNamespace(student_id=1),)
SCORES = {
    "section_utilization_balance": 10,
    "student_semester_balance": 10,
    "difficulty_balance": 10,
    "course_category_diversity": 10,
    "course_sequence_preferences": 10,
}
LADDER = (
    "hierarchical_evidence",
    "hierarchical_recent",
    "component_aware",
    "horizon_aware",
)
ALL_SELECTOR_POLICIES = ("evidence_guided",) + LADDER


def _state(
    *,
    local=1.0,
    utilization=0.0,
    history=(),
    elapsed=0.0,
    remaining=60.0,
    weighted=None,
):
    return AdaptiveSearchState(
        policy_version="v2-local-allocator-diagnostic-3",
        objective_semantics_version="v2",
        counselor_scores=dict(SCORES),
        normalized_components={},
        weighted_contributions=dict(weighted or {}),
        student_local_weighted_total=100.0,
        highest_student_pressure=80.0,
        top_k_pressure={"1": 0.8},
        nonzero_pressure_student_count=1,
        student_local_weighted_share=local,
        global_utilization_weighted_share=utilization,
        elapsed_seconds=elapsed,
        remaining_seconds=remaining,
        operator_history=tuple(history),
        utilization_ranked_student_ids=tuple(range(1, 11)),
        candidate_validation_time_limit_seconds=0.0,
    )


def _attempt(
    operator,
    *,
    gain=0.0,
    elapsed=60.0,
    adopted=False,
    status="optimal",
    unknown=False,
    validation_classification="not_attempted",
    weighted_delta=None,
):
    return AdaptiveOperatorAttempt(
        operator=operator,
        status=status,
        candidate_found=adopted,
        candidate_validated=adopted,
        adopted=adopted,
        gain=gain,
        elapsed_seconds=elapsed,
        unknown=unknown,
        validation_classification=validation_classification,
        objective_weighted_delta=dict(weighted_delta or {}),
        objective_improvement_weighted_delta=dict(weighted_delta or {}),
    )


def _decision_name(state, variant, ranked=RANKED_TEN):
    decision = choose_adaptive_operator(
        state,
        ranked_students=ranked,
        adaptive_policy_variant=variant,
    )
    return decision.operator.name if decision else None


def _trace_row(trace, operator):
    return next(row for row in trace["candidates"] if row["operator"] == operator)


def test_complete_matrix_preserves_r4_and_can_select_utilization_or_local_descent():
    r4_history = (_attempt("targeted_r4_s2", gain=10.0, adopted=True),)
    states = {
        "R4_DOMINANT": (_state(local=1.0, utilization=0.0, history=r4_history), RANKED_TEN),
        "UTILIZATION_DOMINANT": (
            _state(local=0.05, utilization=1.0, history=r4_history),
            RANKED_TEN,
        ),
        "LOCAL_DESCENT_JUSTIFIED": (
            _state(local=1.0, utilization=0.0),
            (),
        ),
    }
    expected_roles = {
        "R4_DOMINANT": "targeted_r4_s2",
        "UTILIZATION_DOMINANT": "targeted_utilization_r16_s4",
        "LOCAL_DESCENT_JUSTIFIED": "r2",
    }

    for state_name, (state, ranked) in states.items():
        for variant in ALL_SELECTOR_POLICIES:
            decision = choose_adaptive_operator(
                state,
                ranked_students=ranked,
                adaptive_policy_variant=variant,
            )
            assert decision is not None
            assert decision.operator.name == expected_roles[state_name]


def test_stale_recent_evidence_changes_only_when_recent_formula_justifies_it():
    history = (
        _attempt("targeted_r4_s2", gain=10.0, adopted=True),
        _attempt("targeted_r4_s2"),
        *(_attempt("targeted_r8_s2", gain=10.0, adopted=True) for _ in range(5)),
    )
    state = _state(history=history)

    assert _decision_name(state, "hierarchical_evidence") == "targeted_r4_s2"
    assert _decision_name(state, "hierarchical_recent") == "targeted_r8_s2"


def test_component_crossover_changes_direction_without_changing_objective_values():
    history = (
        _attempt(
            "targeted_r4_s1",
            gain=10.0,
            adopted=True,
            weighted_delta={"difficulty_balance": 10.0},
        ),
        _attempt(
            "targeted_utilization_r16_s4",
            gain=10.0,
            adopted=True,
            weighted_delta={"section_utilization_balance": 10.0},
        ),
    )
    difficulty_state = _state(
        local=1.0,
        utilization=1.0,
        history=history,
        weighted={"difficulty_balance": 10.0, "section_utilization_balance": 1.0},
    )
    utilization_state = replace(
        difficulty_state,
        weighted_contributions={
            "difficulty_balance": 1.0,
            "section_utilization_balance": 10.0,
        },
    )
    assert _decision_name(
        difficulty_state, "component_aware", RANKED_ONE
    ) == "targeted_r4_s1"
    assert _decision_name(
        utilization_state, "component_aware", RANKED_ONE
    ) == "targeted_utilization_r16_s4"
    assert difficulty_state.weighted_contributions != utilization_state.weighted_contributions


def test_horizon_long_short_zero_opportunity_and_budget_behavior():
    history = (_attempt("targeted_r4_s2", gain=0.1, adopted=True),)
    long_state = _state(
        local=0.82,
        utilization=1.0,
        history=history,
        remaining=600.0,
    )
    short_state = replace(long_state, elapsed_seconds=540.0, remaining_seconds=60.0)
    zero_opportunity_state = replace(
        long_state, global_utilization_weighted_share=0.0
    )
    unaffordable_state = replace(long_state, elapsed_seconds=590.0, remaining_seconds=10.0)

    assert _decision_name(long_state, "component_aware") == "targeted_r4_s2"
    assert _decision_name(long_state, "horizon_aware") == "targeted_utilization_r16_s4"
    assert _decision_name(short_state, "horizon_aware") == "targeted_r4_s2"

    trace = build_adaptive_competition_trace(
        zero_opportunity_state,
        ranked_students=RANKED_TEN,
        adaptive_policy_variant="horizon_aware",
    )
    utilization_row = _trace_row(trace, "targeted_utilization_r16_s4")
    assert utilization_row["opportunity"] == 0.0
    assert utilization_row["exploration"] == 0.0

    trace = build_adaptive_competition_trace(
        unaffordable_state,
        ranked_students=RANKED_TEN,
        adaptive_policy_variant="horizon_aware",
    )
    assert _trace_row(trace, "targeted_utilization_r16_s4")["eligible"] is False
    assert _trace_row(trace, "targeted_utilization_r16_s4")["ineligibility_reason"] == (
        "insufficient_remaining_budget"
    )


def test_every_new_variant_reconstructs_all_eligible_scores_and_ties():
    state = _state(
        local=1.0,
        utilization=1.0,
        history=(_attempt("targeted_r4_s2", gain=10.0, adopted=True),),
    )
    for variant in LADDER:
        trace = build_adaptive_competition_trace(
            state,
            ranked_students=RANKED_TEN,
            adaptive_policy_variant=variant,
        )
        eligible = [row for row in trace["candidates"] if row["eligible"]]
        assert len(trace["candidates"]) == 16
        assert max(
            abs(
                row["score"]
                - (
                    row["opportunity"]
                    + row["prior"]
                    + 0.50 * row["hierarchical_evidence"].get(
                        "exact_hierarchical_yield", 0.0
                    )
                    + 0.10 * row["reliability"]
                    + row["exploration"]
                    + 0.10 * row["budget_fit"]
                    - 0.25 * row["unresolved_rate"]
                    - row["duplicate_scope_penalty"]
                    + row["component_alignment_term"]
                    + row["continuation_term"]
                )
            )
            for row in eligible
        ) <= 1e-9
        assert [row["operator"] for row in sorted(eligible, key=lambda row: row["rank"])] == [
            row["operator"]
            for row in sorted(eligible, key=lambda row: row["tie_break"], reverse=True)
        ]


def test_hierarchical_pseudocount_sensitivity_is_bounded_and_smooth():
    spec = next(
        item for item in DEFAULT_ADAPTIVE_OPERATOR_PORTFOLIO
        if item.name == "targeted_r4_s2"
    )
    zero = adaptive_search._hierarchical_yield_observation(
        (), adaptive_search._disjoint_evidence_history(
            (), spec, DEFAULT_ADAPTIVE_OPERATOR_PORTFOLIO
        )
    )["exact_hierarchical_yield"]
    one = adaptive_search._hierarchical_yield_observation(
        (_attempt("targeted_r4_s2", gain=10.0, adopted=True),),
        adaptive_search._disjoint_evidence_history(
            (_attempt("targeted_r4_s2", gain=10.0, adopted=True),),
            spec,
            DEFAULT_ADAPTIVE_OPERATOR_PORTFOLIO,
        ),
    )["exact_hierarchical_yield"]
    strong_history = tuple(
        _attempt("targeted_r4_s2", gain=10.0, adopted=True) for _ in range(20)
    )
    strong = adaptive_search._hierarchical_yield_observation(
        strong_history,
        adaptive_search._disjoint_evidence_history(
            strong_history, spec, DEFAULT_ADAPTIVE_OPERATOR_PORTFOLIO
        ),
    )["exact_hierarchical_yield"]
    assert zero == 0.0
    assert 0.0 < one < strong < 1.0
    assert 0.50 * strong <= 0.50


def test_recent_window_has_bounded_influence_and_preserves_unresolved_positions():
    spec = next(
        item for item in DEFAULT_ADAPTIVE_OPERATOR_PORTFOLIO
        if item.name == "targeted_r4_s2"
    )
    old = _attempt("targeted_r4_s2", gain=10.0, adopted=True)
    fillers = tuple(_attempt("r2") for _ in range(6))
    one_recent = _attempt("targeted_r4_s2", elapsed=0.0)
    two_recent = (
        _attempt("targeted_r4_s2", elapsed=0.0),
        _attempt("targeted_r4_s2", elapsed=0.0),
    )

    def facts(history):
        return adaptive_search._hierarchical_yield_observation(
            history,
            adaptive_search._disjoint_evidence_history(
                history, spec, DEFAULT_ADAPTIVE_OPERATOR_PORTFOLIO
            ),
            use_recent=True,
        )["exact"]

    no_recent = facts((old,) + fillers)
    one = facts((old,) + fillers + (one_recent,))
    two = facts((old,) + fillers + two_recent)
    assert no_recent["recent_weight"] == 0.0
    assert one["recent_weight"] == 0.25
    assert two["recent_weight"] == 0.50
    assert one["blended_yield"] == pytest.approx(0.75)
    assert two["blended_yield"] == pytest.approx(0.50)


@pytest.mark.parametrize(
    "weighted_delta,expected_sign",
    [
        ({"difficulty_balance": 10.0}, 1),
        ({"difficulty_balance": -10.0}, -1),
        ({"course_sequence_preferences": 10.0}, 0),
    ],
)
def test_component_alignment_is_bounded_directional_and_sequence_safe(
    weighted_delta, expected_sign
):
    history = (_attempt("targeted_r4_s2", adopted=True, weighted_delta=weighted_delta),)
    state = _state(
        history=history,
        weighted={
            "difficulty_balance": 10.0,
            "course_sequence_preferences": 0.0,
        },
    )
    spec = next(
        item for item in DEFAULT_ADAPTIVE_OPERATOR_PORTFOLIO
        if item.name == "targeted_r4_s2"
    )
    alignment = adaptive_search._component_alignment_observation(
        state,
        history,
        adaptive_search._disjoint_evidence_history(
            history, spec, DEFAULT_ADAPTIVE_OPERATOR_PORTFOLIO
        ),
    )["alignment"]
    assert -1.0 <= alignment <= 1.0
    assert (alignment > 0) - (alignment < 0) == expected_sign


def test_unresolved_outcomes_remain_distinct_from_resolved_zero_gain():
    history = (
        _attempt("r2", status="unknown", unknown=True),
        _attempt(
            "targeted_r4_s2",
            status="optimal",
            validation_classification="validation_unknown",
        ),
        _attempt(
            "targeted_r8_s2",
            status="validation_error",
            validation_classification="validation_error",
        ),
        _attempt("targeted_utilization_r16_s2", status="infeasible"),
        _attempt("targeted_r4_s1"),
    )
    assert [
        adaptive_search._attempt_is_resolved(item) for item in history
    ] == [False, False, False, True, True]
    spec = next(
        item for item in DEFAULT_ADAPTIVE_OPERATOR_PORTFOLIO
        if item.name == "targeted_r4_s2"
    )
    hierarchy = adaptive_search._hierarchical_yield_observation(
        history,
        adaptive_search._disjoint_evidence_history(
            history, spec, DEFAULT_ADAPTIVE_OPERATOR_PORTFOLIO
        ),
    )
    assert hierarchy["exact"]["lifetime"]["resolved_attempt_count"] == 0
    assert hierarchy["exact_hierarchical_yield"] == 0.0
    assert adaptive_search._group_unknown_rate(history) > 0.0


def test_serialized_prospective_trace_replays_exactly():
    state = _state(
        local=1.0,
        utilization=1.0,
        history=(_attempt("targeted_r4_s2", gain=10.0, adopted=True),),
    )
    decision = choose_adaptive_operator(
        state,
        ranked_students=RANKED_TEN,
        adaptive_policy_variant="horizon_aware",
    )
    original_trace = decision.signal_values["competition_trace"]
    serialized_trace = json.loads(json.dumps(original_trace, default=str))
    replay = adaptive_search.replay_selector_decision(
        serialized_trace, state.operator_history
    )

    assert replay["replay_classification"] == "exact"
    replay_trace = replay["competition_trace"]
    for key in ("candidates", "derived"):
        assert json.dumps(
            original_trace[key], sort_keys=True, default=str,
        ) == json.dumps(replay_trace[key], sort_keys=True, default=str)
    assert replay_trace["derived"]["score_winner"] == original_trace[
        "derived"
    ]["score_winner"]
    assert replay_trace["derived"]["runner_up"] == original_trace[
        "derived"
    ]["runner_up"]
    assert replay_trace["derived"]["winner_margin"] == original_trace[
        "derived"
    ]["winner_margin"]


def test_mocked_runtime_captures_and_replays_a_new_variant(monkeypatch):
    data = replace(
        build_realistic_quality_tradeoff_fixture(),
        objective_semantics_version="v2",
    )
    quality = {"objective_semantics": {"components": {}}}
    initial = SimpleNamespace(
        status="complete",
        solver_outcome="optimal",
        unmet_requests=(),
        assignments=(1,),
        commitment_assignments=(),
        objective_components={"weighted_normalized_contributions": {"x": 100}},
        optimization_facts={
            "stage_2": {
                "objective_values": (-1,),
                "final_source_decisions": (("a", 1),),
            }
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
            "stage_2_local_bootstrap": {
                "status": "feasible",
                "candidate_found": True,
                "candidate_validated": True,
                "changed_student_count": 1,
                "changed_source_decision_count": 1,
                "selected_student_ids": (1,),
                "probe_invocation_student_ids": (1,),
                "probe_result_scope_equal": True,
                "iterations": ({
                    "selected_student_ids": (1,),
                    "probe_invocation_student_ids": (1,),
                    "probe_result_scope_equal": True,
                    "candidate_value": 90,
                },),
            },
            "stage_2": {
                "objective_values": (-2,),
                "final_source_decisions": (("a", 2),),
            },
        },
    )
    spec = AdaptiveOperatorSpec(
        "targeted_r4_s1", 4, 1, True, 1, "targeted_repair"
    )
    state = _state(local=1.0, utilization=0.0)
    runtime_ranked = (
        SimpleNamespace(
            student_id=1,
            weighted_current_penalty=1.0,
            opportunity_signal=1.0,
            nonzero_component_count=1,
            sequence_opportunity_count=0,
            sequence_unsatisfied_count=0,
        ),
    )
    monkeypatch.setattr(adaptive_runtime, "_quality_report", lambda *_: quality)
    monkeypatch.setattr(
        adaptive_runtime,
        "rank_students_by_quality_pressure",
        lambda *_: runtime_ranked,
    )
    monkeypatch.setattr(
        adaptive_runtime,
        "build_adaptive_search_state",
        lambda *_args, **kwargs: replace(
            state,
            operator_history=tuple(kwargs.get("history", ())),
            elapsed_seconds=float(kwargs.get("elapsed_seconds", 0.0)),
            remaining_seconds=float(kwargs.get("remaining_seconds", 0.0)),
            current_source_fingerprint=kwargs.get("current_source_fingerprint"),
        ),
    )
    operator_calls = []

    def fake_operator(*_args, **_kwargs):
        operator_calls.append(len(operator_calls) + 1)
        call_number = operator_calls[-1]
        source = (("a", call_number + 1),)
        value = 100 - (10 * call_number)
        facts = deepcopy(candidate.optimization_facts)
        bootstrap = dict(facts["stage_2_local_bootstrap"])
        bootstrap["iterations"] = ({
            "selected_student_ids": (1,),
            "probe_invocation_student_ids": (1,),
            "probe_result_scope_equal": True,
            "candidate_value": value,
            "candidate_source_decisions": source,
        },)
        facts["stage_2_local_bootstrap"] = bootstrap
        facts["stage_2"] = {
            "objective_values": (-call_number - 1,),
            "final_source_decisions": source,
        }
        return SimpleNamespace(
            status=candidate.status,
            solver_outcome=candidate.solver_outcome,
            unmet_requests=(),
            assignments=candidate.assignments,
            commitment_assignments=(),
            objective_components={
                "weighted_normalized_contributions": {"x": value}
            },
            optimization_facts=facts,
        )

    monkeypatch.setattr(adaptive_runtime, "_operator_result", fake_operator)

    result = run_adaptive_local_search_diagnostic(
        data,
        initial_result=initial,
        total_time_limit_seconds=30.0,
        per_operator_time_limit_seconds=0.1,
        max_iterations=3,
        adaptive_policy_variant="hierarchical_evidence",
        portfolio=(spec,),
        session_overrides={
            "targeted_r4_s1": {"session_time_limit_seconds": 0.1}
        },
    )
    traces = [
        decision["signal_values"]["competition_trace"]
        for decision in result.record.decisions
    ]
    replays = [
        adaptive_search.replay_selector_decision(
            json.loads(json.dumps(trace, default=str)), result.record.attempts
        )
        for trace in traces
    ]

    assert len(traces) == 3
    assert all(trace["trace_complete"] is True for trace in traces)
    assert all(replay["replay_classification"] == "exact" for replay in replays)
    assert result.record.objective_trajectory["schema"] == (
        "adaptive_objective_trajectory_v1"
    )
    assert len(result.record.objective_trajectory["adopted_transitions"]) == 3
    assert result.record.attempts[0]["candidate_validated"] is True
