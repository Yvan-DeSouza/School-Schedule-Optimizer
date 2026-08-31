from types import SimpleNamespace

from ortools.sat.python import cp_model

from scheduling_engine.tests.test_validation_benchmark import _data, _source_decisions
from scheduling_engine.student_assignment import validation_qualification
from scheduling_engine.student_assignment.solver import (
    model_proto_fingerprint,
    prepare_validation_context,
    validate_source_decision_candidate_with_status,
)


def _fake_candidate(model, source, auxiliary):
    return SimpleNamespace(
        complete_candidate_found=True,
        candidate_base_model_variable_values={
            source.Index(): 1,
            auxiliary.Index(): 2,
        },
        candidate_base_model_fingerprint=model_proto_fingerprint(model),
        candidate_base_model_witness_error=None,
        candidate_source_variable_values={source.Index(): 1},
        candidate_source_decisions=(
            (("course", 1), (1, 1, None, 1, 101, None)),
        ),
        changed_source_decision_count=0,
        changed_student_count=0,
        candidate_substantive_value=0.0,
        candidate_objective_vector=(0.0,),
        candidate_component_values={},
        component_deltas={},
        candidate_assignment_count=1,
        candidate_summary={
            "assigned_request_count": 1,
            "special_commitment_count": 0,
            "fulfillment_complete": True,
        },
        candidate_quality_summary={},
    )


def test_paired_trial_uses_one_candidate_for_both_validators(monkeypatch):
    data = _data()
    source_decisions = _source_decisions(data)
    model = cp_model.CpModel()
    source = model.NewBoolVar("enroll_1_1")
    auxiliary = model.NewIntVar(0, 2, "derived_auxiliary")
    model.Add(auxiliary == source + 1)
    candidate = _fake_candidate(model, source, auxiliary)

    def fake_operator(_data, **kwargs):
        kwargs["candidate_capture_callback"](
            model=model,
            required_decision_groups=((source,),),
            local_result=candidate,
        )
        return SimpleNamespace(
            status="partial",
            solver_outcome="unknown",
            optimization_facts={
                "stage_2_local_bootstrap": {"stopping_reason": "diagnostic"}
            },
        )

    monkeypatch.setattr(
        "scheduling_engine.student_assignment.core.run_student_assignment_operator_session_diagnostic",
        fake_operator,
    )

    record = validation_qualification.run_paired_validation_trial(
        data,
        initial_source_decisions=source_decisions,
        validation_order="witness_first",
        selected_student_ids=(1, 2),
        operator_worker_count=1,
        validation_time_limit_seconds=2.0,
        collect_resource_telemetry=False,
    )

    assert record["valid"] is True
    assert record["validation_order"] == "witness_first"
    assert record["candidate"]["candidate_source_fingerprint"]
    assert record["ordinary"]["classification"] == "validated"
    assert record["witness"]["classification"] == "validated"
    assert record["classification_parity"] is True
    assert record["false_acceptance"] is False
    assert record["base_model_variable_count"] == 2
    assert record["witness"]["witness"]["fixed_variable_count"] == 2


def test_qualification_record_is_compact_and_hashed(tmp_path):
    record = {
        "schema": validation_qualification.VALIDATION_QUALIFICATION_SCHEMA,
        "durable_witness_values": False,
        "candidate": {"candidate_source_fingerprint": "candidate"},
    }
    path = tmp_path / "pair.json"
    written = validation_qualification.write_validation_qualification_record(
        path, record
    )
    assert written["artifact_hash"]
    assert "candidate_base_model_variable_values" not in path.read_text()


def test_differential_corpus_is_fail_closed_for_invalid_or_stale_witnesses():
    model = cp_model.CpModel()
    source = model.NewBoolVar("enroll_1_1")
    auxiliary = model.NewIntVar(0, 2, "derived_auxiliary")
    model.Add(auxiliary == source + 1)
    groups = ((source,),)
    valid_source = {source.Index(): 1}
    valid_witness = {source.Index(): 1, auxiliary.Index(): 2}

    valid = validation_qualification.compare_validation_classifications(
        model,
        groups,
        valid_source,
        valid_witness,
        expected_model_fingerprint=model_proto_fingerprint(model),
    )
    assert valid["classification_parity"] is True
    assert valid["false_acceptance"] is False

    hard_invalid = validation_qualification.compare_validation_classifications(
        model,
        groups,
        {source.Index(): 0},
        {source.Index(): 0, auxiliary.Index(): 1},
        expected_model_fingerprint=model_proto_fingerprint(model),
    )
    assert hard_invalid["ordinary"]["classification"] == "hard_invalid"
    assert hard_invalid["witness"]["classification"] == "hard_invalid"
    assert hard_invalid["false_acceptance"] is False

    altered = validation_qualification.compare_validation_classifications(
        model,
        groups,
        valid_source,
        {source.Index(): 1, auxiliary.Index(): 0},
        expected_model_fingerprint=model_proto_fingerprint(model),
    )
    assert altered["ordinary"]["classification"] == "validated"
    assert altered["witness"]["classification"] == "hard_invalid"
    assert altered["false_acceptance"] is False

    stale = validation_qualification.compare_validation_classifications(
        model,
        groups,
        valid_source,
        valid_witness,
        expected_model_fingerprint="stale-model-fingerprint",
    )
    assert stale["ordinary"]["classification"] == "validated"
    assert stale["witness"]["classification"] == "validation_error"
    assert stale["false_acceptance"] is False

    incomplete = validation_qualification.compare_validation_classifications(
        model,
        groups,
        valid_source,
        {source.Index(): 1},
        expected_model_fingerprint=model_proto_fingerprint(model),
    )
    assert incomplete["ordinary"]["classification"] == "validated"
    assert incomplete["witness"]["classification"] == "validation_error"
    assert incomplete["false_acceptance"] is False

    extra = validation_qualification.compare_validation_classifications(
        model,
        groups,
        valid_source,
        {**valid_witness, 2: 0},
        expected_model_fingerprint=model_proto_fingerprint(model),
    )
    assert extra["ordinary"]["classification"] == "validated"
    assert extra["witness"]["classification"] == "validation_error"
    assert extra["false_acceptance"] is False


