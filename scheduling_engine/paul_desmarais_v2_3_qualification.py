"""Qualification ladder for the one-repair v2.3 Paul-Desmarais fixture."""

from __future__ import annotations

import argparse
from dataclasses import replace
import json
from unittest.mock import patch

from .paul_desmarais_stress_benchmark import (
    V2_3_AUTHORITATIVE_FINGERPRINT,
)
from .paul_desmarais_v2_2_diagnostics import run_v2_3_diagnostics


def _json_safe(value):
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_safe(item) for item in value]
    return value


def _qualification_gates(diagnostics):
    return {
        "static_identity": (
            diagnostics["fixture_id"] == "paul_desmarais_shaped_g9_12_stress_v2_3"
            and diagnostics["version"] == "v2.3"
            and diagnostics["fingerprint"] == V2_3_AUTHORITATIVE_FINGERPRINT
        ),
        "isolated": diagnostics["isolated_preflight"] == {
            "student_count": 1400,
            "feasible_count": 1400,
            "infeasible_count": 0,
            "unresolved_count": 0,
        },
        "capacity_only": diagnostics["capacity_only_matching"]["feasible"],
        "correlated_pairs": diagnostics["correlated_pair_checks"]["failing_count"] == 0,
        "collision_layer": diagnostics["collision_diagnostic"]["status"] in {
            "optimal",
            "feasible",
        },
    }


def run_explicit_stage1_only(diagnostics):
    """Run Stage 1 only after A-D pass, without initial-hint/Stage 2 work."""

    gates = _qualification_gates(diagnostics)
    if not all(gates.values()):
        return {
            "stage1_reached": False,
            "reason": "qualification_layer_failed",
            "gates": gates,
        }

    from .student_assignment import core
    from .paul_desmarais_stress_benchmark import (
        build_paul_desmarais_shaped_g9_12_stress_v2_3_fixture,
    )

    fixture = build_paul_desmarais_shaped_g9_12_stress_v2_3_fixture()
    requested_limit = 120.0
    certification_input = replace(
        fixture.input_data,
        time_limit_seconds=requested_limit,
    )
    effective_limit = max(
        certification_input.time_limit_seconds,
        requested_limit,
    )
    if effective_limit != 120.0:
        raise AssertionError(f"Unexpected effective Stage 1 limit: {effective_limit}")

    # The existing core calls solve_complete_hard_feasibility_seed() for the
    # authoritative hard model. Returning no initial hints prevents the known
    # post-failure hint-builder runtime debt; it does not alter the hard model.
    with patch.object(core, "_build_initial_assignment_hints", return_value={}):
        result = core._solve_student_assignment(
            certification_input,
            include_lock_costs=False,
            include_candidate_ledger=False,
            use_hard_feasibility_bootstrap=True,
            local_only=True,
            hard_feasibility_time_limit_seconds=120.0,
            hard_feasibility_worker_count=8,
            hard_feasibility_validation_time_limit_seconds=120.0,
            hard_feasibility_validation_worker_count=8,
        )
    stage1 = result.optimization_facts.get("stage_1", {})
    return {
        "stage1_reached": True,
        "effective_time_limit_seconds": effective_limit,
        "worker_count": stage1.get("timings", {}).get("seed_worker_count", 8),
        "raw_stage1_outcome": stage1.get("solver_outcome"),
        "result_status": result.status,
        "result_solver_outcome": result.solver_outcome,
        "complete_seed_produced": stage1.get("complete_seed_produced"),
        "seed_validated_against_full_model": stage1.get(
            "seed_validated_against_full_model"
        ),
        "assignment_count": len(result.assignments),
        "required_decision_group_count": stage1.get("required_decision_group_count"),
        "unmet_mandatory_count": sum(
            item.is_mandatory for item in result.unmet_requests
        ),
        "special_commitment_count": len(result.commitment_assignments),
        "stage1_timings": stage1.get("timings", {}),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--full-stage1",
        action="store_true",
        help="Run Stage 1 only, but only after Layers A-D pass.",
    )
    args = parser.parse_args(argv)
    diagnostics = run_v2_3_diagnostics()
    output = {"diagnostics": diagnostics}
    if args.full_stage1:
        output["stage1"] = run_explicit_stage1_only(diagnostics)
    else:
        output["qualification_gates"] = _qualification_gates(diagnostics)
    print(json.dumps(_json_safe(output), sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
