from dataclasses import replace
from types import SimpleNamespace

from scheduling_engine.realistic_student_assignment_validation import (
    build_realistic_quality_tradeoff_fixture,
)
from scheduling_engine.dto import (
    StudentAssignmentInputDTO,
    StudentAssignmentRequestDTO,
    StudentAssignmentSectionDTO,
    TimeSlotDTO,
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
    build_operator_session_request,
    choose_adaptive_operator,
    replay_adaptive_policy,
)
from scheduling_engine.student_assignment.core import (
    run_student_assignment_operator_session_diagnostic,
    run_student_assignment_stage2_diagnostic,
)
from scheduling_engine.student_assignment.operator_session import (
    ContinuousOperatorSessionConfig,
    build_continuous_operator_session_record,
    select_operator_session_targets,
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
            item["candidate_validated"]
            for item in facts["iterations"]
            if item["adopted"]
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
        collect_resource_telemetry=False,
    )
    unknown_facts = unknown.optimization_facts["stage_2_local_bootstrap"]
    assert unknown.status == "complete"
    assert unknown_facts["iterations"][0]["status"] == "unknown"
    assert unknown_facts["stopping_reason"] == "unresolved_unknown"
    assert unknown_facts["candidate_validated"] is False

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
