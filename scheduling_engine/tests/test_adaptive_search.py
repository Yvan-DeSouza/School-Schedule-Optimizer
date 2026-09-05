from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

import scheduling_engine.student_assignment.adaptive_runtime as adaptive_runtime
import scheduling_engine.student_assignment.adaptive_search as adaptive_search
import scheduling_engine.student_assignment.core as student_assignment_core

from scheduling_engine.realistic_student_assignment_validation import (
    build_realistic_quality_tradeoff_fixture,
    build_production_shaped_medium_fixture,
)
from scheduling_engine.dto import (
    StudentAssignmentInputDTO,
    StudentAssignmentRequestDTO,
    StudentAssignmentSectionDTO,
    TimeSlotDTO,
)
from scheduling_engine.student_assignment.adaptive_runtime import (
    _compact_inner_probe_summary,
    run_adaptive_local_search_diagnostic,
)
from scheduling_engine.student_assignment.search_experiments import (
    source_decision_fingerprint,
)
from scheduling_engine.student_assignment.adaptive_search import (
    ADAPTIVE_ROLE_BIAS_MULTIPLIER,
    ADAPTIVE_POLICY_VARIANTS,
    EVIDENCE_GUIDED_CONTINUATION_LIMIT,
    EVIDENCE_GUIDED_OPERATOR_PRIORS,
    AdaptiveOperatorAttempt,
    AdaptiveOperatorSpec,
    AdaptivePolicyDecision,
    AdaptiveSearchState,
    DEFAULT_ADAPTIVE_OPERATOR_PORTFOLIO,
    build_adaptive_search_state,
    build_operator_session_request,
    choose_adaptive_operator,
    build_adaptive_competition_trace,
    adaptive_selector_state_snapshot,
    select_fixed_cycle_operator,
    select_stateless_role_operator,
    replay_adaptive_policy,
    replay_selector_artifact,
    replay_selector_decision,
    operator_family,
)
from scheduling_engine.student_assignment.core import (
    run_student_assignment_operator_session_diagnostic,
    run_student_assignment_stage2_diagnostic,
    solve_student_assignment,
)
from scheduling_engine.student_assignment.operator_session import (
    ContinuousOperatorSessionConfig,
    build_continuous_operator_session_record,
    measured_lazy_prepared_context_decision,
    operator_session_target_count,
    select_operator_session_targets,
)
from scheduling_engine.student_assignment.grade_guidance import (
    build_grade_opportunity_facts,
)
from scheduling_engine.student_assignment.substantive_probe import (
    _parse_cp_sat_model_summaries,
    _parse_cp_sat_search_start_facts,
)


