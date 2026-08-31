"""Contracts for the diagnostic fixed-cycle sequence study."""

from scheduling_engine.benchmark_sequence_ablation import (
    SEQUENCE_VARIANTS,
    _attempt_summary,
    _complementarity_events,
    _sequence_contributions,
    sequence_ablation_budget_contract,
)


def test_sequence_variants_are_existing_operator_sequences():
    assert SEQUENCE_VARIANTS["full_fixed_cycle"] == (
        "targeted_r4_s2",
        "targeted_utilization_r64_s8",
        "r2",
    )
    assert SEQUENCE_VARIANTS["r4_s2_only"] == ("targeted_r4_s2",)
    assert SEQUENCE_VARIANTS["no_r2"] == (
        "targeted_r4_s2",
        "targeted_utilization_r64_s8",
    )
    assert SEQUENCE_VARIANTS["no_utilization"] == (
        "targeted_r4_s2",
        "r2",
    )
    assert SEQUENCE_VARIANTS["reversed_role_order"] == (
        "targeted_utilization_r64_s8",
        "targeted_r4_s2",
        "r2",
    )
    assert SEQUENCE_VARIANTS["r4_utilization_r4"] == (
        "targeted_r4_s2",
        "targeted_utilization_r64_s8",
        "targeted_r4_s2",
    )


def test_sequence_budget_requires_full_validation_and_no_unvalidated_adoption():
    contract = sequence_ablation_budget_contract()
    assert contract["worker_count"] == 1
    assert contract["per_operator_maximum_seconds"] == 300.0
    assert contract["cumulative_search_opportunity_seconds"] == 900.0
    assert contract["full_model_validation_required"] is True
    assert contract["unvalidated_candidate_adoption"] is False
    assert contract["ordinary_stage2_between_iterations"] is False


def test_sequence_contributions_use_outer_adopted_transition_once():
    attempts = [
        {
            "operator": "targeted_r4_s2",
            "adopted": True,
            "gain": 4,
        },
        {
            "operator": "targeted_utilization_r64_s8",
            "adopted": True,
            "gain": 6,
        },
        {
            "operator": "r2",
            "adopted": False,
            "gain": 100,
        },
    ]
    assert _sequence_contributions(attempts) == {
        "by_role": {"r4_s2": 4.0, "utilization": 6.0, "r2": 0.0},
        "gain_after_prior_role_transition": 6.0,
        "adopted_count": 2,
        "total_gain": 10.0,
    }


def test_sequence_complementarity_requires_r4_failure_then_other_role_then_r4():
    attempts = [
        {
            "operator": "targeted_r4_s2",
            "adopted": False,
            "exhaustion_classification": "OPERATOR_NON_IMPROVING",
        },
        {"operator": "targeted_utilization_r64_s8", "adopted": True},
        {"operator": "targeted_r4_s2", "adopted": True},
    ]
    events = _complementarity_events(attempts)
    assert len(events) == 1
    assert events[0]["classification"] == "OBSERVED ROLE COMPLEMENTARITY"


def test_attempt_summary_preserves_role_pressure_and_status_facts():
    attempt = {
        "sequence_position": 2,
        "operator": "r2",
        "status": "unknown",
        "role_pressure_before": {"role_signals": {"r2": 3}},
        "role_pressure_after": {"role_signals": {"r2": 3}},
        "exhaustion_classification": "OPERATOR_UNRESOLVED",
        "role_exhaustion_classification": "ROLE_REMAINS_ACTIONABLE",
        "candidate_found": False,
        "candidate_validated": False,
        "adopted": False,
        "inner_probe_summaries": (),
    }
    result = _attempt_summary(attempt, {"r2": "local_descent"})
    assert result["sequence_position"] == 2
    assert result["role"] == "local_descent"
    assert result["exhaustion_classification"] == "OPERATOR_UNRESOLVED"
    assert result["role_exhaustion_classification"] == "ROLE_REMAINS_ACTIONABLE"
    assert result["role_pressure_before"]["role_signals"]["r2"] == 3
