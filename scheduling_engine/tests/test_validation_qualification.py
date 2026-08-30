from types import SimpleNamespace

from ortools.sat.python import cp_model

from scheduling_engine.tests.test_validation_benchmark import _data, _source_decisions
from scheduling_engine.student_assignment import validation_qualification
from scheduling_engine.student_assignment.solver import model_proto_fingerprint


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