def _state(*, local_share, utilization_share, history=()):
    return AdaptiveSearchState(
        policy_version="v2-local-allocator-diagnostic-2",
        objective_semantics_version="v2",
        counselor_scores={
            "section_utilization_balance": 10,
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
        utilization_ranked_student_ids=(7, 8, 9, 10, 11, 12, 13, 14, 15, 16),
    )


def _multi_attempt_operator_fixture(student_ids=(1, 2)):
    """Eight independent groups give radius-limited sessions several steps."""

    blocks = (
        (1, "A", 1001),
        (1, "B", 1002),
        (1, "C", 1003),
        (1, "D", 1004),
        (2, "A", 2001),
        (2, "B", 2002),
        (2, "C", 2003),
        (2, "D", 2004),
    )
    requests = []
    source = []
    sections = []
    for group_index, (semester, block, timeslot_id) in enumerate(blocks, start=1):
        course_id = 100 + group_index
        offering_id = 1000 + group_index
        first_section_id = group_index * 10 + 1
        sections.extend((
            StudentAssignmentSectionDTO(
                section_id=first_section_id,
                delivery_group_id=group_index,
                member_course_offering_ids=(offering_id,),
                member_course_ids=(course_id,),
                semester=semester,
                timeslot_id=timeslot_id,
                capacity_max=len(student_ids),
                target_capacity=len(student_ids),
            ),
            StudentAssignmentSectionDTO(
                section_id=first_section_id + 1,
                delivery_group_id=group_index,
                member_course_offering_ids=(offering_id,),
                member_course_ids=(course_id,),
                semester=semester,
                timeslot_id=timeslot_id,
                capacity_max=len(student_ids),
                target_capacity=len(student_ids),
            ),
        ))
        for student_id in student_ids:
            request_id = student_id * 100 + group_index
            requests.append(StudentAssignmentRequestDTO(
                request_id=request_id,
                student_id=student_id,
                course_id=course_id,
                course_offering_id=offering_id,
                is_primary=True,
                is_mandatory=True,
                priority_tier=1,
            ))
            source.append((
                ("course", request_id),
                (student_id, first_section_id, None, semester, timeslot_id, None),
            ))
    return (
        StudentAssignmentInputDTO(
            academic_year_id=1,
            requests=tuple(requests),
            sections=tuple(sections),
            fixed_enrollments=(),
            hard_prerequisites=(),
            soft_sequence_preferences=(),
            section_utilization_balance_importance="important",
            student_semester_balance_importance="not_important",
            course_sequence_preferences_importance="not_important",
            difficulty_balance_importance="not_important",
            course_category_diversity_importance="not_important",
            timeslots=tuple(
                TimeSlotDTO(timeslot_id, 1, semester, block, True)
                for semester, block, timeslot_id in blocks
            ),
        ),
        tuple(source),
    )


def test_grade_bounded_operator_uses_actual_grade_without_radius_or_student_cap():
    original_data, source = _multi_attempt_operator_fixture((1, 2))
    data = replace(original_data, student_grades=((1, 9), (2, 12)))
    config = ContinuousOperatorSessionConfig(
        operator_family="grade_bounded_g9",
        target_policy="fixed",
        selected_grade=9,
    )
    assert config.neighborhood_radius is None
    assert config.max_changed_students is None
    assert operator_session_target_count("grade_bounded_g9") is None

    result = run_student_assignment_operator_session_diagnostic(
        data,
        operator_family="grade_bounded_g9",
        selected_grade=9,
        target_policy="fixed",
        initial_source_decisions=source,
        total_time_limit_seconds=8,
        max_attempts=1,
        per_attempt_time_limit_seconds=2,
        worker_count=1,
        hard_feasibility_validation_time_limit_seconds=2,
        hard_feasibility_validation_worker_count=1,
        collect_resource_telemetry=False,
    )
    facts = result.optimization_facts["stage_2_local_bootstrap"]
    assert result.status == "complete"
    assert not result.unmet_requests
    assert facts["selected_grade"] == 9
    assert facts["selected_student_ids"] == ()
    assert facts["iterations"][0]["selected_grade"] == 9
    assert facts["iterations"][0]["radius"] is None
    assert facts["iterations"][0]["changed_student_count"] <= 1
    assert facts["iterations"][0]["candidate_validated"] is True
    assert set(facts["iterations"][0]["affected_student_ids"]).issubset({1})
    assert facts["grade_opportunity"]["student_ids"] == (1,)


def test_projected_grade_scope_matches_full_grade_scope_on_complete_fixture():
    """The residualized diagnostic keeps the same complete semantic result."""

    original_data, source = _multi_attempt_operator_fixture((1, 2))
    data = replace(original_data, student_grades=((1, 9), (2, 12)))
    common = dict(
        operator_family="grade_bounded_g9",
        selected_grade=9,
        target_policy="fixed",
        initial_source_decisions=source,
        total_time_limit_seconds=4,
        max_attempts=1,
        per_attempt_time_limit_seconds=2,
        worker_count=1,
        hard_feasibility_validation_time_limit_seconds=2,
        hard_feasibility_validation_worker_count=1,
        collect_resource_telemetry=False,
    )

    full_result = run_student_assignment_operator_session_diagnostic(
        data, **common
    )
    projected_result = run_student_assignment_operator_session_diagnostic(
        data, projected_grade_scope=True, **common
    )

    assert full_result.status == projected_result.status == "complete"
    assert not full_result.unmet_requests
    assert not projected_result.unmet_requests
    assert full_result.assignments == projected_result.assignments
    assert full_result.commitment_assignments == projected_result.commitment_assignments
    assert full_result.objective_components == projected_result.objective_components

    projected_facts = projected_result.optimization_facts[
        "stage_2_local_bootstrap"
    ]
    iteration = projected_facts["iterations"][0]
    assert iteration["projected_grade_scope"] is True
    assert iteration["projected_active_source_variable_count"] > 0
    assert iteration["projected_frozen_source_variable_count"] > 0
    assert iteration["candidate_validated"] is True


def test_projected_grade_scope_includes_special_commitment_source_variables():
    """Study, Focus, and Co-op sources participate in grade residualization."""

    data = build_production_shaped_medium_fixture(
        student_count=80,
        special_profile_cycle=50,
    )
    data = replace(
        data,
        student_grades=tuple(
            (student_id, 9 if student_id % 2 else 12)
            for student_id in range(1, 81)
        ),
    )
    initial = run_student_assignment_stage2_diagnostic(
        data,
        total_time_limit_seconds=8,
        hard_feasibility_time_limit_seconds=8,
        hard_feasibility_validation_time_limit_seconds=8,
        hard_feasibility_worker_count=1,
        hard_feasibility_validation_worker_count=1,
        optimization_worker_count=1,
        capture_final_source_decisions=True,
    )
    assert initial.status == "complete"
    assert {item.commitment_kind for item in initial.commitment_assignments} >= {
        "study",
        "focus",
        "co_op",
    }
    source = initial.optimization_facts["stage_2"]["final_source_decisions"]

    result = run_student_assignment_operator_session_diagnostic(
        data,
        operator_family="grade_bounded_g9",
        selected_grade=9,
        projected_grade_scope=True,
        target_policy="fixed",
        initial_source_decisions=source,
        total_time_limit_seconds=4,
        max_attempts=1,
        per_attempt_time_limit_seconds=2,
        worker_count=1,
        hard_feasibility_validation_time_limit_seconds=2,
        hard_feasibility_validation_worker_count=1,
        collect_resource_telemetry=False,
    )

    assert result.status == "complete"
    assert not result.unmet_requests
    iteration = result.optimization_facts[
        "stage_2_local_bootstrap"
    ]["iterations"][0]
    assert iteration["projected_grade_scope"] is True
    assert iteration["projected_frozen_source_variable_count"] > 0
    if iteration["candidate_value"] is not None:
        assert iteration["candidate_validated"] is True


def test_projected_grade_scope_can_report_presolve_and_hint_telemetry():
    """The opt-in audit reports native presolve and hint facts."""

    original_data, source = _multi_attempt_operator_fixture((1, 2))
    data = replace(original_data, student_grades=((1, 9), (2, 12)))
    common = dict(
        data=data,
        operator_family="grade_bounded_g9",
        selected_grade=9,
        target_policy="fixed",
        initial_source_decisions=source,
        total_time_limit_seconds=4,
        max_attempts=1,
        per_attempt_time_limit_seconds=2,
        worker_count=1,
        hard_feasibility_validation_time_limit_seconds=2,
        hard_feasibility_validation_worker_count=1,
        collect_resource_telemetry=False,
    )
    result = run_student_assignment_operator_session_diagnostic(
        projected_grade_scope=True,
        collect_presolve_telemetry=True,
        **common,
    )

    assert result.status == "complete"
    assert not result.unmet_requests
    iteration = result.optimization_facts["stage_2_local_bootstrap"][
        "iterations"
    ][0]
    presolve = iteration["presolve_telemetry"]
    hints = iteration["hint_telemetry"]
    assert presolve["enabled"] is True
    assert presolve["stop_after_presolve"] is True
    assert presolve["initial_log_variable_count"] == iteration[
        "model_variable_count"
    ]
    assert presolve["initial_log_constraint_count"] == iteration[
        "model_constraint_count"
    ]
    assert presolve["presolved_variable_count"] is not None
    assert presolve["presolved_constraint_count"] is not None
    assert hints["projected_grade_scope"] is True
    assert hints["outside_grade_source_variable_count"] > 0
    assert hints["hinted_frozen_source_variable_count"] > 0
    assert iteration["model_family_constraint_counts"]


def test_operator_session_can_report_search_start_without_stopping_at_presolve():
    data, source = _multi_attempt_operator_fixture((1, 2))
    result = run_student_assignment_operator_session_diagnostic(
        data,
        operator_family="targeted_r4_s2",
        target_policy="fixed",
        selected_student_ids=(1, 2),
        initial_source_decisions=source,
        total_time_limit_seconds=4,
        max_attempts=1,
        per_attempt_time_limit_seconds=2,
        worker_count=1,
        hard_feasibility_validation_time_limit_seconds=2,
        hard_feasibility_validation_worker_count=1,
        collect_resource_telemetry=False,
        collect_search_start_telemetry=True,
    )

    iteration = result.optimization_facts["stage_2_local_bootstrap"][
        "iterations"
    ][0]
    telemetry = iteration["search_start_telemetry"]
    assert telemetry["enabled"] is True
    assert telemetry["search_started"] is True
    assert telemetry["first_branch_time_supported"] is False
    assert iteration["presolve_telemetry"]["stop_after_presolve"] is False


def test_operator_session_can_bound_candidate_validation_independently():
    data, source = _multi_attempt_operator_fixture((1, 2))
    result = run_student_assignment_operator_session_diagnostic(
        data,
        operator_family="targeted_r4_s2",
        target_policy="fixed",
        selected_student_ids=(1, 2),
        initial_source_decisions=source,
        total_time_limit_seconds=8,
        max_attempts=1,
        per_attempt_time_limit_seconds=1,
        candidate_validation_time_limit_seconds=1,
        worker_count=1,
        hard_feasibility_validation_time_limit_seconds=2,
        hard_feasibility_validation_worker_count=1,
        collect_resource_telemetry=False,
    )

    facts = result.optimization_facts["stage_2_local_bootstrap"]
    iteration = facts["iterations"][0]
    assert facts["independent_validation_budget_enabled"] is True
    assert facts["candidate_validation_time_limit_seconds"] == pytest.approx(1)
    assert iteration["search_time_limit_seconds"] == pytest.approx(1)
    assert iteration["candidate_validation_time_limit_seconds"] == pytest.approx(1)
    assert iteration["validation_budget_scope"] == "independent_operation"
    assert iteration["remaining_stage2_budget_at_validation_start"] is not None
    assert iteration["validation_requested_time_limit_seconds"] <= 1
    assert iteration["candidate_validated"] is True
    assert iteration["validation_classification"] == "validated"
    assert iteration["source_decision_identity_checked"] is True
    assert iteration["source_decision_identity_matches"] is True
    assert iteration["candidate_source_decision_count"] == (
        iteration["validated_source_decision_count"]
    )
    assert iteration["validation_telemetry"][
        "cp_sat_solve_external_wall_time_seconds"
    ] is not None


def test_presolve_parser_accepts_ortools_grouped_counts():
    initial, presolved = _parse_cp_sat_model_summaries((
        "Initial satisfaction model '':",
        "#Variables: 110'922",
        "#kLinearN: 75'848",
        "Presolved satisfaction model '':",
        "#Variables: 23'242",
        "#kLinearN: 1'300",
    ))

    assert initial["variable_count"] == 110922
    assert initial["constraint_count"] == 75848
    assert presolved["variable_count"] == 23242
    assert presolved["constraint_count"] == 1300


def test_search_start_parser_reports_only_supported_native_milestones():
    facts = _parse_cp_sat_search_start_facts((
        "Starting presolve at 0.12s",
        "Presolve summary:",
        "Preloading model.",
        "The solution hint is complete and is feasible.",
        "Starting search at 1.75s",
        "#1       2.10s best:42 next:[0,41]",
        "#Bound   2.25s best:42 next:[0,41]",
    ))

    assert facts["search_started"] is True
    assert facts["presolve_start_seconds"] == pytest.approx(0.12)
    assert facts["search_start_seconds"] == pytest.approx(1.75)
    assert facts["first_solution_seconds"] == pytest.approx(2.10)
    assert facts["first_bound_seconds"] == pytest.approx(2.25)
    assert facts["presolve_summary_emitted"] is True
    assert facts["preloading_model_started"] is True
    assert facts["complete_hint_reported"] is True
    assert facts["first_branch_seconds"] is None
    assert facts["first_branch_time_supported"] is False


def test_operator_session_emits_live_phase_breadcrumbs_without_changing_result():
    data, source = _multi_attempt_operator_fixture((1, 2))
    events = []

    result = run_student_assignment_operator_session_diagnostic(
        data,
        operator_family="targeted_r4_s2",
        target_policy="fixed",
        selected_student_ids=(1, 2),
        initial_source_decisions=source,
        total_time_limit_seconds=8,
        max_attempts=1,
        per_attempt_time_limit_seconds=2,
        worker_count=1,
        hard_feasibility_validation_time_limit_seconds=2,
        hard_feasibility_validation_worker_count=1,
        collect_resource_telemetry=False,
        phase_callback=lambda phase, **facts: events.append((phase, facts)),
    )

    assert result.status == "complete"
    assert not result.unmet_requests
    phases = {phase for phase, _facts in events}
    assert {
        "student_assignment_input",
        "model_construction",
        "mature_seed_materialization",
        "mature_seed_validation",
        "operator_static_setup",
        "target_preparation",
        "attempt_preparation",
        "cp_sat",
        "candidate_validation",
    }.issubset(phases)


def test_grade_opportunity_facts_cover_all_supported_grades_and_locks():
    original_data, source = _multi_attempt_operator_fixture((1, 2))
    data = replace(original_data, student_grades=((1, 9), (2, 12)))
    opportunities = build_grade_opportunity_facts(data, source, {})
    by_grade = {item.grade_level: item for item in opportunities}
    assert set(by_grade) == {9, 10, 11, 12}
    assert by_grade[9].student_ids == (1,)
    assert by_grade[12].student_ids == (2,)
    assert by_grade[10].effective_search_available is False


def test_grade_bounded_configuration_rejects_missing_or_dynamic_grade_scope():
    with pytest.raises(ValueError, match="selected_grade"):
        ContinuousOperatorSessionConfig(
            operator_family="grade_bounded_g11",
            target_policy="fixed",
        )
    with pytest.raises(ValueError, match="fixed targeting"):
        ContinuousOperatorSessionConfig(
            operator_family="grade_bounded_g11",
            target_policy="dynamic",
            selected_grade=11,
        )


def test_all_grade_operator_identities_require_the_matching_actual_grade():
    for grade_level in (9, 10, 11, 12):
        config = ContinuousOperatorSessionConfig(
            operator_family=f"grade_bounded_g{grade_level}",
            target_policy="fixed",
            selected_grade=grade_level,
        )
        assert config.neighborhood_radius is None
        assert config.max_changed_students is None
    with pytest.raises(ValueError, match="selected_grade 12"):
        ContinuousOperatorSessionConfig(
            operator_family="grade_bounded_g12",
            target_policy="fixed",
            selected_grade=9,
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
    assert decision.operator.portfolio_role == "utilization_repair"
    assert decision.selected_student_ids == (7, 8, 9, 10)


def test_adaptive_policy_variants_are_bounded_role_gate_biases():
    assert ADAPTIVE_ROLE_BIAS_MULTIPLIER == 0.25
    assert ADAPTIVE_POLICY_VARIANTS == (
        "balanced",
        "student_pressure_biased",
        "utilization_biased",
        "evidence_guided",
        "r4_anchor",
    )
    balanced = choose_adaptive_operator(
        _state(local_share=0.4, utilization_share=0.46),
        ranked_students=(SimpleNamespace(student_id=7), SimpleNamespace(student_id=8)),
    )
    student_biased = choose_adaptive_operator(
        _state(local_share=0.4, utilization_share=0.46),
        ranked_students=(SimpleNamespace(student_id=7), SimpleNamespace(student_id=8)),
        adaptive_policy_variant="student_pressure_biased",
    )
    utilization_biased = choose_adaptive_operator(
        _state(local_share=0.46, utilization_share=0.4),
        ranked_students=(SimpleNamespace(student_id=7), SimpleNamespace(student_id=8)),
        adaptive_policy_variant="utilization_biased",
    )

    assert balanced.operator.portfolio_role == "utilization_repair"
    assert student_biased.operator.portfolio_role == "targeted_repair"
    assert utilization_biased.operator.portfolio_role == "utilization_repair"
    assert student_biased.signal_values["role_signals"]["targeted_repair"] == 0.4
    assert student_biased.signal_values["adjusted_role_signals"]["targeted_repair"] == 0.5
    assert utilization_biased.signal_values["role_biases"]["utilization_repair"] == 0.1


def test_adaptive_bias_does_not_turn_a_nonzero_signal_into_an_automatic_role():
    decision = choose_adaptive_operator(
        _state(local_share=0.2, utilization_share=0.3),
        ranked_students=(SimpleNamespace(student_id=7), SimpleNamespace(student_id=8)),
        adaptive_policy_variant="student_pressure_biased",
    )
    assert decision.operator.portfolio_role == "utilization_repair"
    assert decision.signal_values["role_signals"]["targeted_repair"] > 0
    assert decision.signal_values["adjusted_role_signals"]["targeted_repair"] < (
        decision.signal_values["adjusted_role_signals"]["utilization_repair"]
    )


def test_evidence_guided_policy_competes_across_roles_with_bounded_prior():
    ranked = (SimpleNamespace(student_id=7), SimpleNamespace(student_id=8))
    decision = choose_adaptive_operator(
        _state(local_share=0.4, utilization_share=0.46),
        ranked_students=ranked,
        adaptive_policy_variant="evidence_guided",
    )
    assert decision.operator.name == "targeted_r4_s2"
    assert decision.signal_values["evidence_guided"]["operator_prior"] == (
        EVIDENCE_GUIDED_OPERATOR_PRIORS["targeted_r4_s2"]
    )
    assert decision.signal_values["selected_role"] == "targeted_repair"


def test_evidence_guided_policy_allows_decisive_utilization_signal_to_win():
    decision = choose_adaptive_operator(
        _state(local_share=0.1, utilization_share=1.0),
        ranked_students=(SimpleNamespace(student_id=7), SimpleNamespace(student_id=8)),
        adaptive_policy_variant="evidence_guided",
    )
    assert decision.operator.portfolio_role == "utilization_repair"


def test_r4_anchor_starts_with_r4_then_uses_fresh_incumbent_continuation():
    ranked = (SimpleNamespace(student_id=7), SimpleNamespace(student_id=8))
    first = choose_adaptive_operator(
        _state(local_share=0.1, utilization_share=1.0),
        ranked_students=ranked,
        adaptive_policy_variant="r4_anchor",
    )
    assert first.operator.name == "targeted_r4_s2"
    previous = AdaptiveOperatorAttempt(
        operator="targeted_r4_s2",
        status="optimal",
        candidate_found=True,
        candidate_validated=True,
        adopted=True,
        gain=12,
        elapsed_seconds=10,
        source_fingerprint_before="old",
        candidate_source_decision_fingerprint="new",
        target_scope=(7, 8),
        actual_target_scope=(7, 8),
    )
    continued = choose_adaptive_operator(
        replace(
            _state(local_share=0.1, utilization_share=1.0, history=(previous,)),
            current_source_fingerprint="new",
        ),
        ranked_students=ranked,
        adaptive_policy_variant="r4_anchor",
    )
    assert continued.operator.name == "targeted_r4_s2"
    assert "productive_validated_continuation" in continued.reasons
    assert EVIDENCE_GUIDED_CONTINUATION_LIMIT == 2


def test_evidence_guided_policy_does_not_repeat_exhausted_source_scope():
    exhausted = AdaptiveOperatorAttempt(
        operator="targeted_r4_s2",
        status="optimal",
        candidate_found=False,
        candidate_validated=False,
        adopted=False,
        gain=0,
        elapsed_seconds=10,
        target_scope=(7, 8),
        actual_target_scope=(7, 8),
        exhaustion_classification="EXACT_SCOPE_EXHAUSTED",
    )
    decision = choose_adaptive_operator(
        replace(
            _state(local_share=0.4, utilization_share=0.46, history=(exhausted,)),
            current_source_fingerprint=None,
        ),
        portfolio=(
            AdaptiveOperatorSpec("targeted_r4_s2", 4, 2, True, 2, "targeted_repair"),
        ),
        ranked_students=(SimpleNamespace(student_id=7), SimpleNamespace(student_id=8)),
        adaptive_policy_variant="evidence_guided",
    )
    assert decision is None


def test_evidence_guided_policy_preserves_grade_escape_stagnation_gate():
    grade = AdaptiveOperatorSpec(
        "grade_bounded_g9", None, None, False, 0, "basin_escape",
        target_policy="fixed", selected_grade=9,
    )
    state = replace(
        _state(local_share=0.0, utilization_share=0.0),
        grade_opportunities=(
            {"grade_level": 9, "effective_search_available": True},
        ),
        consecutive_no_improvement_attempts=1,
    )
    assert choose_adaptive_operator(
        state,
        portfolio=(grade,),
        adaptive_policy_variant="evidence_guided",
    ) is None


def test_r4_anchor_records_explicit_fallback_when_anchor_is_unavailable():
    decision = choose_adaptive_operator(
        _state(local_share=0.2, utilization_share=0.2),
        portfolio=(AdaptiveOperatorSpec("r2", 2, None, False, 0, "local_descent"),),
        adaptive_policy_variant="r4_anchor",
    )
    assert decision.operator.name == "r2"
    assert "r4_anchor_fallback" in decision.reasons
    assert decision.signal_values["anchor_fallback"] is True


def test_operator_family_groups_existing_portfolio_without_changing_operator_names():
    assert operator_family("targeted_r4_s1") == "targeted_r4"
    assert operator_family("targeted_utilization_r64_s8") == "utilization_r64"
    assert operator_family("grade_bounded_g12") == "grade_bounded_g12"


def test_balanced_adaptive_variant_is_backward_compatible():
    state = _state(local_share=0.8, utilization_share=0.2)
    ranked = (SimpleNamespace(student_id=7), SimpleNamespace(student_id=8))
    historical = choose_adaptive_operator(state, ranked_students=ranked)
    explicit = choose_adaptive_operator(
        state,
        ranked_students=ranked,
        adaptive_policy_variant="balanced",
    )
    assert historical.to_dict() == explicit.to_dict()


def test_role_signals_bound_rounding_inflated_student_share():
    state = replace(
        _state(local_share=4.0, utilization_share=0.9),
        counselor_scores={
            "section_utilization_balance": 10,
            "student_semester_balance": 2,
            "difficulty_balance": 2,
            "course_category_diversity": 2,
            "course_sequence_preferences": 2,
        },
    )
    decision = choose_adaptive_operator(
        state,
        ranked_students=(SimpleNamespace(student_id=7), SimpleNamespace(student_id=8)),
    )
    signals = decision.signal_values["role_signals"]
    assert all(0.0 <= value <= 1.0 for value in signals.values())
    assert decision.operator.portfolio_role == "utilization_repair"


def test_policy_decision_exposes_history_budget_and_resource_effects():
    attempt = AdaptiveOperatorAttempt(
        operator="r2",
        status="unknown",
        candidate_found=False,
        candidate_validated=False,
        adopted=False,
        gain=0,
        elapsed_seconds=7,
        unknown=True,
    )
    state = replace(
        _state(local_share=0.2, utilization_share=0.2, history=(attempt,)),
        recent_memory_peak_bytes=123456,
        recent_operation_seconds=7,
    )
    decision = choose_adaptive_operator(
        state,
        portfolio=(AdaptiveOperatorSpec("r2", 2, None, False, 0, "local_descent"),),
    )
    assert decision.signal_values["history_effect"]["attempt_count"] == 1
    assert decision.signal_values["history_effect"]["unknown_rate"] == 1.0
    assert decision.signal_values["budget_effect"]["remaining_seconds"] == 60.0
    assert decision.signal_values["resource_facts"]["recent_memory_peak_bytes"] == 123456


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


def test_frozen_policy_state_replays_same_choice_and_reasoning():
    recorded = ({
        "operator": "targeted_r8_s2",
        "status": "unknown",
        "candidate_found": False,
        "candidate_validated": False,
        "candidate_adopted": False,
        "gain": 0,
        "total_operation_seconds": 3,
    },)
    replayed_history = replay_adaptive_policy(recorded)
    state = replace(
        _state(local_share=0.8, utilization_share=0.2),
        operator_history=replayed_history,
    )
    ranked = (SimpleNamespace(student_id=7), SimpleNamespace(student_id=8))
    first = choose_adaptive_operator(state, ranked_students=ranked)
    second = choose_adaptive_operator(state, ranked_students=ranked)
    assert first.to_dict() == second.to_dict()


def test_new_policy_records_a_complete_bounded_competition_trace():
    state = _state(local_share=0.8, utilization_share=0.2)
    ranked = (SimpleNamespace(student_id=7), SimpleNamespace(student_id=8))
    decision = choose_adaptive_operator(
        state,
        ranked_students=ranked,
        adaptive_policy_variant="hierarchical_evidence",
    )

    trace = decision.signal_values["competition_trace"]
    assert trace["schema"] == "adaptive_selector_trace_v1"
    assert trace["trace_complete"] is True
    assert len(trace["candidates"]) == len(DEFAULT_ADAPTIVE_OPERATOR_PORTFOLIO)
    assert {
        row["operator"] for row in trace["candidates"]
    } == {spec.name for spec in DEFAULT_ADAPTIVE_OPERATOR_PORTFOLIO}
    assert trace["derived"]["score_winner"]["operator"] == decision.operator.name
    assert trace["derived"]["executed_winner"]["operator"] == decision.operator.name
    assert trace["derived"]["continuation_override"] is False


def test_hierarchical_evidence_keeps_exact_and_peer_strata_disjoint():
    exact = AdaptiveOperatorAttempt(
        operator="targeted_r4_s2",
        status="optimal",
        candidate_found=True,
        candidate_validated=True,
        adopted=True,
        gain=12,
        elapsed_seconds=60,
    )
    state = _state(local_share=0.8, utilization_share=0.2, history=(exact,))
    spec = next(
        item for item in DEFAULT_ADAPTIVE_OPERATOR_PORTFOLIO
        if item.name == "targeted_r4_s2"
    )
    strata = adaptive_search._disjoint_evidence_history(
        state.operator_history, spec, DEFAULT_ADAPTIVE_OPERATOR_PORTFOLIO
    )
    facts = adaptive_search._hierarchical_yield_observation(
        state.operator_history, strata
    )

    assert facts["exact"]["lifetime"]["resolved_attempt_count"] == 1
    assert facts["family_peers"]["lifetime"]["resolved_attempt_count"] == 0
    assert facts["role_peers"]["lifetime"]["resolved_attempt_count"] == 0
    assert facts["exact_hierarchical_yield"] < facts["exact"]["blended_yield"]


def test_new_policy_ladder_terms_are_deterministic_and_bounded():
    attempt = AdaptiveOperatorAttempt(
        operator="targeted_r4_s2",
        status="optimal",
        candidate_found=True,
        candidate_validated=True,
        adopted=True,
        gain=8,
        elapsed_seconds=30,
        objective_weighted_delta={
            "difficulty_balance": 4.0,
            "course_category_diversity": -1.0,
        },
    )
    state = replace(
        _state(local_share=0.7, utilization_share=0.3, history=(attempt,)),
        weighted_contributions={
            "difficulty_balance": 8.0,
            "course_category_diversity": 2.0,
        },
    )
    ranked = (SimpleNamespace(student_id=7), SimpleNamespace(student_id=8))
    for variant in (
        "hierarchical_evidence",
        "hierarchical_recent",
        "component_aware",
        "horizon_aware",
    ):
        first = build_adaptive_competition_trace(
            state,
            ranked_students=ranked,
            adaptive_policy_variant=variant,
        )
        second = build_adaptive_competition_trace(
            state,
            ranked_students=ranked,
            adaptive_policy_variant=variant,
        )
        assert first == second
        for row in first["candidates"]:
            assert -1.0 <= row["component_alignment"].get("alignment", 0.0) <= 1.0
            assert 0.0 <= row["exploration"] <= 0.20


def test_component_alignment_uses_live_v2_weighted_keys_and_improvement_direction():
    history = (
        AdaptiveOperatorAttempt(
            operator="targeted_r4_s2",
            status="optimal",
            candidate_found=True,
            candidate_validated=True,
            adopted=True,
            gain=10.0,
            elapsed_seconds=30.0,
            # Runtime telemetry is after-minus-before.
            objective_weighted_delta={"difficulty_balance": -10.0},
            # Component signatures consume before-minus-after.
            objective_improvement_weighted_delta={"difficulty_balance": 10.0},
        ),
    )
    state = replace(
        _state(local_share=0.8, utilization_share=0.2, history=history),
        weighted_contributions={
            "difficulty_balance_penalty": 10.0,
            "section_utilization_balance_penalty": 1.0,
            "course_sequence_preferences_penalty": 0.0,
        },
    )
    spec = next(
        item for item in DEFAULT_ADAPTIVE_OPERATOR_PORTFOLIO
        if item.name == "targeted_r4_s2"
    )
    facts = adaptive_search._component_alignment_observation(
        state,
        history,
        adaptive_search._disjoint_evidence_history(
            history, spec, DEFAULT_ADAPTIVE_OPERATOR_PORTFOLIO
        ),
    )
    assert facts["remaining_share"]["difficulty_balance"] > 0
    assert facts["alignment"] > 0


def test_prospective_selector_snapshot_replays_exactly_without_schedule_inference():
    state = _state(local_share=0.8, utilization_share=0.2)
    ranked = (SimpleNamespace(student_id=7), SimpleNamespace(student_id=8))
    decision = choose_adaptive_operator(
        state,
        ranked_students=ranked,
        adaptive_policy_variant="horizon_aware",
    )
    replay = replay_selector_decision(
        decision.signal_values["competition_trace"], state.operator_history
    )

    assert replay["replay_classification"] == "exact"
    assert replay["selection_matches_original_score_winner"] is True
    assert replay["replay_selected_operator"] == decision.operator.name
    assert replay["schedule_outcome_inferred"] is False


def test_legacy_selector_artifact_replay_is_partial_not_counterfactual():
    replay = replay_selector_artifact({
        "decisions": [{"operator": {"name": "targeted_r4_s2"}}],
        "attempts": [],
    })

    assert replay["results"][0]["replay_classification"] == "partial"
    assert replay["results"][0]["schedule_outcome_inferred"] is False


def test_selector_trace_and_objective_trajectory_fit_telemetry_budget():
    state = _state(local_share=0.8, utilization_share=0.2)
    ranked = (SimpleNamespace(student_id=7), SimpleNamespace(student_id=8))
    trace = build_adaptive_competition_trace(
        state,
        ranked_students=ranked,
        adaptive_policy_variant="horizon_aware",
    )
    component = {
        "raw_penalty": 10.0,
        "normalization_denominator": 100.0,
        "normalized_penalty": 0.1,
        "counselor_importance": 6,
        "weighted_normalized_contribution": 0.06,
    }
    trajectory = {
        "schema": "adaptive_objective_trajectory_v1",
        "initial": {"components": {name: dict(component) for name in (
            "section_utilization_balance",
            "student_semester_balance",
            "difficulty_balance",
            "course_category_diversity",
            "course_sequence_preferences",
        )}},
        "adopted_transitions": tuple({
            "decision_index": index,
            "before": {"components": dict(component)},
            "after": {"components": dict(component)},
            "weighted_delta": {},
        } for index in range(25)),
        "final": {"components": {name: dict(component) for name in (
            "section_utilization_balance",
            "student_semester_balance",
            "difficulty_balance",
            "course_category_diversity",
            "course_sequence_preferences",
        )}},
    }
    payload = {
        "selector_traces": tuple(trace for _ in range(25)),
        "objective_trajectory": trajectory,
    }
    assert len(json.dumps(
        payload, default=str, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")) < 1_048_576


def test_policy_selects_grade_from_opportunity_facts_after_stagnation():
    history = (
        AdaptiveOperatorAttempt(
            operator="r2", status="unknown", candidate_found=False,
            candidate_validated=False, adopted=False, gain=0,
            elapsed_seconds=5, unknown=True,
        ),
        AdaptiveOperatorAttempt(
            operator="targeted_r4_s2", status="infeasible", candidate_found=False,
            candidate_validated=False, adopted=False, gain=0,
            elapsed_seconds=5, infeasible=True,
        ),
    )
    state = replace(
        _state(local_share=0.1, utilization_share=0.1, history=history),
        grade_opportunities=(
            {"grade_level": 9, "local_pressure_total": 80,
             "utilization_pressure_share": 0.05,
             "effective_search_available": True},
            {"grade_level": 10, "local_pressure_total": 10,
             "utilization_pressure_share": 0.01,
             "effective_search_available": True},
        ),
        student_local_weighted_total=100,
        consecutive_no_improvement_attempts=2,
    )
    decision = choose_adaptive_operator(state, ranked_students=())
    assert decision.operator.name == "grade_bounded_g9"
    assert decision.operator.selected_grade == 9
    assert "specialized_search_stagnation" in decision.reasons


def test_policy_does_not_memorize_grade_number_when_opportunity_changes():
    history = tuple(
        AdaptiveOperatorAttempt(
            operator="r2", status="unknown", candidate_found=False,
            candidate_validated=False, adopted=False, gain=0,
            elapsed_seconds=5, unknown=True,
        ) for _ in range(2)
    )
    state = replace(
        _state(local_share=0.1, utilization_share=0.1, history=history),
        grade_opportunities=(
            {"grade_level": 9, "local_pressure_total": 5,
             "utilization_pressure_share": 0.01,
             "effective_search_available": True},
            {"grade_level": 12, "local_pressure_total": 90,
             "utilization_pressure_share": 0.05,
             "effective_search_available": True},
        ),
        student_local_weighted_total=100,
        consecutive_no_improvement_attempts=2,
    )
    decision = choose_adaptive_operator(state, ranked_students=())
    assert decision.operator.name == "grade_bounded_g12"


def test_policy_returns_to_local_after_validated_grade_escape():
    state = replace(
        _state(local_share=0.1, utilization_share=0.1),
        operator_history=(
            AdaptiveOperatorAttempt(
                operator="grade_bounded_g9", status="feasible",
                candidate_found=True, candidate_validated=True, adopted=True,
                gain=10, elapsed_seconds=5, selected_grade=9,
            ),
        ),
        grade_opportunities=(
            {"grade_level": 9, "local_pressure_total": 80,
             "utilization_pressure_share": 0.05,
             "effective_search_available": True},
        ),
    )
    decision = choose_adaptive_operator(
        state,
        ranked_students=(SimpleNamespace(student_id=7),),
    )
    assert decision.operator.name == "r2"
    assert "return_to_local_after_escape" in decision.reasons
    assert decision.signal_values["selected_role"] == "local_descent"


def test_stateless_policy_ignores_prior_attempt_history():
    history = (
        AdaptiveOperatorAttempt(
            operator="r2", status="unknown", candidate_found=False,
            candidate_validated=False, adopted=False, gain=0,
            elapsed_seconds=5, unknown=True,
        ),
    )
    state = _state(local_share=0.8, utilization_share=0.2, history=history)
    decision = select_stateless_role_operator(
        state,
        portfolio=(
            AdaptiveOperatorSpec("r2", 2, None, False, 0, "local_descent"),
            AdaptiveOperatorSpec("targeted_r4_s1", 4, 1, True, 1, "targeted_repair"),
        ),
        ranked_students=(SimpleNamespace(student_id=7),),
    )
    assert decision.operator.name == "targeted_r4_s1"
    assert decision.signal_values["attempt_count"] == 0


def test_stateless_policy_clears_derived_stagnation_history():
    state = replace(
        _state(local_share=0.1, utilization_share=0.1),
        consecutive_no_improvement_attempts=4,
        unknown_streak=4,
        grade_opportunities=(
            {
                "grade_level": 9,
                "local_pressure_total": 90,
                "utilization_pressure_share": 0.05,
                "effective_search_available": True,
            },
        ),
    )
    decision = select_stateless_role_operator(
        state,
        portfolio=(
            AdaptiveOperatorSpec("r2", 2, None, False, 0, "local_descent"),
            AdaptiveOperatorSpec(
                "grade_bounded_g9", None, None, False, 0, "basin_escape",
                target_policy="fixed", selected_grade=9,
            ),
        ),
    )
    assert decision.operator.name == "r2"


def test_fixed_cycle_control_is_deterministic_and_solver_free():
    state = _state(local_share=0.2, utilization_share=0.2)
    cycle = (
        AdaptiveOperatorSpec("r2", 2, None, False, 0, "local_descent"),
        AdaptiveOperatorSpec("targeted_r4_s1", 4, 1, True, 1, "targeted_repair"),
    )
    first = select_fixed_cycle_operator(
        state, cycle, ranked_students=(SimpleNamespace(student_id=7),)
    )
    second = select_fixed_cycle_operator(
        replace(state, operator_history=(AdaptiveOperatorAttempt(
            operator="r2", status="unknown", candidate_found=False,
            candidate_validated=False, adopted=False, gain=0,
            elapsed_seconds=1,
        ),)),
        cycle,
        ranked_students=(SimpleNamespace(student_id=7),),
    )
    assert first.operator.name == "r2"
    assert second.operator.name == "targeted_r4_s1"


def test_policy_stops_cleanly_when_history_exhausts_single_operator():
    exhausted = AdaptiveOperatorAttempt(
        operator="r2", status="infeasible", candidate_found=False,
        candidate_validated=False, adopted=False, gain=0,
        elapsed_seconds=1, infeasible=True,
        stopping_reason="proven_scope_exhausted",
    )
    state = _state(local_share=0.5, utilization_share=0.5, history=(exhausted,))
    assert choose_adaptive_operator(
        state,
        portfolio=(AdaptiveOperatorSpec(
            "r2", 2, None, False, 0, "local_descent"
        ),),
    ) is None


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
                "selected_student_ids": (1,),
                "probe_invocation_student_ids": (1,),
                "probe_result_scope_equal": True,
                "iterations": ({
                    "selected_student_ids": (1,),
                    "probe_invocation_student_ids": (1,),
                    "probe_result_scope_equal": True,
                },),
            },
            "stage_2": {"objective_values": (-2,), "final_source_decisions": (("a", 2),)},
        },
    )
    monkeypatch.setattr(
        "scheduling_engine.student_assignment.adaptive_runtime._operator_result",
        lambda *args, **kwargs: candidate,
    )
    monkeypatch.setattr(
        "scheduling_engine.student_assignment.adaptive_runtime._quality_report",
        lambda *_args: quality,
    )
    events = []
    result = run_adaptive_local_search_diagnostic(
        data,
        initial_result=initial,
        total_time_limit_seconds=1,
        per_operator_time_limit_seconds=0.1,
        max_iterations=1,
        portfolio=(AdaptiveOperatorSpec("targeted_r8_s1", 8, 1, True, 1, "targeted_repair"),),
        phase_callback=lambda phase, **facts: events.append((phase, facts)),
    )
    assert result.result is candidate
    assert result.record.attempts[0]["adopted"] is True
    assert result.source_decisions == (("a", 2),)
    assert result.record.final_assignment_count == 2
    assert result.record.phase_timings["initial_quality_evaluation"] >= 0
    assert result.record.phase_timings["target_preparation"] >= 0
    assert result.record.phase_timings["policy_selection"] >= 0
    assert result.record.phase_timings["operator_execution"] >= 0
    assert result.record.phase_timings["candidate_processing"] >= 0
    assert result.record.attempts[0]["actual_target_scope"] == (1,)
    assert result.record.attempts[0]["candidate_source_decision_fingerprint"] == (
        source_decision_fingerprint((("a", 2),))
    )
    assert result.record.phase_timings["finalization"] >= 0
    assert result.record.phase_timings["total"] >= 0
    decisions = [facts for phase, facts in events if phase == "policy_decision"]
    assert len(decisions) == 1
    assert decisions[0]["selected_role"] == "targeted_repair"
    assert decisions[0]["selected_operator"] == "targeted_r8_s1"
    assert "reasons" in decisions[0]
    assert "signal_values" in decisions[0]


def test_runner_executes_fixed_cycle_control_through_shared_operator_boundary(monkeypatch):
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
        }
    }
    initial = SimpleNamespace(
        status="complete",
        solver_outcome="optimal",
        unmet_requests=(),
        assignments=(1,),
        commitment_assignments=(),
        objective_components={"weighted_normalized_contributions": {"x": 100}},
        optimization_facts={
            "stage_1": {"objective_values": (-1,)},
            "stage_2": {
                "objective_values": (-1,),
                "final_source_decisions": (("a", 1),),
            },
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
            },
            "stage_2": {
                "objective_values": (-2,),
                "final_source_decisions": (("a", 2),),
            },
        },
    )
    spec = AdaptiveOperatorSpec("r2", 2, None, False, 0, "local_descent")
    decision = AdaptivePolicyDecision(
        operator=spec,
        selected_student_ids=(),
        score=0.0,
        reasons=("fixed_cycle_control",),
        signal_values={"remaining_seconds": 1},
    )
    monkeypatch.setattr(
        "scheduling_engine.student_assignment.adaptive_runtime._quality_report",
        lambda *_args: quality,
    )
    monkeypatch.setattr(
        "scheduling_engine.student_assignment.adaptive_runtime.select_fixed_cycle_operator",
        lambda *_args, **_kwargs: decision,
    )
    monkeypatch.setattr(
        "scheduling_engine.student_assignment.adaptive_runtime._operator_result",
        lambda *_args, **_kwargs: candidate,
    )

    result = run_adaptive_local_search_diagnostic(
        data,
        initial_result=initial,
        total_time_limit_seconds=1,
        per_operator_time_limit_seconds=0.1,
        max_iterations=1,
        selection_policy="fixed_cycle",
        fixed_cycle=(spec,),
    )

    assert result.record.selection_policy == "fixed_cycle"
    assert result.record.decisions[0]["selection_policy"] == "fixed_cycle"
    assert result.result is candidate
    assert result.record.attempts[0]["adopted"] is True


