"""Diagnostic fixed-cycle sequence ablation and role-exhaustion study.

This module compares sequences of existing Objective Semantics v2 search
operators. It owns only experiment protocol, telemetry reduction, and
immutable artifacts. CP-SAT and the unchanged full-model validator remain the
authorities for every candidate and no sequence is wired into production.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import re
import statistics
from pathlib import Path
from time import perf_counter

from .benchmark_policy_generalization import (
    TARGET_SCENARIO_DIRECTORIES,
    _REPOSITORY_ROOT,
    _relative_path,
    _scenario_manifest,
    _sha256_file,
    _json_write_atomic,
    STARTUP_AWARE_PARENT_HARD_WALL_SECONDS,
    STARTUP_AWARE_PROFILE,
    STARTUP_AWARE_SEEDS,
)
from .benchmark_adaptive_calibration import run_supervised_calibration_trial
from .student_assignment.adaptive_calibration import (
    CALIBRATION_PROFILES,
    STARTUP_AWARE_MAX_OPERATOR_SECONDS,
    STARTUP_AWARE_SESSION_OVERRIDES,
    STARTUP_AWARE_TOTAL_POLICY_SECONDS,
)
from .student_assignment.adaptive_search import (
    DEFAULT_ADAPTIVE_OPERATOR_PORTFOLIO,
)


SEQUENCE_STUDY_ID = "v2_fixed_cycle_sequence_ablation_20260831"
SEQUENCE_STUDY_SCHEMA = "student_assignment_sequence_ablation_study_v1"
SEQUENCE_RESULT_SCHEMA = "student_assignment_sequence_ablation_result_v1"
SEQUENCE_SUMMARY_SCHEMA = "student_assignment_sequence_ablation_summary_v1"
SEQUENCE_PROFILE = STARTUP_AWARE_PROFILE
SEQUENCE_SEEDS = STARTUP_AWARE_SEEDS
SEQUENCE_VALIDATION_SECONDS = 180.0
SEQUENCE_PARENT_HARD_WALL_SECONDS = STARTUP_AWARE_PARENT_HARD_WALL_SECONDS
SEQUENCE_WORKER_COUNT = 1
SEQUENCE_CAUSAL_STATUS_PRELIMINARY = "PRELIMINARY_NON_PARITY_QUALIFIED"
SEQUENCE_PARITY_SCHEMA = "student_assignment_fixed_cycle_parity_diff_v1"

SEQUENCE_VARIANTS = {
    "full_fixed_cycle": (
        "targeted_r4_s2",
        "targeted_utilization_r64_s8",
        "r2",
    ),
    "r4_s2_only": ("targeted_r4_s2",),
    "no_r2": (
        "targeted_r4_s2",
        "targeted_utilization_r64_s8",
    ),
    "no_utilization": (
        "targeted_r4_s2",
        "r2",
    ),
    "reversed_role_order": (
        "targeted_utilization_r64_s8",
        "targeted_r4_s2",
        "r2",
    ),
    # Included because the completed startup-aware fixed-cycle trajectory
    # visibly returned to R4/S2 after utilization. This is an observed
    # interaction test, not an arbitrary sequence.
    "r4_utilization_r4": (
        "targeted_r4_s2",
        "targeted_utilization_r64_s8",
        "targeted_r4_s2",
    ),
}
SEQUENCE_SCENARIOS = tuple(TARGET_SCENARIO_DIRECTORIES)


def sequence_ablation_budget_contract():
    """Return the common causal-search budget for every sequence variant."""

    return {
        "protocol_version": "fixed-cycle-sequence-ablation-v1",
        "profile": SEQUENCE_PROFILE,
        "worker_count": SEQUENCE_WORKER_COUNT,
        "seeds": list(SEQUENCE_SEEDS),
        "per_operator_maximum_seconds": STARTUP_AWARE_MAX_OPERATOR_SECONDS,
        "cumulative_search_opportunity_seconds": STARTUP_AWARE_TOTAL_POLICY_SECONDS,
        "candidate_validation_time_limit_seconds": SEQUENCE_VALIDATION_SECONDS,
        "candidate_validation_worker_count": 1,
        "parent_hard_wall_seconds": SEQUENCE_PARENT_HARD_WALL_SECONDS,
        "session_overrides": STARTUP_AWARE_SESSION_OVERRIDES,
        "full_model_validation_required": True,
        "unvalidated_candidate_adoption": False,
        "ordinary_stage2_between_iterations": False,
        "production_policy_wiring": False,
    }


def build_sequence_ablation_manifest(
    *,
    study_directory,
    scenario_ids=SEQUENCE_SCENARIOS,
    variant_ids=tuple(SEQUENCE_VARIANTS),
):
    unknown_scenarios = set(scenario_ids) - set(TARGET_SCENARIO_DIRECTORIES)
    unknown_variants = set(variant_ids) - set(SEQUENCE_VARIANTS)
    if unknown_scenarios:
        raise ValueError(f"Unknown sequence-study scenarios: {sorted(unknown_scenarios)}")
    if unknown_variants:
        raise ValueError(f"Unknown sequence-study variants: {sorted(unknown_variants)}")
    return {
        "schema": SEQUENCE_STUDY_SCHEMA,
        "study_id": SEQUENCE_STUDY_ID,
        "parent_study_id": "v2_policy_generalization_startup_aware_20260831",
        "purpose": (
            "Causal ablation of the existing fixed-cycle sequence and "
            "solver-neutral role-exhaustion characterization."
        ),
        "question": (
            "Determine whether R4/S2, utilization follow-on, R2, repeated "
            "R4/S2, or role ordering explains the fixed-cycle advantage."
        ),
        "synthetic_only": True,
        "production_wiring": False,
        # Existing cells were collected before the parent and ablation
        # wrappers shared fixed-cycle request construction.  They remain
        # useful observations, but cannot support causal sequence ranking
        # until the positive-control gate is rerun.
        "causal_comparison_status": SEQUENCE_CAUSAL_STATUS_PRELIMINARY,
        "objective_semantics_version": "v2",
        "profile": SEQUENCE_PROFILE,
        "scenarios": {
            scenario_id: _scenario_manifest(scenario_id)
            for scenario_id in scenario_ids
        },
        "scenario_ids": list(scenario_ids),
        "variant_ids": list(variant_ids),
        "sequence_variants": {
            variant: list(SEQUENCE_VARIANTS[variant]) for variant in variant_ids
        },
        "seeds": list(SEQUENCE_SEEDS),
        "budget_contract": sequence_ablation_budget_contract(),
        "replication_gate": {
            "reference_seed_101_first": True,
            "replicate_when_discriminating": True,
            "special_pressure_after_reference_gate": True,
        },
        "resource_rules": {
            "one_target_cell_at_a_time": True,
            "preferred_available_memory_gib": 4.5,
            "minimum_available_memory_gib": 4.0,
            "process_recycling_required": True,
            "sleep_contamination_excluded_from_runtime_ranking": True,
        },
        "results": {},
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "study_directory": _relative_path(study_directory),
    }


def initialize_sequence_ablation_study(study_directory):
    study_directory = Path(study_directory)
    path = study_directory / "study_manifest.json"
    if path.exists():
        raise FileExistsError(f"Study manifest already exists: {path}")
    manifest = build_sequence_ablation_manifest(study_directory=study_directory)
    _json_write_atomic(path, manifest)
    return manifest


def _result_filename(scenario_id, variant, seed):
    return f"{scenario_id}_{variant}_seed{int(seed)}.json"


def _operator_roles():
    return {
        spec.name: spec.portfolio_role for spec in DEFAULT_ADAPTIVE_OPERATOR_PORTFOLIO
    }


def run_sequence_ablation_cell(
    *,
    study_directory,
    scenario_id,
    variant,
    seed,
    profile=SEQUENCE_PROFILE,
    benchmark_directory=None,
):
    """Run one supervised sequence cell and register its immutable artifact."""

    if scenario_id not in TARGET_SCENARIO_DIRECTORIES:
        raise ValueError(f"Unknown sequence-study scenario: {scenario_id}")
    if variant not in SEQUENCE_VARIANTS:
        raise ValueError(f"Unknown sequence-study variant: {variant}")
    if int(seed) not in SEQUENCE_SEEDS:
        raise ValueError(f"Seed is not preregistered: {seed}")
    if profile not in CALIBRATION_PROFILES:
        raise ValueError(f"Unknown calibration profile: {profile}")

    study_directory = Path(study_directory)
    manifest_path = study_directory / "study_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    scenario = manifest["scenarios"][scenario_id]
    benchmark_directory = Path(
        benchmark_directory
        or (_REPOSITORY_ROOT / scenario["benchmark_directory"])
    )
    filename = _result_filename(scenario_id, variant, seed)
    result_path = study_directory / "results" / filename
    if result_path.exists():
        raise FileExistsError(f"Result artifact already exists: {result_path}")

    started = perf_counter()
    try:
        payload = run_supervised_calibration_trial(
            policy="fixed_cycle",
            profile=profile,
            benchmark_directory=benchmark_directory,
            total_time_limit_seconds=STARTUP_AWARE_TOTAL_POLICY_SECONDS,
            per_operator_time_limit_seconds=STARTUP_AWARE_MAX_OPERATOR_SECONDS,
            worker_count=SEQUENCE_WORKER_COUNT,
            validation_time_limit_seconds=SEQUENCE_VALIDATION_SECONDS,
            hard_wall_seconds=SEQUENCE_PARENT_HARD_WALL_SECONDS,
            startup_aware=True,
            fixed_cycle_names=SEQUENCE_VARIANTS[variant],
            cp_sat_random_seed=int(seed),
        )
        payload = dict(payload)
        payload.update({
            "schema": SEQUENCE_RESULT_SCHEMA,
            "study_id": SEQUENCE_STUDY_ID,
            "parent_study_id": manifest["parent_study_id"],
            "scenario_id": scenario_id,
            "sequence_variant": variant,
            "sequence": list(SEQUENCE_VARIANTS[variant]),
            "seed": int(seed),
            "profile": profile,
            "source_lineage": {
                "input_fingerprint": scenario["input_fingerprint"],
                "source_seed_fingerprint": scenario["source_seed_fingerprint"],
            },
            "budget_contract": sequence_ablation_budget_contract(),
            "cell_elapsed_seconds": perf_counter() - started,
        })
    except Exception as error:
        payload = {
            "schema": SEQUENCE_RESULT_SCHEMA,
            "study_id": SEQUENCE_STUDY_ID,
            "parent_study_id": manifest["parent_study_id"],
            "scenario_id": scenario_id,
            "sequence_variant": variant,
            "sequence": list(SEQUENCE_VARIANTS[variant]),
            "seed": int(seed),
            "profile": profile,
            "status": "source_or_runner_error",
            "error_type": type(error).__name__,
            "error": str(error),
            "source_lineage": {
                "input_fingerprint": scenario["input_fingerprint"],
                "source_seed_fingerprint": scenario["source_seed_fingerprint"],
            },
            "budget_contract": sequence_ablation_budget_contract(),
            "cell_elapsed_seconds": perf_counter() - started,
        }

    _json_write_atomic(result_path, payload)
    manifest["results"][filename] = {
        "path": _relative_path(result_path),
        "sha256": _sha256_file(result_path),
        "status": payload.get("execution_status") or payload.get("status"),
        "final_substantive_value": payload.get("final_substantive_value"),
        "candidate_complete": payload.get("candidate_complete"),
    }
    _json_write_atomic(manifest_path, manifest)
    return payload


def _load_results(study_directory):
    study_directory = Path(study_directory)
    manifest_path = study_directory / "study_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    payloads = {}
    missing = []
    mismatched = []
    for filename, metadata in sorted((manifest.get("results") or {}).items()):
        path = Path(metadata["path"])
        if not path.is_absolute():
            path = _REPOSITORY_ROOT / path
        if not path.exists():
            missing.append(filename)
            continue
        if _sha256_file(path) != metadata.get("sha256"):
            mismatched.append(filename)
            continue
        payloads[filename] = json.loads(path.read_text(encoding="utf-8"))
    if missing or mismatched:
        raise ValueError(
            f"Sequence-study artifact integrity failure: missing={missing!r}, "
            f"mismatched={mismatched!r}"
        )
    return manifest, payloads


def _attempt_summary(attempt, roles):
    inner = tuple(attempt.get("inner_probe_summaries") or ())
    adopted_values = [
        item.get("candidate_substantive_value")
        for item in inner
        if item.get("adopted") and item.get("candidate_substantive_value") is not None
    ]
    component_deltas = [
        dict(item.get("component_deltas") or {})
        for item in inner
        if item.get("adopted")
    ]
    return {
        "sequence_position": attempt.get("sequence_position"),
        "operator": attempt.get("operator"),
        "role": roles.get(attempt.get("operator")),
        "target_scope": attempt.get("target_scope", ()),
        "actual_target_scope": attempt.get("actual_target_scope", ()),
        "status": attempt.get("status"),
        "candidate_found": bool(attempt.get("candidate_found")),
        "candidate_validated": bool(attempt.get("candidate_validated")),
        "adopted": bool(attempt.get("adopted")),
        "gain": attempt.get("gain", 0),
        "candidate_values": adopted_values,
        "component_deltas": component_deltas,
        "exhaustion_classification": attempt.get("exhaustion_classification"),
        "role_exhaustion_classification": attempt.get(
            "role_exhaustion_classification"
        ),
        "role_pressure_before": attempt.get("role_pressure_before", {}),
        "role_pressure_after": attempt.get("role_pressure_after", {}),
        "candidate_source_decision_fingerprint": attempt.get(
            "candidate_source_decision_fingerprint"
        ),
        "cp_sat_seconds": attempt.get("session_cp_sat_seconds")
        or attempt.get("solver_wall_time_seconds"),
        "validation_seconds": attempt.get("session_validation_seconds")
        or attempt.get("validation_seconds"),
        "elapsed_seconds": attempt.get("elapsed_seconds"),
        "stopping_reason": attempt.get("stopping_reason"),
    }


def _complementarity_events(attempts):
    events = []
    for first_index, first in enumerate(attempts):
        if (
            first.get("operator") != "targeted_r4_s2"
            or first.get("adopted")
            or first.get("exhaustion_classification")
            not in {"OPERATOR_NON_IMPROVING", "EXACT_SCOPE_EXHAUSTED"}
        ):
            continue
        for middle_index in range(first_index + 1, len(attempts)):
            middle = attempts[middle_index]
            if not middle.get("adopted") or middle.get("operator") == "targeted_r4_s2":
                continue
            for last_index in range(middle_index + 1, len(attempts)):
                last = attempts[last_index]
                if last.get("operator") != "targeted_r4_s2" or not last.get("adopted"):
                    continue
                events.append({
                    "classification": "OBSERVED ROLE COMPLEMENTARITY",
                    "first_r4_index": first_index,
                    "transition_operator": middle.get("operator"),
                    "productive_r4_index": last_index,
                    "first_r4": first,
                    "transition": middle,
                    "productive_r4": last,
                })
                break
            if events and events[-1]["first_r4_index"] == first_index:
                break
    return events


def _sequence_contributions(attempts):
    role_map = {
        "targeted_r4_s2": "r4_s2",
        "targeted_utilization_r64_s8": "utilization",
        "r2": "r2",
    }
    contributions = {"r4_s2": 0.0, "utilization": 0.0, "r2": 0.0}
    after_prior_role = 0.0
    adopted_count = 0
    prior_roles = set()
    for attempt in attempts:
        if not attempt.get("adopted"):
            continue
        gain = float(attempt.get("gain", 0) or 0)
        role = role_map.get(attempt.get("operator"), attempt.get("operator"))
        contributions[role] = contributions.get(role, 0.0) + gain
        if prior_roles and role not in prior_roles:
            after_prior_role += gain
        prior_roles.add(role)
        adopted_count += 1
    return {
        "by_role": contributions,
        "gain_after_prior_role_transition": after_prior_role,
        "adopted_count": adopted_count,
        "total_gain": sum(contributions.values()),
    }


def _attempt_totals(attempts):
    """Derive timing totals from the stable per-attempt contract."""

    return {
        "cp_sat_seconds": sum(
            float(item.get("cp_sat_seconds") or 0.0) for item in attempts
        ),
        "validation_seconds": sum(
            float(item.get("validation_seconds") or 0.0) for item in attempts
        ),
    }


def _fixed_cycle_names(payload):
    sequence = payload.get("sequence")
    if sequence:
        return tuple(sequence)
    if payload.get("policy") == "fixed_cycle":
        return tuple(
            SEQUENCE_VARIANTS["full_fixed_cycle"]
        )
    return ()


def _normalized_budget_contract(payload):
    contract = payload.get("budget_contract") or {}
    session_overrides = contract.get("session_overrides") or {}
    selected_sessions = {
        name: {
            key: session_overrides.get(name, {}).get(key)
            for key in (
                "session_time_limit_seconds",
                "session_max_attempts",
                "per_attempt_cp_sat_limit_seconds",
            )
        }
        for name in _fixed_cycle_names(payload)
    }
    return {
        "profile": payload.get("profile") or contract.get("profile"),
        "profile_fingerprint": payload.get("profile_fingerprint")
        or contract.get("profile_fingerprint"),
        "worker_count": payload.get("worker_count")
        or contract.get("worker_count"),
        "cp_sat_random_seed": payload.get("cp_sat_random_seed"),
        "per_operator_maximum_seconds": contract.get(
            "per_operator_maximum_seconds"
        ),
        "cumulative_seconds": contract.get(
            "cumulative_policy_budget_seconds",
            contract.get("cumulative_search_opportunity_seconds"),
        ),
        "candidate_validation_time_limit_seconds": contract.get(
            "candidate_validation_time_limit_seconds"
        ),
        "candidate_validation_worker_count": contract.get(
            "candidate_validation_worker_count"
        ),
        "parent_hard_wall_seconds": payload.get("hard_wall_seconds")
        or contract.get("parent_hard_wall_seconds"),
        "full_model_validation_required": contract.get(
            "full_model_validation_required"
        ),
        "ordinary_stage2_between_iterations": contract.get(
            "ordinary_stage2_between_iterations"
        ),
        "session_overrides": selected_sessions,
    }


def _inner_probe_projection(summary):
    fields = (
        "iteration",
        "operator",
        "radius",
        "effective_radius",
        "target_scope",
        "actual_target_scope",
        "selected_grade",
        "affected_student_ids",
        "affected_section_ids",
        "candidate_found",
        "candidate_complete",
        "candidate_validated",
        "candidate_source_decision_fingerprint",
        "candidate_substantive_value",
        "starting_incumbent_value",
        "substantive_gain",
        "component_deltas",
        "validation_classification",
        "status",
        "stopping_reason",
        "model_variable_count",
        "model_constraint_count",
    )
    return {field: summary.get(field) for field in fields}


def _attempt_projection(attempt, position):
    fields = (
        "operator",
        "target_scope",
        "actual_target_scope",
        "selected_grade",
        "candidate_found",
        "candidate_validated",
        "candidate_source_decision_fingerprint",
        "gain",
        "status",
        "validation_classification",
        "validation_solver_outcome",
        "adopted",
        "stopping_reason",
    )
    projection = {field: attempt.get(field) for field in fields}
    projection["sequence_position"] = attempt.get(
        "sequence_position", position
    )
    projection["inner_probe_summaries"] = [
        _inner_probe_projection(summary)
        for summary in tuple(attempt.get("inner_probe_summaries") or ())
    ]
    return projection


def fixed_cycle_parity_projection(payload):
    """Return comparable semantic facts from a parent or ablation trial.

    Parent and child studies intentionally use different artifact schemas and
    budget-key names.  This projection compares the contract and trajectory
    fields that affect the positive control while excluding telemetry noise.
    """

    return {
        "input_fingerprint": payload.get("input_fingerprint")
        or (payload.get("source_lineage") or {}).get("input_fingerprint"),
        "source_seed_fingerprint": payload.get("source_seed_fingerprint")
        or (payload.get("source_lineage") or {}).get("source_seed_fingerprint"),
        "policy": payload.get("policy"),
        "sequence": list(_fixed_cycle_names(payload)),
        "budget": _normalized_budget_contract(payload),
        "initial_substantive_value": payload.get("initial_substantive_value"),
        "initial_source_decision_fingerprint": payload.get(
            "source_seed_fingerprint"
        )
        or (payload.get("source_lineage") or {}).get("source_seed_fingerprint"),
        "attempts": [
            _attempt_projection(attempt, position)
            for position, attempt in enumerate(tuple(payload.get("attempts") or ()))
        ],
        "final": {
            "execution_status": payload.get("execution_status")
            or payload.get("status"),
            "candidate_complete": payload.get("candidate_complete"),
            "final_substantive_value": payload.get("final_substantive_value"),
            "final_source_decision_fingerprint": payload.get(
                "final_source_decision_fingerprint"
            ),
            "final_assignment_count": payload.get("final_assignment_count"),
            "final_unmet_count": payload.get("final_unmet_count"),
            "final_special_commitment_count": payload.get(
                "final_special_commitment_count"
            ),
        },
    }


def _field_differences(left, right, path=""):
    if isinstance(left, dict) and isinstance(right, dict):
        differences = []
        for key in sorted(set(left) | set(right)):
            child_path = f"{path}.{key}" if path else key
            differences.extend(
                _field_differences(left.get(key), right.get(key), child_path)
            )
        return differences
    if isinstance(left, list) and isinstance(right, list):
        differences = []
        for index in range(max(len(left), len(right))):
            child_path = f"{path}[{index}]"
            differences.extend(
                _field_differences(
                    left[index] if index < len(left) else None,
                    right[index] if index < len(right) else None,
                    child_path,
                )
            )
        return differences
    if left != right:
        return [{"path": path, "left": left, "right": right}]
    return []


def _first_semantic_difference(differences):
    """Order a diff by sequence position, then by decision significance."""

    priorities = (
        "operator",
        "target_scope",
        "actual_target_scope",
        "candidate_source_decision_fingerprint",
        "candidate_validated",
        "validation_classification",
        "validation_solver_outcome",
        "adopted",
        "final_substantive_value",
    )

    def sort_key(difference):
        match = re.search(r"attempts\[(\d+)\]", difference["path"])
        position = int(match.group(1)) if match else 10**9
        priority = next(
            (
                index
                for index, marker in enumerate(priorities)
                if marker in difference["path"]
            ),
            len(priorities),
        )
        return position, priority, difference["path"]

    return min(differences, key=sort_key) if differences else None


def _control_observation(payload):
    """Keep useful runtime/resource facts without copying source decisions."""

    def scalar_mapping(value):
        if not isinstance(value, dict):
            return {}
        return {
            key: item
            for key, item in value.items()
            if item is None or isinstance(item, (bool, int, float, str))
        }

    resource = payload.get("resource") or {}
    resource_keys = (
        "available",
        "elapsed_seconds",
        "logical_cpu_count",
        "peak_child_process_count",
        "peak_pagefile_bytes",
        "peak_thread_count",
        "peak_tree_uss_bytes",
        "peak_tree_working_set_bytes",
        "peak_uss_bytes",
        "peak_working_set_bytes",
        "sample_count",
        "system_memory_percent",
    )

    return {
        "execution_status": payload.get("execution_status")
        or payload.get("status"),
        "candidate_complete": payload.get("candidate_complete"),
        "initial_substantive_value": payload.get("initial_substantive_value"),
        "final_substantive_value": payload.get("final_substantive_value"),
        "final_assignment_count": payload.get("final_assignment_count"),
        "final_unmet_count": payload.get("final_unmet_count"),
        "final_special_commitment_count": payload.get(
            "final_special_commitment_count"
        ),
        "cell_elapsed_seconds": payload.get("cell_elapsed_seconds"),
        "timing": scalar_mapping(payload.get("timing")),
        "phase_timings": scalar_mapping(payload.get("phase_timings")),
        "policy_accounting": scalar_mapping(payload.get("policy_accounting")),
        "supervision": scalar_mapping(payload.get("supervision")),
        "resource": {
            key: resource.get(key)
            for key in resource_keys
            if key in resource
        },
        "attempts": [
            {
                "operator": attempt.get("operator"),
                "candidate_found": attempt.get("candidate_found"),
                "candidate_validated": attempt.get("candidate_validated"),
                "validation_classification": attempt.get(
                    "validation_classification"
                ),
                "adopted": attempt.get("adopted"),
                "solver_wall_time_seconds": attempt.get(
                    "solver_wall_time_seconds"
                ),
                "validation_seconds": attempt.get("validation_seconds"),
            }
            for attempt in tuple(payload.get("attempts") or ())
        ],
    }


def compare_fixed_cycle_control_payloads(parent_payload, ablation_payload):
    """Compare matched parent/ablation controls without ranking operators."""

    parent = fixed_cycle_parity_projection(parent_payload)
    ablation = fixed_cycle_parity_projection(ablation_payload)
    configuration_fields = (
        "input_fingerprint",
        "source_seed_fingerprint",
        "policy",
        "sequence",
        "budget",
    )
    configuration_differences = []
    for field in configuration_fields:
        configuration_differences.extend(
            _field_differences(
                parent[field], ablation[field], f"configuration.{field}"
            )
        )
    trajectory_differences = _field_differences(
        {
            "initial_substantive_value": parent["initial_substantive_value"],
            "initial_source_decision_fingerprint": parent[
                "initial_source_decision_fingerprint"
            ],
            "attempts": parent["attempts"],
            "final": parent["final"],
        },
        {
            "initial_substantive_value": ablation["initial_substantive_value"],
            "initial_source_decision_fingerprint": ablation[
                "initial_source_decision_fingerprint"
            ],
            "attempts": ablation["attempts"],
            "final": ablation["final"],
        },
        "trajectory",
    )
    first_divergence = _first_semantic_difference(trajectory_differences)
    if configuration_differences:
        classification = "CONFIGURATION_NON_PARITY"
    elif not trajectory_differences:
        classification = "PARITY_MATCH"
    elif any(
        marker in (first_divergence or {}).get("path", "")
        for marker in (
            "candidate_validated",
            "validation_classification",
            "validation_solver_outcome",
            "adopted",
        )
    ):
        classification = "VALIDATION_TRANSITION_VARIANCE"
    else:
        classification = "TRAJECTORY_NON_PARITY"
    return {
        "schema": SEQUENCE_PARITY_SCHEMA,
        "classification": classification,
        "observations": {
            "parent": _control_observation(parent_payload),
            "ablation": _control_observation(ablation_payload),
        },
        "configuration": {
            "parent": {field: parent[field] for field in configuration_fields},
            "ablation": {
                field: ablation[field] for field in configuration_fields
            },
            "differences": configuration_differences,
        },
        "trajectory": {
            "parent": {
                "initial_substantive_value": parent["initial_substantive_value"],
                "attempts": parent["attempts"],
                "final": parent["final"],
            },
            "ablation": {
                "initial_substantive_value": ablation["initial_substantive_value"],
                "attempts": ablation["attempts"],
                "final": ablation["final"],
            },
            "differences": trajectory_differences,
            "first_divergence": first_divergence,
        },
    }


def write_fixed_cycle_parity_diff(parent_path, ablation_path, output_path):
    """Write a compact, immutable positive-control comparison artifact."""

    parent_payload = json.loads(Path(parent_path).read_text(encoding="utf-8"))
    ablation_payload = json.loads(Path(ablation_path).read_text(encoding="utf-8"))
    result = compare_fixed_cycle_control_payloads(parent_payload, ablation_payload)
    _json_write_atomic(Path(output_path), result)
    return result


def summarize_sequence_ablation_study(study_directory):
    """Verify artifacts and produce compact causal-study facts."""

    manifest, payloads = _load_results(study_directory)
    roles = _operator_roles()
    cells = []
    grouped = {}
    for filename, payload in sorted(payloads.items()):
        attempts = [
            _attempt_summary(item, roles) for item in tuple(payload.get("attempts") or ())
        ]
        contributions = _sequence_contributions(attempts)
        attempt_totals = _attempt_totals(attempts)
        timing = payload.get("timing") or {}
        accounting = payload.get("policy_accounting") or {}
        resource = payload.get("resource") or {}
        cell = {
            "artifact": filename,
            "scenario_id": payload.get("scenario_id"),
            "sequence_variant": payload.get("sequence_variant"),
            "sequence": payload.get("sequence"),
            "seed": payload.get("seed"),
            "input_fingerprint": payload.get("input_fingerprint"),
            "source_seed_fingerprint": payload.get("source_seed_fingerprint"),
            "final_source_decision_fingerprint": payload.get(
                "final_source_decision_fingerprint"
            ),
            "execution_status": payload.get("execution_status") or payload.get("status"),
            "candidate_complete": payload.get("candidate_complete"),
            "final_unmet_count": payload.get("final_unmet_count"),
            "final_assignment_count": payload.get("final_assignment_count"),
            "final_special_commitment_count": payload.get(
                "final_special_commitment_count"
            ),
            "initial_substantive_value": payload.get("initial_substantive_value"),
            "final_substantive_value": payload.get("final_substantive_value"),
            "final_components": payload.get("final_components"),
            "attempts": attempts,
            "contributions": contributions,
            "complementarity_events": _complementarity_events(attempts),
            "cumulative_cp_sat_seconds": accounting.get(
                "cumulative_cp_sat_seconds", attempt_totals["cp_sat_seconds"]
            ),
            "cumulative_validation_seconds": accounting.get(
                "cumulative_validation_seconds", attempt_totals["validation_seconds"]
            ),
            "policy_seconds": (payload.get("phase_timings") or {})
            .get("policy", {})
            .get("total"),
            "cell_seconds": payload.get("cell_elapsed_seconds")
            or timing.get("total_elapsed_seconds"),
            "peak_tree_working_set_bytes": resource.get(
                "peak_tree_working_set_bytes"
            ),
        }
        cells.append(cell)
        grouped.setdefault(
            (cell["scenario_id"], cell["sequence_variant"]), []
        ).append(cell)

    grouped_summary = {}
    for (scenario_id, variant), group in sorted(grouped.items()):
        values = [float(item["final_substantive_value"]) for item in group]
        gains = [
            float(item["initial_substantive_value"])
            - float(item["final_substantive_value"])
            for item in group
        ]
        cp = [float(item["cumulative_cp_sat_seconds"] or 0) for item in group]
        grouped_summary[f"{scenario_id}:{variant}"] = {
            "scenario_id": scenario_id,
            "sequence_variant": variant,
            "sequence": list(SEQUENCE_VARIANTS[variant]),
            "seeds": [item["seed"] for item in group],
            "final_values": values,
            "median_final_value": statistics.median(values),
            "best_final_value": min(values),
            "worst_final_value": max(values),
            "direct_gains": gains,
            "all_complete": all(item["candidate_complete"] for item in group),
            "all_unmet_free": all(item["final_unmet_count"] == 0 for item in group),
            "all_attempts_authority_validated": all(
                all(
                    not attempt["adopted"] or attempt["candidate_validated"]
                    for attempt in item["attempts"]
                )
                for item in group
            ),
            "r4_s2_gain": sum(
                item["contributions"]["by_role"].get("r4_s2", 0)
                for item in group
            ),
            "utilization_gain": sum(
                item["contributions"]["by_role"].get("utilization", 0)
                for item in group
            ),
            "r2_gain": sum(
                item["contributions"]["by_role"].get("r2", 0)
                for item in group
            ),
            "adopted_count_by_seed": [
                item["contributions"]["adopted_count"] for item in group
            ],
            "median_cp_sat_seconds": statistics.median(cp),
            "median_validation_seconds": statistics.median(
                float(item["cumulative_validation_seconds"] or 0) for item in group
            ),
            "median_policy_seconds": statistics.median(
                float(item["policy_seconds"] or 0) for item in group
            ),
            "median_cell_seconds": statistics.median(
                float(item["cell_seconds"] or 0) for item in group
            ),
            "peak_tree_working_set_bytes": max(
                int(item["peak_tree_working_set_bytes"] or 0) for item in group
            ),
            "complementarity_event_count": sum(
                len(item["complementarity_events"]) for item in group
            ),
            "exhaustion_classifications": {
                classification: sum(
                    attempt["exhaustion_classification"] == classification
                    for item in group
                    for attempt in item["attempts"]
                )
                for classification in (
                    "PRODUCTIVE",
                    "EXACT_SCOPE_EXHAUSTED",
                    "OPERATOR_UNRESOLVED",
                    "OPERATOR_NON_IMPROVING",
                )
            },
            "role_exhaustion_classifications": {
                classification: sum(
                    attempt["role_exhaustion_classification"] == classification
                    for item in group
                    for attempt in item["attempts"]
                )
                for classification in (
                    "ROLE_REMAINS_ACTIONABLE",
                    "ROLE_EXHAUSTION_NOT_PROVEN",
                )
            },
        }

    scenario_winners = {}
    for scenario_id in manifest["scenario_ids"]:
        candidates = [
            item
            for item in grouped_summary.values()
            if item["scenario_id"] == scenario_id
        ]
        if candidates:
            winner = min(candidates, key=lambda item: item["median_final_value"])
            scenario_winners[scenario_id] = {
                "sequence_variant": winner["sequence_variant"],
                "median_final_value": winner["median_final_value"],
            }

    return {
        "schema": SEQUENCE_SUMMARY_SCHEMA,
        "study_id": manifest["study_id"],
        "parent_study_id": manifest["parent_study_id"],
        "production_wiring": False,
        "artifact_integrity": {
            "manifest_result_count": len(manifest.get("results") or {}),
            "loaded_result_count": len(cells),
            "all_result_hashes_verified": True,
        },
        "budget_contract": manifest["budget_contract"],
        "scenarios": manifest["scenarios"],
        "sequence_variants": manifest["sequence_variants"],
        "scenario_winners": scenario_winners,
        "grouped_summary": grouped_summary,
        "cells": cells,
        "interpretation": {
            "objective_semantics_changed": False,
            "hard_constraints_changed": False,
            "validation_authority_changed": False,
            "unvalidated_incumbent_transitions": False,
            "role_exhaustion_rule": (
                "One failed operator never proves global role exhaustion; only "
                "ROLE_REMAINS_ACTIONABLE or ROLE_EXHAUSTION_NOT_PROVEN is reported."
            ),
            "promotion_decision": "PENDING_SEQUENCE_ABLATION_EVIDENCE",
            "causal_comparison_status": manifest.get(
                "causal_comparison_status",
                SEQUENCE_CAUSAL_STATUS_PRELIMINARY,
            ),
        },
    }


def write_sequence_ablation_summary(study_directory):
    study_directory = Path(study_directory)
    summary = summarize_sequence_ablation_study(study_directory)
    summary_path = study_directory / "study_summary.json"
    _json_write_atomic(summary_path, summary)
    manifest_path = study_directory / "study_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["summary_artifact"] = {
        "path": _relative_path(summary_path),
        "sha256": _sha256_file(summary_path),
    }
    _json_write_atomic(manifest_path, manifest)
    return summary


def main(argv=None):  # pragma: no cover - offline experiment entrypoint
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--study-directory", type=Path, required=True)
    parser.add_argument("--initialize", action="store_true")
    parser.add_argument("--summarize", action="store_true")
    parser.add_argument("--compare-parent", type=Path)
    parser.add_argument("--compare-ablation", type=Path)
    parser.add_argument("--compare-output", type=Path)
    parser.add_argument("--scenario", choices=SEQUENCE_SCENARIOS)
    parser.add_argument("--variant", choices=tuple(SEQUENCE_VARIANTS))
    parser.add_argument("--seed", type=int)
    args = parser.parse_args(argv)
    compare_args = (
        args.compare_parent,
        args.compare_ablation,
        args.compare_output,
    )
    if any(value is not None for value in compare_args) and not all(
        value is not None for value in compare_args
    ):
        parser.error(
            "--compare-parent, --compare-ablation, and --compare-output "
            "must be supplied together"
        )
    if args.initialize and args.summarize:
        parser.error("--initialize and --summarize are mutually exclusive")
    if any(value is not None for value in compare_args) and (
        args.initialize or args.summarize or args.scenario or args.variant or args.seed
    ):
        parser.error("comparison arguments are mutually exclusive with study actions")
    if args.compare_parent is not None:
        payload = write_fixed_cycle_parity_diff(
            args.compare_parent,
            args.compare_ablation,
            args.compare_output,
        )
    elif args.initialize:
        payload = initialize_sequence_ablation_study(args.study_directory)
    elif args.summarize:
        payload = write_sequence_ablation_summary(args.study_directory)
    else:
        if args.scenario is None or args.variant is None or args.seed is None:
            parser.error("--scenario, --variant, and --seed are required")
        payload = run_sequence_ablation_cell(
            study_directory=args.study_directory,
            scenario_id=args.scenario,
            variant=args.variant,
            seed=args.seed,
        )
    if args.compare_parent is not None or args.initialize or args.summarize:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    else:
        filename = _result_filename(args.scenario, args.variant, args.seed)
        print(json.dumps({
            "study_id": payload.get("study_id"),
            "scenario_id": payload.get("scenario_id"),
            "sequence_variant": payload.get("sequence_variant"),
            "seed": payload.get("seed"),
            "status": payload.get("execution_status") or payload.get("status"),
            "initial_substantive_value": payload.get("initial_substantive_value"),
            "final_substantive_value": payload.get("final_substantive_value"),
            "candidate_complete": payload.get("candidate_complete"),
            "attempt_count": len(tuple(payload.get("attempts") or ())),
            "adopted_count": sum(
                bool(item.get("adopted"))
                for item in tuple(payload.get("attempts") or ())
            ),
            "cell_elapsed_seconds": payload.get("cell_elapsed_seconds"),
            "artifact_sha256": _sha256_file(
                Path(args.study_directory) / "results" / filename
            ),
        }, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "SEQUENCE_RESULT_SCHEMA",
    "SEQUENCE_SCENARIOS",
    "SEQUENCE_STUDY_ID",
    "SEQUENCE_STUDY_SCHEMA",
    "SEQUENCE_SUMMARY_SCHEMA",
    "SEQUENCE_VARIANTS",
    "SEQUENCE_CAUSAL_STATUS_PRELIMINARY",
    "SEQUENCE_PARITY_SCHEMA",
    "build_sequence_ablation_manifest",
    "compare_fixed_cycle_control_payloads",
    "fixed_cycle_parity_projection",
    "initialize_sequence_ablation_study",
    "run_sequence_ablation_cell",
    "sequence_ablation_budget_contract",
    "summarize_sequence_ablation_study",
    "write_fixed_cycle_parity_diff",
    "write_sequence_ablation_summary",
]