def test_prepared_validation_reuses_completion_and_static_indexes_with_parity():
    model = cp_model.CpModel()
    left = model.NewBoolVar("enroll_1_1")
    right = model.NewBoolVar("enroll_1_2")
    model.AddExactlyOne((left, right))
    source_values = {left.Index(): 1, right.Index(): 0}

    report = validation_qualification.run_prepared_validation_sequence(
        model,
        ((left, right),),
        source_values,
        repetitions=2,
        time_limit_seconds=5,
    )

    assert report["classification_parity"] is True
    assert report["false_acceptance"] is False
    assert all(
        item["classification"] == "validated"
        for item in report["ordinary"] + report["prepared"]
    )
    assert all(
        item["prepared_context"]["used"] is False
        for item in report["ordinary"]
    )
    assert all(
        item["prepared_context"]["used"] is True
        and item["prepared_context"][
            "completion_constraints_prepared"
        ] is True
        for item in report["prepared"]
    )


def test_prepared_validation_context_rejects_a_different_model_lineage():
    model = cp_model.CpModel()
    source = model.NewBoolVar("enroll_1_1")
    context = prepare_validation_context(model, ((source,),))
    other_model = cp_model.CpModel()
    other_source = other_model.NewBoolVar("enroll_1_1")

    outcome = validate_source_decision_candidate_with_status(
        other_model,
        ((other_source,),),
        {other_source.Index(): 1},
        5,
        prepared_context=context,
    )

    assert outcome.classification == "validation_error"
    assert "different model object" in (outcome.error or "")


def test_prepared_validation_context_rejects_stale_identity_metadata():
    model = cp_model.CpModel()
    source = model.NewBoolVar("enroll_1_1")
    context = prepare_validation_context(
        model,
        ((source,),),
        input_semantic_fingerprint="input-v1",
        model_schema_version="model-v1",
        objective_semantics_version="objective-v2",
        configuration_fingerprint="config-v1",
    )

    outcome = validate_source_decision_candidate_with_status(
        model,
        ((source,),),
        {source.Index(): 1},
        5,
        prepared_context=context,
        expected_prepared_context_identity=(
            "input-v2",
            "model-v1",
            "objective-v2",
            "config-v1",
            context.base_model_fingerprint,
        ),
    )

    assert outcome.classification == "validation_error"
    assert "identity" in (outcome.error or "")


def test_prepared_validation_corpus_preserves_parity_across_distinct_candidates():
    model = cp_model.CpModel()
    candidates = [
        model.NewBoolVar("enroll_1_1"),
        model.NewBoolVar("enroll_1_2"),
        model.NewBoolVar("enroll_1_3"),
    ]
    groups = (tuple(candidates),)
    report = validation_qualification.run_prepared_validation_corpus(
        model,
        groups,
        (
            {candidates[0].Index(): 1, candidates[1].Index(): 0, candidates[2].Index(): 0},
            {candidates[0].Index(): 0, candidates[1].Index(): 1, candidates[2].Index(): 0},
            {candidates[0].Index(): 0, candidates[1].Index(): 0, candidates[2].Index(): 1},
        ),
        time_limit_seconds=5,
    )

    assert report["candidate_count"] == 3
    assert report["classification_parity"] is True
    assert report["false_acceptance"] is False
    assert all(
        record["ordinary"]["classification"] == "validated"
        and record["prepared"]["classification"] == "validated"
        for record in report["records"]
    )


def test_validation_telemetry_is_opt_in_for_source_fixed_authority():
    model = cp_model.CpModel()
    source = model.NewBoolVar("enroll_1_1")
    outcome = validate_source_decision_candidate_with_status(
        model,
        ((source,),),
        {source.Index(): 1},
        5,
    )

    assert outcome.classification == "validated"
    assert outcome.telemetry["model_fingerprint_wall_time_seconds"] is None
    assert outcome.telemetry["variable_freedom"] is None

    detailed = validate_source_decision_candidate_with_status(
        model,
        ((source,),),
        {source.Index(): 1},
        5,
        collect_validation_telemetry=True,
    )
    assert detailed.classification == "validated"
    assert detailed.telemetry["model_fingerprint_wall_time_seconds"] is not None
    assert detailed.telemetry["variable_freedom"] is not None
