"""Contracts for the diagnostic fixed-cycle sequence study."""

from scheduling_engine.benchmark_sequence_ablation import (
    SEQUENCE_VARIANTS,
    _attempt_summary,
    _complementarity_events,
    _sequence_contributions,
    compare_fixed_cycle_control_payloads,
    fixed_cycle_parity_projection,
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


def _control_payload(*, ablation=False, validation_classification="validated"):
    budget = {
        "profile": "balanced",
        "profile_fingerprint": "profile-fingerprint",
        "worker_count": 1,
        "per_operator_maximum_seconds": 300.0,
        "candidate_validation_time_limit_seconds": 180.0,
        "candidate_validation_worker_count": 1,
        "parent_hard_wall_seconds": 1800.0,
        "full_model_validation_required": True,
        "ordinary_stage2_between_iterations": False,
        "session_overrides": {
            name: {
                "session_time_limit_seconds": 300.0,
                "session_max_attempts": 1,
                "per_attempt_cp_sat_limit_seconds": 300.0,
            }
            for name in SEQUENCE_VARIANTS["full_fixed_cycle"]
        },
    }
    budget[
        "cumulative_search_opportunity_seconds"
        if ablation
        else "cumulative_policy_budget_seconds"
    ] = 900.0
    attempt = {
        "operator": "targeted_r4_s2",
        "target_scope": {"student_count": 2},
        "actual_target_scope": {"student_ids": [10, 11]},
        "candidate_found": True,
        "candidate_validated": validation_classification == "validated",
        "candidate_source_decision_fingerprint": "candidate-fingerprint",
        "gain": 78.0,
        "status": "complete",
        "validation_classification": validation_classification,
        "validation_solver_outcome": "optimal",
        "adopted": validation_classification == "validated",
        "inner_probe_summaries": [
            {
                "iteration": 0,
                "operator": "targeted_r4_s2",
                "radius": 4,
                "effective_radius": 4,
                "target_scope": {"student_count": 2},
                "actual_target_scope": {"student_ids": [10, 11]},
                "affected_student_ids": [10],
                "affected_section_ids": [20, 21],
                "candidate_found": True,
                "candidate_complete": True,
                "candidate_validated": validation_classification == "validated",
                "candidate_source_decision_fingerprint": "candidate-fingerprint",
                "candidate_substantive_value": 42672.0,
                "starting_incumbent_value": 42750.0,
                "substantive_gain": 78.0,
                "component_deltas": {"section_utilization": -4.0},
                "validation_classification": validation_classification,
                "status": "feasible",
                "stopping_reason": "validated_candidate",
                "model_variable_count": 100,
                "model_constraint_count": 50,
            }
        ],
    }
    payload = {
        "policy": "fixed_cycle",
        "sequence": list(SEQUENCE_VARIANTS["full_fixed_cycle"])
        if ablation
        else None,
        "profile": "balanced",
        "profile_fingerprint": "profile-fingerprint",
        "input_fingerprint": "input-fingerprint",
        "source_seed_fingerprint": "seed-fingerprint",
        "budget_contract": budget,
        "worker_count": 1,
        "cp_sat_random_seed": 101,
        "initial_substantive_value": 42750.0,
        "attempts": [attempt],
        "execution_status": "complete",
        "candidate_complete": True,
        "final_substantive_value": 42672.0
        if validation_classification == "validated"
        else 42750.0,
        "final_source_decision_fingerprint": "candidate-fingerprint"
        if validation_classification == "validated"
        else "seed-fingerprint",
        "final_assignment_count": 9030,
        "final_unmet_count": 0,
        "final_special_commitment_count": 140,
    }
    return payload


def test_fixed_cycle_parity_projection_normalizes_parent_and_ablation_contracts():
    parent = fixed_cycle_parity_projection(_control_payload())
    ablation = fixed_cycle_parity_projection(_control_payload(ablation=True))
    assert parent["sequence"] == ablation["sequence"]
    assert parent["budget"] == ablation["budget"]


def test_fixed_cycle_parity_reports_validation_transition_variance():
    result = compare_fixed_cycle_control_payloads(
        _control_payload(),
        _control_payload(
            ablation=True,
            validation_classification="validation_unknown",
        ),
    )
    assert result["classification"] == "VALIDATION_TRANSITION_VARIANCE"
    paths = [item["path"] for item in result["trajectory"]["differences"]]
    assert any("candidate_validated" in path for path in paths)
    assert any("validation_classification" in path for path in paths)
    assert "candidate_validated" in result["trajectory"]["first_divergence"]["path"]


def test_fixed_cycle_parity_reports_configuration_mismatch():
    parent = _control_payload()
    ablation = _control_payload(ablation=True)
    ablation["cp_sat_random_seed"] = 202
    result = compare_fixed_cycle_control_payloads(parent, ablation)
    assert result["classification"] == "CONFIGURATION_NON_PARITY"


def test_fixed_cycle_parity_distinguishes_candidate_generation_divergence():
    parent = _control_payload()
    ablation = _control_payload(ablation=True)
    ablation["attempts"][0]["candidate_found"] = False
    ablation["attempts"][0]["candidate_source_decision_fingerprint"] = None
    ablation["attempts"][0]["candidate_validated"] = False
    ablation["attempts"][0]["validation_classification"] = "not_attempted"
    ablation["attempts"][0]["adopted"] = False
    result = compare_fixed_cycle_control_payloads(parent, ablation)
    assert result["classification"] == "TRAJECTORY_NON_PARITY"
    assert any(
        marker in result["trajectory"]["first_divergence"]["path"]
        for marker in ("candidate_found", "candidate_source_decision_fingerprint")
    )
