"""Research-only exact CP-SAT hint identity observability.

The model builder supplies explicit semantic rows.  This module never parses
variable names or guesses relationships between business IDs and proto indexes.
"""

from __future__ import annotations

import hashlib
import json


SCHEMA = "student_assignment_exact_hint_identity_v1"


def _normalize(value):
    if isinstance(value, dict):
        return {
            str(key): _normalize(item)
            for key, item in sorted(value.items(), key=lambda pair: repr(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    if isinstance(value, set):
        return [_normalize(item) for item in sorted(value, key=repr)]
    return value


def _fingerprint(value):
    payload = json.dumps(
        _normalize(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def build_exact_hint_identity_mapping(
    model,
    identity_rows,
    seed_source_variable_values,
    *,
    selected_student_ids=(),
):
    """Join explicit source semantics to incumbent and hint proto values.

    ``identity_rows`` must come from the model builder that created ``model``.
    Every row names one assignment option and its exact variable index.
    """

    selected = {int(item) for item in selected_student_ids}
    static_rows = [
        dict(row) for row in identity_rows
        if not selected or int(row["student_id"]) in selected
    ]
    indexes = [int(row["variable_index"]) for row in static_rows]
    if len(indexes) != len(set(indexes)):
        raise ValueError("Hint identity rows contain duplicate variable indexes")
    variable_count = len(model.Proto().variables)
    invalid = [index for index in indexes if index < 0 or index >= variable_count]
    if invalid:
        raise ValueError(f"Hint identity rows reference invalid indexes: {invalid[:5]}")

    hint_proto = model.Proto().solution_hint
    hint_pairs = list(zip(hint_proto.vars, hint_proto.values))
    if len(hint_pairs) != len({int(index) for index, _value in hint_pairs}):
        raise ValueError("Model hint contains duplicate variable indexes")
    hints = {int(index): int(value) for index, value in hint_pairs}
    incumbent = {
        int(index): int(value)
        for index, value in dict(seed_source_variable_values).items()
    }

    current_option_by_source = {}
    for row in static_rows:
        index = int(row["variable_index"])
        if incumbent.get(index) != 1:
            continue
        key = _fingerprint(row["source_key"])
        if key in current_option_by_source:
            raise ValueError("One source decision has multiple incumbent options")
        current_option_by_source[key] = _normalize(row["assignment_option"])

    rows = []
    for static in sorted(
        static_rows,
        key=lambda row: (
            int(row["student_id"]),
            repr(row["source_key"]),
            int(row["variable_index"]),
        ),
    ):
        index = int(static["variable_index"])
        source_identity = _fingerprint(static["source_key"])
        incumbent_value = incumbent.get(index)
        hint_value = hints.get(index)
        current_option = current_option_by_source.get(source_identity)
        current_section = (
            current_option.get("section_id")
            if isinstance(current_option, dict) else None
        )
        current_online_session = (
            current_option.get("online_supervision_session_id")
            if isinstance(current_option, dict) else None
        )
        section_id = static.get("section_id")
        online_session_id = static.get("online_supervision_session_id")
        is_current = incumbent_value == 1
        row = {
            **static,
            "variable_index": index,
            "incumbent_variable_value": incumbent_value,
            "current_hint_value": hint_value,
            "is_current_assignment_option": is_current,
            "current_assignment_option": current_option,
            "alternate_assignment_option": (
                None if is_current else _normalize(static["assignment_option"])
            ),
            "current_section_id": current_section,
            "alternate_destination_section_id": (
                section_id
                if section_id is not None and section_id != current_section
                else None
            ),
            "current_online_supervision_session_id": current_online_session,
            "alternate_destination_online_supervision_session_id": (
                online_session_id
                if online_session_id is not None
                and online_session_id != current_online_session
                else None
            ),
        }
        rows.append(_normalize(row))

    payload = {
        "schema": SCHEMA,
        "selected_student_ids": sorted(selected),
        "row_count": len(rows),
        "source_decision_count": len({_fingerprint(row["source_key"]) for row in rows}),
        "variable_index_unique": len(indexes) == len(set(indexes)),
        "incumbent_value_complete": all(row["incumbent_variable_value"] is not None for row in rows),
        "hint_value_complete": all(row["current_hint_value"] is not None for row in rows),
        "mapping_rows": rows,
        "identity_source": "explicit_model_builder_metadata",
        "variable_name_parsing_used": False,
        "fuzzy_matching_used": False,
        "heuristic_id_offsets_used": False,
    }
    payload["mapping_fingerprint"] = _fingerprint(payload)
    return payload
