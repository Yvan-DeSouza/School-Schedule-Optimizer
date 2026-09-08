from types import SimpleNamespace

import pytest

from scheduling_engine.student_assignment.hint_observability import (
    build_exact_hint_identity_mapping,
)


class FakeModel:
    def __init__(self, variable_count, hints):
        self._proto = SimpleNamespace(
            variables=[object() for _ in range(variable_count)],
            solution_hint=SimpleNamespace(
                vars=[item[0] for item in hints],
                values=[item[1] for item in hints],
            ),
        )

    def Proto(self):
        return self._proto


def identity_rows():
    return (
        {
            "student_id": 10,
            "request_id": 100,
            "source_key": ("course", 100),
            "model_source_key": ("course", 100),
            "assignment_kind": "course",
            "assignment_option": {"section_id": 2},
            "section_id": 2,
            "online_supervision_session_id": None,
            "variable_index": 1,
        },
        {
            "student_id": 10,
            "request_id": 100,
            "source_key": ("course", 100),
            "model_source_key": ("course", 100),
            "assignment_kind": "course",
            "assignment_option": {"section_id": 1},
            "section_id": 1,
            "online_supervision_session_id": None,
            "variable_index": 0,
        },
        {
            "student_id": 11,
            "request_id": 101,
            "source_key": ("course", 101),
            "model_source_key": ("course", 101),
            "assignment_kind": "course",
            "assignment_option": {"section_id": 3},
            "section_id": 3,
            "online_supervision_session_id": None,
            "variable_index": 2,
        },
    )


def test_exact_hint_mapping_is_deterministic_and_target_scoped():
    model = FakeModel(3, ((0, 1), (1, 0), (2, 1)))
    first = build_exact_hint_identity_mapping(
        model,
        identity_rows(),
        {0: 1, 1: 0, 2: 1},
        selected_student_ids=(10,),
    )
    second = build_exact_hint_identity_mapping(
        model,
        tuple(reversed(identity_rows())),
        {2: 1, 1: 0, 0: 1},
        selected_student_ids=(10,),
    )

    assert first == second
    assert first["row_count"] == 2
    assert first["source_decision_count"] == 1
    assert first["variable_index_unique"] is True
    assert first["incumbent_value_complete"] is True
    assert first["hint_value_complete"] is True
    assert first["variable_name_parsing_used"] is False
    by_index = {row["variable_index"]: row for row in first["mapping_rows"]}
    assert by_index[0]["current_section_id"] == 1
    assert by_index[0]["is_current_assignment_option"] is True
    assert by_index[0]["current_assignment_option"] == {"section_id": 1}
    assert by_index[0]["alternate_assignment_option"] is None
    assert by_index[0]["alternate_destination_section_id"] is None
    assert by_index[1]["current_section_id"] == 1
    assert by_index[1]["alternate_assignment_option"] == {"section_id": 2}
    assert by_index[1]["alternate_destination_section_id"] == 2
    assert len(first["mapping_fingerprint"]) == 64


def test_exact_hint_mapping_rejects_duplicate_variable_identity():
    model = FakeModel(3, ((0, 1), (1, 0), (2, 1)))
    rows = identity_rows() + ({**identity_rows()[0]},)
    with pytest.raises(ValueError, match="duplicate variable indexes"):
        build_exact_hint_identity_mapping(model, rows, {0: 1, 1: 0, 2: 1})


def test_exact_hint_mapping_reports_missing_hint_without_guessing():
    model = FakeModel(3, ((0, 1),))
    result = build_exact_hint_identity_mapping(
        model,
        identity_rows(),
        {0: 1, 1: 0, 2: 1},
    )
    assert result["hint_value_complete"] is False
    assert result["mapping_rows"][1]["current_hint_value"] is None
    assert result["fuzzy_matching_used"] is False
    assert result["heuristic_id_offsets_used"] is False