def test_validation_unknown_candidate_is_retried_without_becoming_interim_incumbent(
    monkeypatch,
):
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
                "candidate_validated": False,
                "validation_classification": "validation_unknown",
                "iterations": ({
                    "candidate_source_decisions": (("a", 2),),
                    "candidate_value": 90,
                },),
            },
            "stage_2": {
                "final_source_decisions": (("a", 2),),
            },
        },
    )
    retry = {}

    def fake_retry(*args, **kwargs):
        retry.update(kwargs)
        return {
            "result": candidate,
            "validated": True,
            "classification": "validated",
            "solver_outcome": "feasible",
            "elapsed_seconds": 0.01,
            "source_decision_identity_matches": True,
            "source_decision_count": 1,
            "requested_time_limit_seconds": kwargs["time_limit_seconds"],
            "cp_sat_random_seed": None,
        }

    monkeypatch.setattr(adaptive_runtime, "_quality_report", lambda *_args: quality)
    monkeypatch.setattr(adaptive_runtime, "_operator_result", lambda *_args, **_kwargs: candidate)
    monkeypatch.setattr(adaptive_runtime, "_retry_candidate_validation", fake_retry)

    result = run_adaptive_local_search_diagnostic(
        data,
        initial_result=initial,
        total_time_limit_seconds=1,
        per_operator_time_limit_seconds=0.1,
        candidate_validation_time_limit_seconds=0.8,
        max_iterations=1,
        portfolio=(AdaptiveOperatorSpec("r2", 2, None, False, 0, "local_descent"),),
    )

    assert retry["candidate_source_decisions"] == (("a", 2),)
    assert retry["time_limit_seconds"] <= 0.8
    assert result.result is candidate
    assert result.record.attempts[0]["candidate_validated"] is True
    assert result.record.attempts[0]["validation_retry_count"] == 1


