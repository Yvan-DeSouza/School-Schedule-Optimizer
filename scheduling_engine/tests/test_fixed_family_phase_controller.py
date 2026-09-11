from types import SimpleNamespace

import pytest

from scheduling_engine.student_assignment import adaptive_runtime
from scheduling_engine.student_assignment.adaptive_runtime import (
    AdaptiveSessionResult,
    FixedFamilyPhase,
    FixedFamilyPhaseController,
)
from scheduling_engine.student_assignment.adaptive_search import (
    AdaptiveOperatorAttempt,
    AdaptivePolicyDecision,
)
from scheduling_engine.student_assignment.search_experiments import (
    source_decision_fingerprint,
)
from scheduling_engine.benchmark_r16_direct_ia_vs_top_90m import source_diff


def _result(source):
    return SimpleNamespace(
        status="complete",
        unmet_requests=(),
        assignments=(),
        commitment_assignments=(),
        objective_components={
            "weighted_normalized_contributions": {
                "difficulty_balance_penalty": 10.0,
            }
        },
        optimization_facts={
            "stage_2": {
                "objective_values": (10.0,),
                "final_source_decisions": tuple(source),
            }
        },
        solver_outcome="feasible",
    )


def _data():
    return SimpleNamespace(
        objective_semantics_version="v2",
        objective_importance_scores={"difficulty_balance": 6},
    )


def _state():
    return SimpleNamespace(
        remaining_seconds=300.0,
        operator_history=(),
        utilization_ranked_student_ids=(1, 2, 3, 4),
        to_dict=lambda: {"schema": "adaptive_selector_state_v1"},
    )


def _patch_controller_runtime(monkeypatch, *, late=False, clock=None):
    clock = clock if clock is not None else [0.0]
    monkeypatch.setattr(
        adaptive_runtime,
        "_quality_report",
        lambda data, result: {},
    )
    monkeypatch.setattr(
        adaptive_runtime,
        "rank_students_by_quality_pressure",
        lambda data, quality: tuple(
            SimpleNamespace(student_id=value) for value in (1, 2, 3, 4)
        ),
    )
    monkeypatch.setattr(
        adaptive_runtime,
        "build_adaptive_search_state",
        lambda *args, **kwargs: _state(),
    )
    monkeypatch.setattr(
        adaptive_runtime,
        "build_adaptive_competition_trace",
        lambda state, **kwargs: {
            "schema": "adaptive_selector_trace_v1",
            "trace_complete": True,
            "derived": {"score_winner": None},
        },
    )
    monkeypatch.setattr(
        adaptive_runtime,
        "select_fixed_cycle_operator",
        lambda state, cycle, *, ranked_students: AdaptivePolicyDecision(
            operator=cycle[0],
            selected_student_ids=(1, 2),
            score=0.0,
            reasons=("fixed_family_test",),
            signal_values={},
        ),
    )

    calls = []

    def fake_run(data, **kwargs):
        calls.append(kwargs)
        source_before = tuple(kwargs["initial_source_decisions"])
        next_source = (("decision", len(calls)),)
        attempt = AdaptiveOperatorAttempt(
            operator=kwargs["fixed_cycle"][0].name,
            status="optimal",
            candidate_found=True,
            candidate_validated=True,
            adopted=True,
            gain=1.0,
            elapsed_seconds=1.0,
            validation_classification="validated",
            target_scope=(1, 2),
            actual_target_scope=(1, 2),
            source_fingerprint_before=source_decision_fingerprint(source_before),
            candidate_source_decision_fingerprint=source_decision_fingerprint(
                next_source
            ),
            exhaustion_classification="PRODUCTIVE",
            operator_family="targeted_r4",
        )
        if late:
            clock[0] = 20.0
        return AdaptiveSessionResult(
            record=SimpleNamespace(),
            result=_result(next_source),
            source_decisions=next_source,
            history=tuple(kwargs["initial_history"]) + (attempt,),
            trusted_branch_context=SimpleNamespace(
                authority="canonical_full_model_validation"
            ),
        )

    monkeypatch.setattr(
        adaptive_runtime,
        "run_adaptive_local_search_diagnostic",
        fake_run,
    )
    return calls


def _controller(monkeypatch, clock, *, phases, late=False):
    _patch_controller_runtime(monkeypatch, late=late, clock=clock)
    source = (("initial", 0),)
    return FixedFamilyPhaseController(
        _data(),
        initial_result=_result(source),
        initial_source_decisions=source,
        phases=phases,
        worker_count=8,
        clock=lambda: clock[0],
    )


def test_fixed_phase_plan_requires_contiguous_phases():
    with pytest.raises(ValueError, match="contiguous"):
        FixedFamilyPhaseController(
            _data(),
            initial_result=_result((("initial", 0),)),
            phases=(
                FixedFamilyPhase("r4", "targeted_r4_s2", 0, 10),
                FixedFamilyPhase("r16", "targeted_utilization_r16_s4", 11, 20),
            ),
        )


def test_fixed_phase_controller_carries_context_and_refreshes_source(monkeypatch):
    clock = [0.0]
    calls = _patch_controller_runtime(monkeypatch)
    controller = FixedFamilyPhaseController(
        _data(),
        initial_result=_result((("initial", 0),)),
        initial_source_decisions=(("initial", 0),),
        phases=(FixedFamilyPhase("r4", "targeted_r4_s2", 0, 10),),
        clock=lambda: clock[0],
    )

    event = controller.run_next_attempt()

    assert event.event_type == "attempt_completed"
    assert event.attempt["adopted"] is True
    assert controller.trusted_branch_context.authority == (
        "canonical_full_model_validation"
    )
    assert controller.source_decisions == (("decision", 1),)
    assert calls[0]["use_trusted_branch_context"] is True
    assert calls[0]["initial_trusted_branch_context"] is None

    clock[0] = 1.0
    second = controller.run_next_attempt()

    assert second.event_type == "attempt_completed"
    assert calls[1]["initial_trusted_branch_context"] is not None


