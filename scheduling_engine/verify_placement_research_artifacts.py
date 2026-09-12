"""Fresh-process verifier for detached section-placement checkpoints."""

from __future__ import annotations

import json
import sys

from .placement_research_artifacts import (
    load_frozen_placement_input,
    load_frozen_placement_result,
    placement_pair_summary,
)


def verify(input_path, result_path):
    frozen_input = load_frozen_placement_input(input_path)
    frozen_result = load_frozen_placement_result(
        result_path,
        data=frozen_input["data"],
        expected_input_fingerprint=frozen_input["semantic_fingerprint"],
    )
    data = frozen_input["data"]
    result = frozen_result["result"]
    pair_summary = placement_pair_summary(data, result)
    return {
        "input_schema": frozen_input["payload"]["schema"],
        "result_schema": frozen_result["payload"]["schema"],
        "input_semantic_fingerprint": frozen_input["semantic_fingerprint"],
        "result_semantic_fingerprint": frozen_result["semantic_fingerprint"],
        "unit_count": len(data.units),
        "timeslot_count": len(data.timeslots),
        "assignment_count": len(result.assignments),
        "solver_outcome": result.solver_outcome,
        "unplaced_unit_count": len(result.unplaced_unit_keys),
        "pair_count": pair_summary["pair_count"],
        "co_timed_pair_count": pair_summary["co_timed_pair_count"],
        "split_pair_count": pair_summary["split_pair_count"],
        "missing_pair_count": pair_summary["missing_pair_count"],
        "valid_pair_capacity": pair_summary["valid_pair_capacity"],
        "half_pair_unit_count": sum(
            unit.shared_placement_key is not None for unit in data.units
        ),
        "online_unit_count": sum(
            unit.online_supervision_session_id is not None for unit in data.units
        ),
    }


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 2:
        raise SystemExit("usage: python -m scheduling_engine.verify_placement_research_artifacts INPUT RESULT")
    print(json.dumps(verify(argv[0], argv[1]), sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