def test_runner_records_validation_error_and_retains_incumbent(monkeypatch):
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
    quality = {"objective_semantics": {"components": {}}}
    initial = SimpleNamespace(
        status="complete",
        solver_outcome="optimal",
        unmet_requests=(),
        assignments=(1,),
        commitment_assignments=(),
        objective_components={"weighted_normalized_contributions": {"x": 100}},
        optimization_facts={
            "stage_1": {"objective_values": (-1,)},
            "stage_2": {
                "objective_values": (-1,),
                "final_source_decisions": (("a", 1),),
            },
            # This deliberately resembles a prior operator result. A new
            # operator failure must not reuse its inner scope as current
            # execution evidence.
            "stage_2_local_bootstrap": {
                "iterations": ({"selected_student_ids": (9,)},),
            },
        },
    )
    spec = AdaptiveOperatorSpec(
        "targeted_r4_s1", 4, 1, True, 1, "targeted_repair"
    )
    decision = AdaptivePolicyDecision(
        operator=spec,
        selected_student_ids=(7,),
        score=0.0,
        reasons=("test_operator_error",),
        signal_values={},
    )
    monkeypatch.setattr(
        "scheduling_engine.student_assignment.adaptive_runtime._quality_report",
        lambda *_args: quality,
    )
    monkeypatch.setattr(
        "scheduling_engine.student_assignment.adaptive_runtime.select_fixed_cycle_operator",
        lambda *_args, **_kwargs: decision,
    )
    monkeypatch.setattr(
        "scheduling_engine.student_assignment.adaptive_runtime._operator_result",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ValueError("The supplied mature checkpoint failed full-model validation")
        ),
    )

    result = run_adaptive_local_search_diagnostic(
        data,
        initial_result=initial,
        total_time_limit_seconds=1,
        per_operator_time_limit_seconds=0.1,
        max_iterations=1,
        portfolio=(spec,),
        selection_policy="fixed_cycle",
        fixed_cycle=(spec,),
    )

    attempt = result.record.attempts[0]
    assert result.result is initial
    assert attempt["status"] == "validation_error"
    assert attempt["validation_classification"] == "validation_error"
    assert attempt["candidate_validated"] is False
    assert attempt["scope_mismatch"] is False
    assert attempt["actual_target_scope"] is None
    assert result.record.final_assignment_count == 1


