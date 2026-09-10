"""Solver-free post-analysis tests for the frozen 72-cell grid."""

import json

import pytest

from scheduling_engine import analyze_r16_fixed_scope_search_semantics as analysis
from scheduling_engine import benchmark_r16_fixed_scope_search_semantics as runner


def _contract():
    scopes = [(f"scope{index}", [index * 10 + offset for offset in range(1, 5)])
              for index in range(1, 7)]
    contract = {
        "experiment_id": "analysis-test",
        "treatments": list(analysis.TREATMENTS),
        "source_cells": [{"id": name, "scope": scope} for name, scope in scopes],
        "repeat_count": 3,
        "total_cell_count": 72,
        "decision_criteria": {
            "practical_gain_points": 12,
            "advance": {"minimum_scope_wins": 2, "maximum_scope_losses": 1,
                        "minimum_validated_repeats_per_scope": 2,
                        "minimum_reliable_scopes": 5},
            "minimum_coordination_extra": {
                "material_non_utilization_loss_points": 12,
                "maximum_material_non_utilization_loss_scopes": 1,
                "broad_low_gain_below_points": 12,
                "broad_low_excess_repeats_per_scope": 2,
                "maximum_broad_low_excess_scopes": 1,
            },
            "direct_vs_iterative": {"minimum_scope_wins": 2,
                                    "maximum_scope_losses": 1,
                                    "no_worse_validation": "count"},
        },
    }
    contract["cell_order"] = runner.expected_cell_order(contract)
    return contract


def _result(cell):
    gain = {
        "control_first_qualifying": 6,
        "minimum_coordination": 18,
        "iterative_strict_bound_refinement": 30,
        "direct_exact_v2_optimization": 48,
    }[cell["treatment"]]
    components = {name: 0 for name in analysis.COMPONENTS}
    components["section_utilization_balance"] = gain
    return {
        "cell": cell,
        "treatment": cell["treatment"],
        "repeat": cell["repeat"],
        "source": {"sha256": "source", "source_decision_fingerprint": "state",
                   "substantive_value": 100},
        "scope_fingerprint": "scope",
        "search": {"status": "feasible", "termination": "test",
                   "cumulative_native_solve_wall_seconds": 60,
                   "cumulative_external_solve_wall_seconds": 60,
                   "first_qualifying_latency_seconds": 10,
                   "solve_rounds": [{"round_index": 1}], "branches": 1, "conflicts": 0},
        "candidate": {"found": True, "fingerprint": f"{cell['cell_id']}-candidate",
                      "first_gain": gain - 6, "final_discovery_gain": gain,
                      "changed_student_count": 4, "changed_source_decision_count": 16,
                      "best_objective": 100 - gain, "best_bound": 100 - gain,
                      "absolute_gap": 0, "relative_gap": 0},
        "quality": {"authoritative_gain": gain, "component_improvements": components},
        "validation": {"authoritative": True, "classification": "validated",
                       "wall_seconds": 1},
        "resource_summary": {"peak_tree_rss_bytes": 1000, "peak_tree_uss_bytes": 900,
                             "minimum_available_memory_bytes": 10_000},
        "artifact_sizes": {"result.json": 1000},
    }


def _write_fixture(tmp_path, *, complete=True):
    contract = _contract()
    contract_path = tmp_path / "contract.json"
    contract_path.write_text(json.dumps(contract), encoding="utf-8")
    lineage = tmp_path / "lineage"
    lineage.mkdir()
    (lineage / "frozen_contract.json").write_text(json.dumps(contract), encoding="utf-8")
    cells = contract["cell_order"] if complete else contract["cell_order"][:-1]
    for cell in cells:
        path = lineage / "cells" / cell["cell_id"] / "valid_result.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(_result(cell)), encoding="utf-8")
    return contract_path, lineage


def test_analysis_builds_frozen_tables_and_applies_preregistered_rules(tmp_path):
    contract_path, lineage = _write_fixture(tmp_path)

    result = analysis.analyze(lineage, contract_path)

    assert result["complete_cell_count"] == 72
    assert result["treatment_classifications"]["minimum_coordination"]["classification"] == "advance"
    assert result["direct_vs_iterative"]["classification"] == "direct_wins_engineering_comparison"
    assert (lineage / "analysis" / "treatment_scope_repeat.csv").exists()
    assert (lineage / "analysis" / "component_decomposition.json").exists()
    assert (lineage / "report" / "study_report.md").exists()


def test_analysis_refuses_an_incomplete_grid(tmp_path):
    contract_path, lineage = _write_fixture(tmp_path, complete=False)

    with pytest.raises(RuntimeError, match="requires 72 valid cells"):
        analysis.analyze(lineage, contract_path)


def test_seal_is_allowed_only_after_complete_analysis_and_is_one_way(tmp_path):
    contract_path, lineage = _write_fixture(tmp_path)
    analysis.analyze(lineage, contract_path)

    sealed = analysis.seal(lineage, contract_path)

    assert sealed["sealed"] is True
    assert (lineage / "artifact_hashes.sha256").exists()
    assert (lineage / "SEALED").exists()
    with pytest.raises(RuntimeError, match="already sealed"):
        analysis.seal(lineage, contract_path)
