"""Fresh-process verifier for frozen student-assignment research checkpoints."""

from __future__ import annotations

import json
import sys

from .quality import evaluate_student_assignment_quality
from .research_artifacts import (
    load_frozen_student_assignment_input,
    load_validated_stage1_seed,
    validate_loaded_stage1_seed,
)


def verify(input_path, seed_path, *, validation_time_limit_seconds=60.0, worker_count=8):
    loaded_input = load_frozen_student_assignment_input(input_path)
    data = loaded_input["data"]
    seed = load_validated_stage1_seed(
        seed_path,
        data=data,
        expected_input_fingerprint=loaded_input["input_semantic_fingerprint"],
    )
    validation = validate_loaded_stage1_seed(
        data,
        seed,
        time_limit_seconds=validation_time_limit_seconds,
        worker_count=worker_count,
    )
    if not validation["valid"]:
        raise ValueError("Loaded seed failed canonical full-model validation")
    first = evaluate_student_assignment_quality(
        data,
        assignments=seed["assignments"],
        commitment_assignments=seed["commitment_assignments"],
    )
    second = evaluate_student_assignment_quality(
        data,
        assignments=seed["assignments"],
        commitment_assignments=seed["commitment_assignments"],
    )
    if first != second:
        raise ValueError("Offline Objective V2 evaluation is not deterministic")
    return {
        "artifact_only": True,
        "input_semantic_fingerprint": loaded_input["input_semantic_fingerprint"],
        "seed_source_decision_fingerprint": seed["seed_source_decision_fingerprint"],
        "assignment_count": len(seed["assignments"]),
        "special_commitment_count": len(seed["commitment_assignments"]),
        "unmet_mandatory_count": validation["unmet_mandatory_count"],
        "independent_full_model_validation": validation["independent_full_model_validation"],
        "objective_v2_deterministic": True,
    }


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: python -m scheduling_engine.student_assignment.verify_research_artifacts INPUT SEED")
    print(json.dumps(verify(sys.argv[1], sys.argv[2]), sort_keys=True, separators=(",", ":")))