def test_ordinary_solver_does_not_call_diagnostic_adaptive_runner(monkeypatch):
    original_data, _source = _multi_attempt_operator_fixture((1,))
    data = replace(original_data, objective_semantics_version="v2")

    monkeypatch.setattr(
        "scheduling_engine.student_assignment.adaptive_runtime.run_adaptive_local_search_diagnostic",
        lambda *_args, **_kwargs: pytest.fail(
            "ordinary student assignment must not invoke adaptive diagnostics"
        ),
    )

    result = solve_student_assignment(data)

    assert result.status == "complete"
    assert not result.unmet_requests


def test_policy_portfolio_covers_all_diagnostic_operator_families():
    assert {item.name for item in DEFAULT_ADAPTIVE_OPERATOR_PORTFOLIO} == {
        "r2",
        "targeted_r4_s1",
        "targeted_r8_s1",
        "targeted_r4_s2",
        "targeted_r8_s2",
        "targeted_utilization_r16_s2",
        "targeted_utilization_r16_s4",
        "targeted_utilization_r32_s4",
        "targeted_utilization_r32_s6",
        "targeted_utilization_r64_s6",
        "targeted_utilization_r64_s8",
        "targeted_utilization_r64_s10",
        "grade_bounded_g9",
        "grade_bounded_g10",
        "grade_bounded_g11",
        "grade_bounded_g12",
    }


def test_session_config_validates_family_target_policy_and_fixed_targets():
    config = ContinuousOperatorSessionConfig(
        operator_family="targeted_r8_s2",
        target_policy="fixed",
        selected_student_ids=(7, 8),
        total_time_limit_seconds=30,
        max_attempts=2,
        per_attempt_time_limit_seconds=5,
    )
    assert config.neighborhood_radius == 8
    assert config.max_changed_students == 2
    assert config.targeted is True


def test_enforced_scope_is_explicit_and_must_match_fixed_scope():
    config = ContinuousOperatorSessionConfig(
        operator_family="targeted_utilization_r16_s2",
        target_policy="fixed",
        selected_student_ids=(8, 7),
        enforced_student_scope=(7, 8),
        total_time_limit_seconds=30,
        max_attempts=2,
        per_attempt_time_limit_seconds=5,
    )
    assert config.enforced_student_scope == (7, 8)

    with pytest.raises(ValueError, match="match enforced_student_scope"):
        ContinuousOperatorSessionConfig(
            operator_family="targeted_utilization_r16_s2",
            target_policy="fixed",
            selected_student_ids=(7, 8),
            enforced_student_scope=(8, 9),
            total_time_limit_seconds=30,
            max_attempts=2,
            per_attempt_time_limit_seconds=5,
        )


