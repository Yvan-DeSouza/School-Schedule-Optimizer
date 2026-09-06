import pytest

from scheduling_engine.student_assignment.core import (
    run_student_assignment_operator_session_diagnostic,
)
from scheduling_engine.student_assignment.operator_session import (
    ValidatedStudentAssignmentBranchContext,
)
from scheduling_engine.tests.test_adaptive_search import _multi_attempt_operator_fixture


def _session_kwargs(data, source):
    return dict(
        operator_family="targeted_r4_s2",
        target_policy="fixed",
        selected_student_ids=(1, 2),
        enforced_student_scope=(1, 2),
        initial_source_decisions=source,
        total_time_limit_seconds=4,
        max_attempts=1,
        per_attempt_time_limit_seconds=1,
        worker_count=1,
        hard_feasibility_validation_time_limit_seconds=2,
        hard_feasibility_validation_worker_count=1,
        collect_resource_telemetry=False,
    )


def test_trusted_branch_context_is_created_only_after_canonical_validation():
    data, source = _multi_attempt_operator_fixture((1, 2))
    captured = []
    result = run_student_assignment_operator_session_diagnostic(
        data,
        **_session_kwargs(data, source),
        _validated_branch_context_callback=captured.append,
    )

    assert result.status == "complete"
    assert captured
    context = captured[-1]
    assert context.authority == "canonical_full_model_validator"
    final_source = tuple(
        result.optimization_facts["stage_2"]["final_source_decisions"]
    )
    assert context.source_decisions == tuple(
        sorted(dict(final_source).items(), key=repr)
    )
    assert context.validated_solver is not None


def test_trusted_branch_context_requires_matching_model_lineage_and_preserves_validation():
    data, source = _multi_attempt_operator_fixture((1, 2))
    captured = []
    first = run_student_assignment_operator_session_diagnostic(
        data,
        **_session_kwargs(data, source),
        _validated_branch_context_callback=captured.append,
    )
    assert first.status == "complete"
    context = captured[-1]
    source = tuple(first.optimization_facts["stage_2"]["final_source_decisions"])

    second = run_student_assignment_operator_session_diagnostic(
        data,
        **_session_kwargs(data, source),
        _trusted_branch_context=context,
    )
    assert second.status == "complete"
    stage_1 = dict(
        (second.optimization_facts.get("stage_1") or {}).get("timings") or {}
    )
    assert stage_1["mature_seed_validation_reused_trusted_context"] is True


def test_trusted_branch_context_rejects_arbitrary_source_identity():
    data, source = _multi_attempt_operator_fixture((1, 2))
    captured = []
    first = run_student_assignment_operator_session_diagnostic(
        data,
        **_session_kwargs(data, source),
        _validated_branch_context_callback=captured.append,
    )
    context = captured[-1]
    assert first.status == "complete"
    with pytest.raises(ValueError, match="does not match supplied source"):
        run_student_assignment_operator_session_diagnostic(
            data,
            **_session_kwargs(data, source),
            _trusted_branch_context=context,
        )


def test_trusted_branch_context_cannot_be_constructed_without_internal_validation_token():
    with pytest.raises(ValueError, match="requires canonical validation"):
        ValidatedStudentAssignmentBranchContext._from_canonical_validation(
            validation_token=object()
        )