def test_fixed_phase_controller_blocks_resolved_duplicate_scope(monkeypatch):
    clock = [0.0]
    _patch_controller_runtime(monkeypatch)
    controller = FixedFamilyPhaseController(
        _data(),
        initial_result=_result((("initial", 0),)),
        initial_source_decisions=(("initial", 0),),
        phases=(FixedFamilyPhase("r4", "targeted_r4_s2", 0, 10),),
        clock=lambda: clock[0],
    )
    controller._history.append(
        AdaptiveOperatorAttempt(
            operator="targeted_r4_s2",
            status="optimal",
            candidate_found=True,
            candidate_validated=True,
            adopted=False,
            gain=0.0,
            elapsed_seconds=1.0,
            validation_classification="validated",
            target_scope=(1, 2),
            actual_target_scope=(1, 2),
            source_fingerprint_before=source_decision_fingerprint(
                (("initial", 0),)
            ),
            exhaustion_classification="OPERATOR_NON_IMPROVING",
        )
    )

    event = controller.run_next_attempt()

    assert event.event_type == "phase_blocked"
    assert event.blocked_reason == "scope_non_improving"
    assert controller.run_next_attempt() is None


def test_fixed_phase_controller_discards_late_candidate_and_context(monkeypatch):
    clock = [0.0]
    controller = _controller(
        monkeypatch,
        clock,
        phases=(FixedFamilyPhase("r4", "targeted_r4_s2", 0, 10),),
        late=True,
    )
    event = controller.run_next_attempt()

    assert event.event_type == "boundary_truncated_attempt"
    assert event.attempt["adopted"] is False
    assert controller.source_decisions == (("initial", 0),)
    assert controller.trusted_branch_context is None


def test_fixed_phase_controller_switches_operator_at_contiguous_boundary(monkeypatch):
    clock = [0.0]
    calls = _patch_controller_runtime(monkeypatch, clock=clock)
    controller = FixedFamilyPhaseController(
        _data(),
        initial_result=_result((("initial", 0),)),
        initial_source_decisions=(("initial", 0),),
        phases=(
            FixedFamilyPhase("r4", "targeted_r4_s2", 0, 10),
            FixedFamilyPhase("r16", "targeted_utilization_r16_s4", 10, 20),
        ),
        clock=lambda: clock[0],
    )

    first = controller.run_next_attempt()
    assert first.phase_id == "r4"
    clock[0] = 10.0
    second = controller.run_next_attempt()

    assert second.phase_id == "r16"
    assert second.operator == "targeted_utilization_r16_s4"
    assert calls[1]["fixed_cycle"][0].name == "targeted_utilization_r16_s4"


def test_targeting_callback_is_observational_and_attached_to_event(monkeypatch):
    clock = [0.0]
    snapshots = []
    _patch_controller_runtime(monkeypatch, clock=clock)
    monkeypatch.setattr(
        FixedFamilyPhaseController,
        "_targeting_snapshot",
        lambda self, **kwargs: {
            "schema": "adaptive_targeting_snapshot_v1",
            "selected_student_ids": [1, 2],
        },
    )
    controller = FixedFamilyPhaseController(
        _data(),
        initial_result=_result((("initial", 0),)),
        initial_source_decisions=(("initial", 0),),
        phases=(FixedFamilyPhase("r4", "targeted_r4_s2", 0, 10),),
        clock=lambda: clock[0],
        targeting_snapshot_callback=lambda payload: snapshots.append(payload) or {
            "path": "targeting/attempt_0001_targeting.json.gz",
            "sha256": "snapshot-hash",
        },
    )

    event = controller.run_next_attempt()

    assert snapshots[0]["selected_student_ids"] == [1, 2]
    assert event.targeting_snapshot["sha256"] == "snapshot-hash"


def test_fixed_family_controller_passes_research_search_semantics(monkeypatch):
    clock = [100.0]
    calls = _patch_controller_runtime(monkeypatch, clock=clock)
    controller = FixedFamilyPhaseController(
        _data(),
        initial_result=_result((("initial", 0),)),
        initial_source_decisions=(("initial", 0),),
        phases=(FixedFamilyPhase("r16", "targeted_utilization_r16_s4", 0, 10),),
        clock=lambda: clock[0],
        started_at=100.0,
        search_semantics="direct_exact_v2_optimization",
    )

    event = controller.run_next_attempt()

    assert event.event_type == "attempt_completed"
    assert calls[0]["search_semantics"] == "direct_exact_v2_optimization"


def test_fixed_family_controller_started_at_includes_bootstrap_time(monkeypatch):
    clock = [105.0]
    _patch_controller_runtime(monkeypatch, clock=clock)
    controller = FixedFamilyPhaseController(
        _data(),
        initial_result=_result((("initial", 0),)),
        initial_source_decisions=(("initial", 0),),
        phases=(FixedFamilyPhase("r16", "targeted_utilization_r16_s4", 0, 10),),
        clock=lambda: clock[0],
        started_at=100.0,
    )

    assert controller.elapsed_seconds == 5.0


def test_dynamic_target_study_source_diff_accepts_pair_sequences():
    changed, students = source_diff(
        ((("course", 1), (7, 10)),),
        ((("course", 1), (7, 11)), (("course", 2), (8, 12))),
    )

    assert len(changed) == 2
    assert students == [7, 8]