def test_enforced_scope_prevents_utilization_guidance_from_replacing_probe_scope():
    data, source = _multi_attempt_operator_fixture((1, 2, 3, 4))
    result = run_student_assignment_operator_session_diagnostic(
        data,
        operator_family="targeted_utilization_r16_s2",
        initial_source_decisions=source,
        total_time_limit_seconds=4,
        max_attempts=2,
        per_attempt_time_limit_seconds=1.5,
        worker_count=1,
        target_policy="fixed",
        selected_student_ids=(2, 1),
        enforced_student_scope=(2, 1),
        hard_feasibility_validation_time_limit_seconds=1,
        hard_feasibility_validation_worker_count=1,
        collect_resource_telemetry=False,
    )
    facts = result.optimization_facts["stage_2_local_bootstrap"]
    assert facts["enforced_student_scope"] == (1, 2)
    assert facts["scope_contract_expected_student_ids"] == (1, 2)
    assert facts["scope_boundary_trace"][0]["boundary"] == (
        "core_session_initialization"
    )
    assert facts["scope_boundary_trace"][0]["fixed_student_scope"] == (1, 2)
    assert all(
        tuple(item["enforced_student_scope"]) == (1, 2)
        and tuple(item["probe_selected_student_ids"]) == (1, 2)
        and tuple(item["probe_invocation_student_ids"]) == (1, 2)
        and item["scope_equal"] is True
        and item["scope_mismatch"] is False
        for item in facts["iterations"]
    )
    assert all(
        item["boundary"] in {
            "core_session_initialization",
            "before_target_preparation",
            "after_target_preparation",
            "probe_invocation",
            "probe_result",
        }
        for item in facts["scope_boundary_trace"]
    )


def test_core_rejects_a_probe_result_with_scope_drift(monkeypatch):
    data, source = _multi_attempt_operator_fixture((1, 2, 3, 4))
    original_probe = student_assignment_core.probe_substantive_soft_tier

    def drifted_probe(*args, **kwargs):
        result = original_probe(*args, **kwargs)
        return replace(result, selected_student_ids=(999,))

    monkeypatch.setattr(
        student_assignment_core,
        "probe_substantive_soft_tier",
        drifted_probe,
    )
    result = run_student_assignment_operator_session_diagnostic(
        data,
        operator_family="targeted_utilization_r16_s2",
        initial_source_decisions=source,
        total_time_limit_seconds=3,
        max_attempts=1,
        per_attempt_time_limit_seconds=1.5,
        worker_count=1,
        target_policy="fixed",
        selected_student_ids=(1, 2),
        enforced_student_scope=(1, 2),
        hard_feasibility_validation_time_limit_seconds=1,
        hard_feasibility_validation_worker_count=1,
        collect_resource_telemetry=False,
    )

    iteration = result.optimization_facts["stage_2_local_bootstrap"][
        "iterations"
    ][0]
    assert tuple(iteration["probe_invocation_student_ids"]) == (1, 2)
    assert tuple(iteration["probe_result_student_ids"]) == (999,)
    assert iteration["probe_result_scope_equal"] is False
    assert iteration["scope_mismatch"] is True
    assert iteration["scope_equal"] is False
    assert iteration["candidate_validated"] is False
    assert iteration["validation_classification"] == "scope_mismatch"
    assert iteration["adopted"] is False


def test_missing_selector_scope_execution_evidence_is_fail_closed(monkeypatch):
    data = replace(
        build_realistic_quality_tradeoff_fixture(),
        objective_semantics_version="v2",
    )
    initial = SimpleNamespace(
        status="complete",
        solver_outcome="optimal",
        unmet_requests=(),
        assignments=(1,),
        commitment_assignments=(),
        objective_components={"weighted_normalized_contributions": {"x": 100}},
        optimization_facts={
            "stage_2": {"final_source_decisions": (("a", 1),)},
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
                "iterations": ({
                    "selected_student_ids": (7,),
                    "candidate_source_decisions": (("a", 2),),
                    "candidate_value": 90,
                },),
            },
            "stage_2": {"final_source_decisions": (("a", 2),)},
        },
    )
    spec = AdaptiveOperatorSpec(
        "targeted_r4_s1", 4, 1, True, 1, "targeted_repair"
    )
    decision = AdaptivePolicyDecision(
        operator=spec,
        selected_student_ids=(7,),
        score=0.0,
        reasons=("test_missing_scope_evidence",),
        signal_values={},
    )
    monkeypatch.setattr(adaptive_runtime, "_quality_report", lambda *_args: {})
    monkeypatch.setattr(
        adaptive_runtime,
        "choose_adaptive_operator",
        lambda *_args, **_kwargs: decision,
    )
    monkeypatch.setattr(
        adaptive_runtime,
        "_operator_result",
        lambda *_args, **_kwargs: candidate,
    )

    result = run_adaptive_local_search_diagnostic(
        data,
        initial_result=initial,
        total_time_limit_seconds=1,
        per_operator_time_limit_seconds=0.1,
        max_iterations=1,
        portfolio=(spec,),
    )

    attempt = result.record.attempts[0]
    assert result.result is initial
    assert result.source_decisions == (("a", 1),)
    assert attempt["actual_target_scope"] is None
    assert attempt["scope_mismatch"] is True
    assert attempt["validation_classification"] == "scope_mismatch"
    assert attempt["adopted"] is False


def test_scope_mismatch_is_fail_closed_at_adaptive_authority_boundary(monkeypatch):
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
        optimization_facts={"stage_2": {"final_source_decisions": (("a", 1),)}},
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
                "iterations": ({
                    "selected_student_ids": (9,),
                    "candidate_source_decisions": (("a", 2),),
                    "candidate_value": 90,
                },),
            },
            "stage_2": {"final_source_decisions": (("a", 2),)},
        },
    )
    spec = AdaptiveOperatorSpec(
        "targeted_r4_s1", 4, 1, True, 1, "targeted_repair"
    )
    decision = AdaptivePolicyDecision(
        operator=spec,
        selected_student_ids=(7,),
        score=0.0,
        reasons=("test_scope_mismatch",),
        signal_values={},
    )
    monkeypatch.setattr(
        adaptive_runtime,
        "_quality_report",
        lambda *_args: quality,
    )
    monkeypatch.setattr(
        adaptive_runtime,
        "choose_adaptive_operator",
        lambda *_args, **_kwargs: decision,
    )
    monkeypatch.setattr(
        adaptive_runtime,
        "_operator_result",
        lambda *_args, **_kwargs: candidate,
    )

    result = run_adaptive_local_search_diagnostic(
        data,
        initial_result=initial,
        total_time_limit_seconds=1,
        per_operator_time_limit_seconds=0.1,
        max_iterations=1,
        portfolio=(spec,),
    )

    attempt = result.record.attempts[0]
    assert result.result is initial
    assert result.source_decisions == (("a", 1),)
    assert attempt["scope_mismatch"] is True
    assert attempt["validation_classification"] == "scope_mismatch"
    assert attempt["adopted"] is False


def test_all_continuous_operator_families_have_explicit_scope_contracts():
    cases = (
        ("r2", (), None, ()),
        ("targeted_r4_s1", (7,), 1, (7,)),
        ("targeted_r8_s1", (7,), 1, (7,)),
        ("targeted_r4_s2", (7, 8), 2, (7, 8)),
        ("targeted_r8_s2", (7, 8), 2, (7, 8)),
    )
    for family, fixed, count, expected in cases:
        config = ContinuousOperatorSessionConfig(
            operator_family=family,
            target_policy="fixed" if fixed else "dynamic",
            selected_student_ids=fixed,
            total_time_limit_seconds=30,
            max_attempts=2,
            per_attempt_time_limit_seconds=5,
        )
        assert config.max_changed_students == count
        assert select_operator_session_targets(
            family,
            target_policy=config.target_policy,
            ranked_student_ids=(8, 7, 9),
            fixed_student_ids=fixed,
        ) == expected


def test_dynamic_targets_are_recomputed_from_current_ranked_pressure():
    assert select_operator_session_targets(
        "targeted_r8_s2",
        target_policy="dynamic",
        ranked_student_ids=(12, 11, 10),
    ) == (11, 12)
    assert select_operator_session_targets(
        "targeted_r8_s2",
        target_policy="dynamic",
        ranked_student_ids=(3,),
    ) == (3,)


def test_adaptive_policy_exposes_a_continuous_session_request():
    decision = choose_adaptive_operator(
        _state(local_share=0.9, utilization_share=0.1),
        ranked_students=(SimpleNamespace(student_id=7), SimpleNamespace(student_id=8)),
    )
    request = build_operator_session_request(
        decision,
        remaining_seconds=17,
        worker_count=4,
    )
    assert request["operator_family"] == decision.operator.name
    assert request["allocated_time_limit_seconds"] <= 17
    assert request["max_attempts"] == decision.operator.session_max_attempts
    assert request["worker_count"] == 4
    assert request["selected_student_ids"] == decision.selected_student_ids
    assert request["enforced_student_scope"] == decision.selected_student_ids
    assert decision.to_dict()["session_request"]["operator_family"] == (
        decision.operator.name
    )


def test_session_record_is_json_safe_from_engine_facts():
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
    result = SimpleNamespace(
        optimization_facts={
            "stage_2_local_bootstrap": {
                "operator_family": "r2",
                "target_policy": "dynamic",
                "configured_session_budget_seconds": 30,
                "max_iterations": 2,
                "time_limit_seconds": 5,
                "baseline_substantive_value": 100,
                "candidate_substantive_value": 90,
                "session_elapsed_seconds": 12,
                "stopping_reason": "attempt_cap_reached",
                "iterations": ({"adopted": True, "solver_wall_time_seconds": 3, "validation_elapsed_seconds": 1},),
            }
        }
    )
    record = build_continuous_operator_session_record(
        data,
        result,
        session_id="test-session",
        seed_source_fingerprint="seed-fingerprint",
    )
    assert record.cumulative_gain == 10
    assert record.source_seed_fingerprint == "seed-fingerprint"
    assert '"operator_family":"r2"' in record.to_json()


def test_inner_probe_summary_preserves_bounded_causal_facts():
    summary = _compact_inner_probe_summary(
        {
            "iteration": 2,
            "radius": 4,
            "effective_radius": 4,
            "status": "feasible",
            "candidate_found": True,
            "candidate_complete": True,
            "candidate_validated": True,
            "adopted": True,
            "incumbent_before": 100,
            "candidate_value": 94,
            "candidate_source_decision_fingerprint": "candidate-fp",
            "elapsed_seconds": 12.0,
            "solver_wall_time_seconds": 10.0,
            "validation_elapsed_seconds": 1.5,
            "branches": 20,
            "conflicts": 3,
            "best_bound": 90,
            "changed_source_decision_count": 4,
            "changed_student_count": 2,
            "component_deltas": {"section_utilization": -6},
            "affected_student_ids": (7, 8),
            "affected_section_ids": (11,),
        },
        operator="targeted_r4_s2",
        target_scope=(7, 8),
        actual_target_scope=(7, 8),
        selected_grade=None,
    )

    assert summary["operator"] == "targeted_r4_s2"
    assert summary["candidate_validated"] is True
    assert summary["substantive_gain"] == 6.0
    assert summary["actual_target_scope"] == (7, 8)
    assert summary["scope_equal"] is True
    assert summary["component_deltas"] == {"section_utilization": -6}
    assert summary["affected_student_ids"] == (7, 8)


def test_adaptive_scope_telemetry_is_order_insensitive_for_fixed_student_sets():
    summary = _compact_inner_probe_summary(
        {"selected_student_ids": (423, 750, 1025, 1392)},
        operator="targeted_r4_s2",
        target_scope=(1392, 423, 750, 1025),
        actual_target_scope=(),
        selected_grade=None,
    )

    expected_scope = tuple(sorted((423, 750, 1025, 1392), key=repr))
    assert summary["target_scope"] == expected_scope
    assert summary["actual_target_scope"] == expected_scope
    assert summary["scope_equal"] is True


def test_continuous_r2_session_reuses_one_engine_context_and_returns_complete_result():
    base = build_realistic_quality_tradeoff_fixture()
    data = replace(
        base,
        objective_semantics_version="v2",
        objective_importance_scores={
            "section_utilization_balance": 6,
            "student_semester_balance": 6,
            "course_sequence_preferences": 6,
            "difficulty_balance": 6,
            "course_category_diversity": 6,
        },
    )
    initial = run_student_assignment_stage2_diagnostic(
        data,
        total_time_limit_seconds=5,
        hard_feasibility_time_limit_seconds=5,
        hard_feasibility_validation_time_limit_seconds=5,
        hard_feasibility_worker_count=1,
        hard_feasibility_validation_worker_count=1,
        optimization_worker_count=1,
        capture_final_source_decisions=True,
    )
    assert initial.status == "complete"
    source = initial.optimization_facts["stage_2"]["final_source_decisions"]
    result = run_student_assignment_operator_session_diagnostic(
        data,
        operator_family="r2",
        initial_source_decisions=source,
        total_time_limit_seconds=2,
        max_attempts=2,
        per_attempt_time_limit_seconds=0.5,
        worker_count=1,
        hard_feasibility_validation_time_limit_seconds=2,
        hard_feasibility_validation_worker_count=1,
        collect_resource_telemetry=False,
    )
    facts = result.optimization_facts["stage_2_local_bootstrap"]
    assert result.status == "complete"
    assert not result.unmet_requests
    assert facts["operator_session"] is True
    assert facts["session_context_reused"] is True
    assert facts["static_probe_context_built_once"] is True
    assert len(facts["iterations"]) <= 2


def test_continuous_r2_session_adopts_multiple_sequential_improvements():
    student_ids = tuple(range(1, 7))
    data = StudentAssignmentInputDTO(
        academic_year_id=1,
        requests=tuple(
            StudentAssignmentRequestDTO(
                request_id=student_id,
                student_id=student_id,
                course_id=1,
                course_offering_id=11,
                is_primary=True,
                is_mandatory=True,
                priority_tier=1,
            )
            for student_id in student_ids
        ),
        sections=(
            StudentAssignmentSectionDTO(
                section_id=1,
                delivery_group_id=1,
                member_course_offering_ids=(11,),
                member_course_ids=(1,),
                semester=1,
                timeslot_id=101,
                capacity_max=6,
                target_capacity=6,
            ),
            StudentAssignmentSectionDTO(
                section_id=2,
                delivery_group_id=1,
                member_course_offering_ids=(11,),
                member_course_ids=(1,),
                semester=1,
                timeslot_id=102,
                capacity_max=6,
                target_capacity=6,
            ),
        ),
        fixed_enrollments=(),
        hard_prerequisites=(),
        soft_sequence_preferences=(),
        section_utilization_balance_importance="important",
        student_semester_balance_importance="not_important",
        course_sequence_preferences_importance="not_important",
        difficulty_balance_importance="not_important",
        course_category_diversity_importance="not_important",
        timeslots=(
            TimeSlotDTO(101, 1, 1, "A", True),
            TimeSlotDTO(102, 1, 1, "B", True),
        ),
    )
    source = tuple(
        (
            ("course", student_id),
            (student_id, 1, None, 1, 101, None),
        )
        for student_id in student_ids
    )
    result = run_student_assignment_operator_session_diagnostic(
        data,
        operator_family="r2",
        initial_source_decisions=source,
        total_time_limit_seconds=10,
        max_attempts=3,
        per_attempt_time_limit_seconds=2,
        worker_count=1,
        hard_feasibility_validation_time_limit_seconds=2,
        hard_feasibility_validation_worker_count=1,
        collect_resource_telemetry=False,
    )
    iterations = result.optimization_facts["stage_2_local_bootstrap"]["iterations"]
    assert result.status == "complete"
    assert not result.unmet_requests
    assert [item["incumbent_before"] for item in iterations[:2]] == [6, 2]
    assert [item["candidate_value"] for item in iterations[:2]] == [2.0, 0.0]
    assert all(item["adopted"] for item in iterations[:2])
    assert result.optimization_facts["stage_2_local_bootstrap"][
        "session_context_reused"
    ] is True


def test_continuous_targeted_operator_families_reuse_context_across_improvements():
    data, source = _multi_attempt_operator_fixture((1, 2))
    cases = (
        ("targeted_r4_s1", (1,)),
        ("targeted_r8_s1", (1,)),
        ("targeted_r4_s2", (1, 2)),
        ("targeted_r8_s2", (1, 2)),
        ("targeted_utilization_r16_s2", (1, 2)),
    )
    for operator_family, selected_student_ids in cases:
        result = run_student_assignment_operator_session_diagnostic(
            data,
            operator_family=operator_family,
            initial_source_decisions=source,
            total_time_limit_seconds=8,
            max_attempts=3,
            per_attempt_time_limit_seconds=2,
            worker_count=1,
            target_policy="fixed",
            selected_student_ids=selected_student_ids,
            hard_feasibility_validation_time_limit_seconds=2,
            hard_feasibility_validation_worker_count=1,
            collect_resource_telemetry=False,
        )
        facts = result.optimization_facts["stage_2_local_bootstrap"]
        assert result.status == "complete"
        assert not result.unmet_requests
        assert facts["operator_session"] is True
        assert facts["session_context_reused"] is True
        assert facts["static_probe_context_built_once"] is True
        assert facts["improvement_adopted"] is True
        assert len(facts["session_target_history"]) == len(facts["iterations"])
        assert all(
            tuple(targets) == tuple(selected_student_ids)
            for targets in facts["session_target_history"]
        )
        assert all(
            tuple(item["selected_student_ids"]) == tuple(selected_student_ids)
            for item in facts["iterations"]
        )
        assert all(
            item["candidate_validated"]
            for item in facts["iterations"]
            if item["adopted"]
        )


def test_utilization_cluster_session_records_guidance_and_effective_radius():
    data, source = _multi_attempt_operator_fixture((1, 2, 3, 4, 5, 6))
    result = run_student_assignment_operator_session_diagnostic(
        data,
        operator_family="targeted_utilization_r16_s2",
        initial_source_decisions=source,
        total_time_limit_seconds=8,
        max_attempts=1,
        per_attempt_time_limit_seconds=2,
        worker_count=1,
        target_policy="dynamic",
        hard_feasibility_validation_time_limit_seconds=2,
        hard_feasibility_validation_worker_count=1,
        collect_resource_telemetry=False,
    )
    facts = result.optimization_facts["stage_2_local_bootstrap"]
    assert result.status == "complete"
    assert len(facts["session_target_history"]) == 1
    assert len(facts["session_target_history"][0]) == 2
    guidance = facts["session_target_guidance"][0]
    assert guidance["guidance_only"] is True
    assert guidance["objective_attribution"] is False
    assert facts["effective_neighborhood_radius"] <= 16
    assert facts["eligible_targeted_source_decision_count"] > 0


def test_utilization_cluster_operator_ladder_has_explicit_radius_and_scope_caps():
    expected = {
        "targeted_utilization_r16_s2": (16, 2),
        "targeted_utilization_r16_s4": (16, 4),
        "targeted_utilization_r32_s4": (32, 4),
        "targeted_utilization_r32_s6": (32, 6),
        "targeted_utilization_r64_s6": (64, 6),
        "targeted_utilization_r64_s8": (64, 8),
        "targeted_utilization_r64_s10": (64, 10),
    }
    for family, (radius, scope_size) in expected.items():
        config = ContinuousOperatorSessionConfig(operator_family=family)
        assert config.neighborhood_radius == radius
        assert config.max_changed_students == scope_size
        assert operator_session_target_count(family) == scope_size


def test_r32_utilization_cluster_keeps_multi_student_candidate_within_scope():
    data, source = _multi_attempt_operator_fixture((1, 2, 3, 4, 5, 6))
    result = run_student_assignment_operator_session_diagnostic(
        data,
        operator_family="targeted_utilization_r32_s4",
        initial_source_decisions=source,
        total_time_limit_seconds=8,
        max_attempts=1,
        per_attempt_time_limit_seconds=2,
        worker_count=1,
        target_policy="dynamic",
        hard_feasibility_validation_time_limit_seconds=2,
        hard_feasibility_validation_worker_count=1,
        collect_resource_telemetry=False,
    )
    facts = result.optimization_facts["stage_2_local_bootstrap"]
    assert result.status == "complete"
    assert len(facts["session_target_history"][0]) == 4
    iteration = facts["iterations"][0]
    assert iteration["candidate_validated"] is True
    assert iteration["changed_student_count"] <= 4
    assert iteration["changed_source_decision_count"] <= 32
    assert set(iteration["affected_student_ids"]).issubset(
        set(facts["session_target_history"][0])
    )


def test_continuous_targeted_session_retargets_after_adoption(monkeypatch):
    data, source = _multi_attempt_operator_fixture((1, 2))
    ranking_calls = []

    def ranked_students(_data, _quality):
        ranking_calls.append(True)
        student_id = 1 if len(ranking_calls) == 1 else 2
        return (SimpleNamespace(student_id=student_id),)

    monkeypatch.setattr(
        "scheduling_engine.student_assignment.core.rank_students_by_quality_pressure",
        ranked_students,
    )
    result = run_student_assignment_operator_session_diagnostic(
        data,
        operator_family="targeted_r4_s1",
        initial_source_decisions=source,
        total_time_limit_seconds=8,
        max_attempts=2,
        per_attempt_time_limit_seconds=2,
        worker_count=1,
        target_policy="dynamic",
        hard_feasibility_validation_time_limit_seconds=2,
        hard_feasibility_validation_worker_count=1,
        collect_resource_telemetry=False,
    )
    facts = result.optimization_facts["stage_2_local_bootstrap"]
    assert result.status == "complete"
    assert facts["improvement_adopted"] is True
    assert facts["session_target_history"][:2] == ((1,), (2,))
    assert len(ranking_calls) >= 2


def test_continuous_dynamic_two_student_session_retargets_after_adoption(monkeypatch):
    data, source = _multi_attempt_operator_fixture((1, 2, 3))
    ranking_calls = []
    ranked_pairs = ((1, 2), (2, 3))

    def ranked_students(_data, _quality):
        pair = ranked_pairs[min(len(ranking_calls), len(ranked_pairs) - 1)]
        ranking_calls.append(True)
        return tuple(SimpleNamespace(student_id=student_id) for student_id in pair)

    monkeypatch.setattr(
        "scheduling_engine.student_assignment.core.rank_students_by_quality_pressure",
        ranked_students,
    )
    result = run_student_assignment_operator_session_diagnostic(
        data,
        operator_family="targeted_r4_s2",
        initial_source_decisions=source,
        total_time_limit_seconds=8,
        max_attempts=2,
        per_attempt_time_limit_seconds=2,
        worker_count=1,
        target_policy="dynamic",
        hard_feasibility_validation_time_limit_seconds=2,
        hard_feasibility_validation_worker_count=1,
        collect_resource_telemetry=False,
    )
    facts = result.optimization_facts["stage_2_local_bootstrap"]
    assert result.status == "complete"
    assert facts["improvement_adopted"] is True
    assert facts["session_target_history"][:2] == ((1, 2), (2, 3))
    assert len(ranking_calls) >= 2


def test_fixed_targeted_session_does_not_leak_attempt_target_state():
    data, source = _multi_attempt_operator_fixture((1, 2))
    result = run_student_assignment_operator_session_diagnostic(
        data,
        operator_family="targeted_r4_s1",
        initial_source_decisions=source,
        total_time_limit_seconds=8,
        max_attempts=2,
        per_attempt_time_limit_seconds=2,
        worker_count=1,
        target_policy="fixed",
        selected_student_ids=(2,),
        hard_feasibility_validation_time_limit_seconds=2,
        hard_feasibility_validation_worker_count=1,
        collect_resource_telemetry=False,
    )
    facts = result.optimization_facts["stage_2_local_bootstrap"]
    assert facts["session_target_history"][:2] == ((2,), (2,))


def test_operator_session_keeps_unknown_distinct_from_proven_scope_exhaustion():
    data, source = _multi_attempt_operator_fixture((1, 2))
    unknown = run_student_assignment_operator_session_diagnostic(
        data,
        operator_family="targeted_r8_s1",
        initial_source_decisions=source,
        total_time_limit_seconds=4,
        max_attempts=1,
        per_attempt_time_limit_seconds=0.001,
        worker_count=1,
        target_policy="fixed",
        selected_student_ids=(1,),
        hard_feasibility_validation_time_limit_seconds=1,
        hard_feasibility_validation_worker_count=1,
        candidate_validation_time_limit_seconds=1,
        collect_resource_telemetry=False,
    )
    unknown_facts = unknown.optimization_facts["stage_2_local_bootstrap"]
    assert unknown.status == "complete"
    assert unknown_facts["iterations"][0]["status"] == "unknown"
    assert unknown_facts["stopping_reason"] == "unresolved_unknown"
    assert unknown_facts["candidate_validated"] is False
    assert unknown_facts["iterations"][0]["validation_classification"] == (
        "not_attempted"
    )
    assert unknown_facts["iterations"][0]["validation_elapsed_seconds"] == 0.0
    assert unknown_facts["iterations"][0]["validation_requested_time_limit_seconds"] is None

    balanced_source = tuple(
        (
            key,
            (
                value[0],
                value[1] + 1 if value[0] == 2 else value[1],
                value[2],
                value[3],
                value[4],
                value[5],
            ),
        )
        for key, value in source
    )
    infeasible = run_student_assignment_operator_session_diagnostic(
        data,
        operator_family="r2",
        initial_source_decisions=balanced_source,
        total_time_limit_seconds=4,
        max_attempts=1,
        per_attempt_time_limit_seconds=1,
        worker_count=1,
        hard_feasibility_validation_time_limit_seconds=1,
        hard_feasibility_validation_worker_count=1,
        collect_resource_telemetry=False,
    )
    infeasible_facts = infeasible.optimization_facts["stage_2_local_bootstrap"]
    assert infeasible.status == "complete"
    assert infeasible_facts["iterations"][0]["status"] == "infeasible"
    assert infeasible_facts["stopping_reason"] == "proven_scope_exhausted"


def test_lazy_prepared_context_does_not_activate_without_a_candidate():
    data, source = _multi_attempt_operator_fixture((1, 2))
    result = run_student_assignment_operator_session_diagnostic(
        data,
        operator_family="targeted_r8_s1",
        initial_source_decisions=source,
        total_time_limit_seconds=4,
        max_attempts=1,
        per_attempt_time_limit_seconds=0.001,
        worker_count=1,
        target_policy="fixed",
        selected_student_ids=(1,),
        hard_feasibility_validation_time_limit_seconds=1,
        hard_feasibility_validation_worker_count=1,
        prepared_validation_strategy="after_first_validated_candidate",
        collect_resource_telemetry=False,
    )

    facts = result.optimization_facts["stage_2_local_bootstrap"]
    assert facts["candidate_validated"] is False
    assert facts["prepared_validation_strategy"] == (
        "after_first_validated_candidate"
    )
    assert facts["prepared_context_activation_count"] == 0
    assert facts["prepared_context_creation_seconds"] == 0.0


def test_measured_lazy_prepared_context_does_not_activate_without_a_candidate():
    data, source = _multi_attempt_operator_fixture((1, 2))
    result = run_student_assignment_operator_session_diagnostic(
        data,
        operator_family="targeted_r8_s1",
        initial_source_decisions=source,
        total_time_limit_seconds=4,
        max_attempts=1,
        per_attempt_time_limit_seconds=0.001,
        worker_count=1,
        target_policy="fixed",
        selected_student_ids=(1,),
        hard_feasibility_validation_time_limit_seconds=1,
        hard_feasibility_validation_worker_count=1,
        prepared_validation_strategy="measured_lazy",
        prepared_validation_estimate_seconds=0.5,
        prepared_context_creation_estimate_seconds=0.1,
        collect_resource_telemetry=False,
    )

    facts = result.optimization_facts["stage_2_local_bootstrap"]
    assert facts["prepared_context_activation_count"] == 0
    assert facts["prepared_context_creation_seconds"] == 0.0
    assert facts["prepared_context_activation_decisions"][0]["reason"] == (
        "zero_observed_candidate_rate"
    )


def test_lazy_prepared_context_activates_after_first_validated_candidate():
    data, source = _multi_attempt_operator_fixture((1, 2, 3))
    result = run_student_assignment_operator_session_diagnostic(
        data,
        operator_family="r2",
        initial_source_decisions=source,
        total_time_limit_seconds=8,
        max_attempts=2,
        per_attempt_time_limit_seconds=2,
        worker_count=1,
        hard_feasibility_validation_time_limit_seconds=2,
        hard_feasibility_validation_worker_count=1,
        prepared_validation_strategy="after_first_validated_candidate",
        collect_resource_telemetry=False,
    )

    facts = result.optimization_facts["stage_2_local_bootstrap"]
    assert facts["prepared_validation_strategy"] == (
        "after_first_validated_candidate"
    )
    assert facts["prepared_context_activation_count"] == 1
    assert facts["prepared_context_creation_seconds"] > 0
    iterations = facts["iterations"]
    assert iterations[0]["validation_telemetry"]["prepared_context"]["used"] is False
    if len(iterations) > 1:
        assert iterations[1]["validation_telemetry"]["prepared_context"]["used"] is True


@pytest.mark.parametrize(
    ("kwargs", "activated", "reason"),
    (
        (
            {
                "completed_attempts": 1,
                "validated_candidates": 1,
                "remaining_attempts": 4,
                "ordinary_validation_estimate_seconds": 4.0,
                "prepared_validation_estimate_seconds": 1.0,
                "context_creation_estimate_seconds": 2.0,
                "remaining_time_seconds": 30.0,
            },
            True,
            "predicted_savings_exceed_safe_break_even",
        ),
        (
            {
                "completed_attempts": 1,
                "validated_candidates": 1,
                "remaining_attempts": 1,
                "ordinary_validation_estimate_seconds": 3.0,
                "prepared_validation_estimate_seconds": 2.5,
                "context_creation_estimate_seconds": 2.0,
                "remaining_time_seconds": 30.0,
            },
            False,
            "predicted_savings_below_safe_break_even",
        ),
        (
            {
                "completed_attempts": 2,
                "validated_candidates": 0,
                "remaining_attempts": 4,
                "ordinary_validation_estimate_seconds": 4.0,
                "prepared_validation_estimate_seconds": 1.0,
                "context_creation_estimate_seconds": 2.0,
                "remaining_time_seconds": 30.0,
            },
            False,
            "zero_observed_candidate_rate",
        ),
    ),
)
def test_measured_lazy_prepared_context_decision_is_conservative(
    kwargs, activated, reason
):
    decision = measured_lazy_prepared_context_decision(**kwargs)
    assert decision["activated"] is activated
    assert decision["reason"] == reason


def test_measured_lazy_prepared_context_requires_timing_estimates():
    decision = measured_lazy_prepared_context_decision(
        completed_attempts=1,
        validated_candidates=1,
        remaining_attempts=10,
        ordinary_validation_estimate_seconds=4.0,
        prepared_validation_estimate_seconds=None,
        context_creation_estimate_seconds=2.0,
    )
    assert decision["activated"] is False
    assert decision["reason"] == "missing_timing_estimate"


def test_measured_lazy_prepared_context_respects_remaining_time():
    decision = measured_lazy_prepared_context_decision(
        completed_attempts=1,
        validated_candidates=1,
        remaining_attempts=10,
        ordinary_validation_estimate_seconds=4.0,
        prepared_validation_estimate_seconds=1.0,
        context_creation_estimate_seconds=2.0,
        remaining_time_seconds=1.0,
    )
    assert decision["activated"] is False
    assert decision["reason"] == "insufficient_remaining_time_for_context"
