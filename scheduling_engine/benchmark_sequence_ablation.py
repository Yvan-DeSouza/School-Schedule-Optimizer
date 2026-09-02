"""Diagnostic fixed-cycle sequence ablation and role-exhaustion study.

This module compares sequences of existing Objective Semantics v2 search
operators. It owns only experiment protocol, telemetry reduction, and
immutable artifacts. CP-SAT and the unchanged full-model validator remain the
authorities for every candidate and no sequence is wired into production.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
import re
import statistics
import subprocess
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
    fixed_cycle_control_request,
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

# The parity qualification is a separate, external diagnostic study.  It is
# intentionally not a continuation of either existing study and never writes
# to their manifests or checkpoint lineage.
PARITY_STUDY_ID = "fixed_cycle_parity_qualification_20260901"
PARITY_STUDY_SCHEMA = "student_assignment_fixed_cycle_parity_study_v1"
PARITY_RESULT_SCHEMA = "student_assignment_fixed_cycle_parity_result_v1"
PARITY_SUMMARY_SCHEMA = "student_assignment_fixed_cycle_parity_summary_v1"
SEED303_REPLICATION_STUDY_ID = "fixed_cycle_parity_seed303_replication_20260901"
SEED303_REPLICATION_SCHEMA = "student_assignment_fixed_cycle_seed303_replication_v1"
SEED303_REPLICATION_SUMMARY_SCHEMA = "student_assignment_fixed_cycle_seed303_replication_summary_v1"
PARITY_ORIGINS = (
    "startup_aware_parent",
    "sequence_ablation",
    "parallel_policy",
)
PARITY_SEEDS = (101, 202, 303)
PARITY_GLOBAL_WALL_SECONDS = 5.5 * 60.0 * 60.0
PARITY_MIN_CELL_RESERVE_SECONDS = 35.0 * 60.0
PARITY_ORIGIN_LABELS = {
    "startup_aware_parent": "startup-aware policy runner",
    "sequence_ablation": "sequence-ablation full_fixed_cycle control",
    "parallel_policy": "progressive-parallel-study fixed_cycle control",
}

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
        control_request = fixed_cycle_control_request(
            profile=profile,
            benchmark_directory=benchmark_directory,
            input_fingerprint=scenario["input_fingerprint"],
            source_seed_fingerprint=scenario["source_seed_fingerprint"],
            cp_sat_random_seed=int(seed),
            total_time_limit_seconds=STARTUP_AWARE_TOTAL_POLICY_SECONDS,
            per_operator_time_limit_seconds=STARTUP_AWARE_MAX_OPERATOR_SECONDS,
            worker_count=SEQUENCE_WORKER_COUNT,
            validation_time_limit_seconds=SEQUENCE_VALIDATION_SECONDS,
            parent_hard_wall_seconds=SEQUENCE_PARENT_HARD_WALL_SECONDS,
            fixed_cycle_names=SEQUENCE_VARIANTS[variant],
        )
        payload = run_supervised_calibration_trial(
            **control_request["run_kwargs"]
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
        payload.update({
            "origin": "sequence_ablation",
            "fixed_cycle_request": control_request["request"],
            "fixed_cycle_request_fingerprint": control_request[
                "request_fingerprint"
            ],
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
        control_request = fixed_cycle_control_request(
            profile=profile,
            benchmark_directory=benchmark_directory,
            input_fingerprint=scenario["input_fingerprint"],
            source_seed_fingerprint=scenario["source_seed_fingerprint"],
            cp_sat_random_seed=int(seed),
            total_time_limit_seconds=STARTUP_AWARE_TOTAL_POLICY_SECONDS,
            per_operator_time_limit_seconds=STARTUP_AWARE_MAX_OPERATOR_SECONDS,
            worker_count=SEQUENCE_WORKER_COUNT,
            validation_time_limit_seconds=SEQUENCE_VALIDATION_SECONDS,
            parent_hard_wall_seconds=SEQUENCE_PARENT_HARD_WALL_SECONDS,
            fixed_cycle_names=SEQUENCE_VARIANTS[variant],
        )
        payload.update({
            "origin": "sequence_ablation",
            "fixed_cycle_request": control_request["request"],
            "fixed_cycle_request_fingerprint": control_request[
                "request_fingerprint"
            ],
        })

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
    request = payload.get("fixed_cycle_request") or {}
    if request.get("fixed_cycle"):
        return tuple(request["fixed_cycle"])
    if payload.get("policy") == "fixed_cycle":
        return tuple(
            SEQUENCE_VARIANTS["full_fixed_cycle"]
        )
    return ()


def _normalized_budget_contract(payload):
    contract = payload.get("budget_contract") or {}
    request = payload.get("fixed_cycle_request") or {}
    session_overrides = contract.get("session_overrides") or {}
    request_overrides = request.get("session_overrides") or {}
    selected_sessions = {
        name: {
            key: (request_overrides or session_overrides).get(name, {}).get(key)
            for key in (
                "session_time_limit_seconds",
                "session_max_attempts",
                "per_attempt_cp_sat_limit_seconds",
            )
        }
        for name in _fixed_cycle_names(payload)
    }
    return {
        "profile": request.get("profile") or payload.get("profile") or contract.get("profile"),
        "profile_fingerprint": request.get("profile_fingerprint")
        or payload.get("profile_fingerprint")
        or contract.get("profile_fingerprint"),
        "worker_count": request.get("worker_count")
        or payload.get("worker_count")
        or contract.get("worker_count"),
        "cp_sat_random_seed": request.get("cp_sat_random_seed")
        if request
        else payload.get("cp_sat_random_seed"),
        "per_operator_maximum_seconds": request.get(
            "per_operator_maximum_seconds",
            contract.get("per_operator_maximum_seconds"),
        ),
        "cumulative_seconds": request.get(
            "cumulative_policy_budget_seconds",
            contract.get(
                "cumulative_policy_budget_seconds",
                contract.get("cumulative_search_opportunity_seconds"),
            ),
        ),
        "candidate_validation_time_limit_seconds": request.get(
            "candidate_validation_time_limit_seconds",
            contract.get("candidate_validation_time_limit_seconds"),
        ),
        "candidate_validation_worker_count": request.get(
            "candidate_validation_worker_count",
            contract.get("candidate_validation_worker_count"),
        ),
        "parent_hard_wall_seconds": request.get(
            "parent_hard_wall_seconds",
            payload.get("hard_wall_seconds")
            or contract.get("parent_hard_wall_seconds"),
        ),
        "full_model_validation_required": request.get(
            "full_model_validation_required",
            contract.get("full_model_validation_required"),
        ),
        "ordinary_stage2_between_iterations": request.get(
            "ordinary_stage2_between_iterations",
            contract.get("ordinary_stage2_between_iterations"),
        ),
        "session_overrides": selected_sessions,
    }


def _inner_probe_projection(summary):
    fields = (
        "iteration",
        "operator",
        "radius",
        "effective_radius",
        "effective_neighborhood_radius",
        "target_scope",
        "actual_target_scope",
        "selected_grade",
        "affected_student_ids",
        "affected_section_ids",
        "candidate_found",
        "candidate_complete",
        "candidate_validated",
        "candidate_source_decision_fingerprint",
        "candidate_components",
        "component_values",
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
        "source_fingerprint_before",
        "candidate_found",
        "candidate_complete",
        "candidate_validated",
        "candidate_source_decision_fingerprint",
        "candidate_components",
        "component_values",
        "changed_source_decision_count",
        "changed_student_count",
        "gain",
        "status",
        "validation_classification",
        "validation_solver_outcome",
        "cp_sat_random_seed",
        "cp_sat_max_deterministic_time_seconds",
        "solver_wall_time_seconds",
        "validation_seconds",
        "branches",
        "conflicts",
        "best_bound",
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
        "origin": payload.get("origin"),
        "request_fingerprint": payload.get("fixed_cycle_request_fingerprint"),
        "fixed_cycle_request": payload.get("fixed_cycle_request"),
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
            "final_components": payload.get("final_components") or {},
            "final_objective_vector": payload.get("final_objective_vector") or (),
            "completeness": {
                "candidate_complete": payload.get("candidate_complete"),
                "final_unmet_count": payload.get("final_unmet_count"),
                "final_assignment_count": payload.get("final_assignment_count"),
                "final_special_commitment_count": payload.get(
                    "final_special_commitment_count"
                ),
            },
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


_PARITY_TELEMETRY_FIELDS = frozenset({
    "solver_wall_time_seconds",
    "validation_seconds",
    "branches",
    "conflicts",
    "best_bound",
})


def _semantic_parity_projection(projection):
    """Remove nondeterministic search telemetry from parity classification.

    Runtime, branch, conflict, and bound values remain in the observations and
    result artifacts for reporting.  They are not part of positive-control
    semantic parity: equivalent controls may legitimately produce different
    wall times or solver-internal counters on separate processes.  Candidate
    status, validation classification, adoption, source fingerprints, and
    final schedule facts remain compared.
    """

    semantic = dict(projection)
    semantic["attempts"] = []
    for attempt in projection.get("attempts") or ():
        semantic_attempt = {
            key: value
            for key, value in attempt.items()
            if key not in _PARITY_TELEMETRY_FIELDS
        }
        semantic["attempts"].append(semantic_attempt)
    return semantic


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
        "request_fingerprint",
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
    parent_trajectory = _semantic_parity_projection({
        "initial_substantive_value": parent["initial_substantive_value"],
        "initial_source_decision_fingerprint": parent[
            "initial_source_decision_fingerprint"
        ],
        "attempts": parent["attempts"],
        "final": parent["final"],
    })
    ablation_trajectory = _semantic_parity_projection({
        "initial_substantive_value": ablation["initial_substantive_value"],
        "initial_source_decision_fingerprint": ablation[
            "initial_source_decision_fingerprint"
        ],
        "attempts": ablation["attempts"],
        "final": ablation["final"],
    })
    trajectory_differences = _field_differences(
        parent_trajectory,
        ablation_trajectory,
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


def _parity_result_filename(scenario_id, origin, seed, repeat=0):
    suffix = f"_repeat{int(repeat)}" if int(repeat) else ""
    return f"{scenario_id}_{origin}_seed{int(seed)}{suffix}.json"


def _parity_cell_key(scenario_id, origin, seed, repeat=0):
    return ":".join(
        (scenario_id, origin, str(int(seed)), str(int(repeat)))
    )


def _utc_now():
    return datetime.now(timezone.utc)


def _utc_timestamp(value):
    return value.isoformat()


def _parse_utc(value):
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def build_fixed_cycle_parity_manifest(*, study_directory):
    """Build the external, immutable-input parity qualification manifest."""

    study_directory = Path(study_directory)
    scenarios = {
        scenario_id: _scenario_manifest(scenario_id)
        for scenario_id in SEQUENCE_SCENARIOS
    }
    started = _utc_now()
    return {
        "schema": PARITY_STUDY_SCHEMA,
        "study_id": PARITY_STUDY_ID,
        "purpose": (
            "Research-only cross-harness qualification of the fixed-cycle "
            "positive control. No application state or canonical benchmark "
            "is mutated."
        ),
        "production_wiring": False,
        "objective_semantics_version": "v2",
        "profile": SEQUENCE_PROFILE,
        "sequence": list(SEQUENCE_VARIANTS["full_fixed_cycle"]),
        "origins": list(PARITY_ORIGINS),
        "origin_labels": dict(PARITY_ORIGIN_LABELS),
        "scenario_ids": list(SEQUENCE_SCENARIOS),
        "scenarios": scenarios,
        "seeds": list(PARITY_SEEDS),
        "budget_contract": sequence_ablation_budget_contract(),
        "gating": {
            "cohort_a_seed": 101,
            "replication_seeds": [202, 303],
            "requires_seed_101_pairwise_parity": True,
            "repeat_count_after_transition_mismatch": 2,
            "one_target_cell_at_a_time": True,
            "ordinary_stage2_between_attempts": False,
        },
        "global_wall_seconds": PARITY_GLOBAL_WALL_SECONDS,
        "minimum_remaining_seconds_to_start_cell": PARITY_MIN_CELL_RESERVE_SECONDS,
        "started_at_utc": _utc_timestamp(started),
        "global_cutoff_at_utc": _utc_timestamp(
            started + timedelta(seconds=PARITY_GLOBAL_WALL_SECONDS)
        ),
        "host_preflight": None,
        "results": {},
        "comparisons": {},
        "status": "NOT_STARTED",
        "conclusion": None,
        "study_directory": str(study_directory),
        "created_at_utc": _utc_timestamp(started),
    }


def initialize_fixed_cycle_parity_study(study_directory):
    """Create a new external parity study without overwriting one."""

    study_directory = Path(study_directory)
    manifest_path = study_directory / "study_manifest.json"
    if manifest_path.exists():
        raise FileExistsError(f"Parity-study manifest already exists: {manifest_path}")
    manifest = build_fixed_cycle_parity_manifest(
        study_directory=study_directory
    )
    _json_write_atomic(manifest_path, manifest)
    return manifest


def initialize_seed303_replication_study(
    study_directory, *, parent_study_directory
):
    """Create a seed-303 continuation without rerunning seeds 101/202.

    The clean reference-target seed-303 artifacts from the completed parent
    study are inherited by reference as replicate zero.  The contaminated
    special-pressure sequence artifact is deliberately never inherited; its
    three-origin cohort is run fresh by the continuation.
    """

    study_directory = Path(study_directory)
    manifest_path = study_directory / "study_manifest.json"
    if manifest_path.exists():
        raise FileExistsError(f"Seed-303 manifest already exists: {manifest_path}")
    parent_study_directory = Path(parent_study_directory)
    parent_manifest = _parity_load_manifest(parent_study_directory)
    parent_payloads = _parity_load_payloads(
        parent_study_directory, parent_manifest
    )
    inherited = {}
    for origin in PARITY_ORIGINS:
        filename = _parity_result_filename(
            "reference_target", origin, 303, 0
        )
        metadata = (parent_manifest.get("results") or {}).get(filename)
        payload = parent_payloads.get(filename)
        if not metadata or not payload:
            raise ValueError(
                "Parent study is missing clean reference seed-303 artifact: "
                f"{filename}"
            )
        if payload.get("execution_status") != "completed":
            raise ValueError(
                f"Parent seed-303 artifact is not complete: {filename}"
            )
        if not payload.get("candidate_complete") or int(
            payload.get("final_unmet_count", 0) or 0
        ) != 0:
            raise ValueError(
                f"Parent seed-303 artifact is not unmet-free: {filename}"
            )
        inherited[filename] = {
            **metadata,
            "inherited_from_study": str(parent_study_directory),
            "inherited": True,
        }

    manifest = build_fixed_cycle_parity_manifest(
        study_directory=study_directory
    )
    manifest.update({
        "schema": SEED303_REPLICATION_SCHEMA,
        "study_id": SEED303_REPLICATION_STUDY_ID,
        "parent_study_id": parent_manifest.get("study_id"),
        "parent_study_directory": str(parent_study_directory),
        "purpose": (
            "Clean seed-303 distributional replication of the fixed-cycle "
            "cross-harness positive control."
        ),
        "seeds": [303],
        "gating": {
            "requires_seed_101_pairwise_parity": False,
            "inherited_reference_seed303_repeat0": True,
            "fresh_special_pressure_seed303_cohort": True,
            "repeat_count_after_transition_mismatch": 2,
            "one_target_cell_at_a_time": True,
            "ordinary_stage2_between_attempts": False,
        },
        "results": inherited,
        "comparisons": {},
        "status": "NOT_STARTED",
        "conclusion": None,
        "study_directory": str(study_directory),
        "created_at_utc": _utc_timestamp(_utc_now()),
        "replication_protocol": {
            "seed": 303,
            "parent_reference_replicate": "reference_target repeat0",
            "special_pressure_repeat0_is_fresh": True,
            "contamination_rule": (
                "Any cell overlapping host sleep is host_sleep_contaminated "
                "and excluded from runtime/parity conclusions."
            ),
        },
    })
    _json_write_atomic(manifest_path, manifest)
    return manifest


def _parity_preflight():
    """Capture the required host preconditions without solver-side effects."""

    try:
        git = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            capture_output=True,
            text=True,
            check=False,
        )
        git_tree_clean = not git.stdout.strip()
        git_error = git.stderr.strip() or None
    except (OSError, subprocess.SubprocessError) as error:
        git_tree_clean = None
        git_error = str(error)

    sibling_processes = []
    try:
        import psutil
    except ImportError:
        sibling_processes = None
    else:
        try:
            current_pid = os.getpid()
            for process in psutil.process_iter(("pid", "name", "cmdline")):
                if process.pid == current_pid:
                    continue
                name = str(process.info.get("name") or "").lower()
                command_line = " ".join(process.info.get("cmdline") or [])
                if "python" in name or "pytest" in command_line.lower():
                    sibling_processes.append({
                        "pid": process.pid,
                        "name": process.info.get("name"),
                        "cmdline": command_line,
                    })
        except (OSError, psutil.Error):
            sibling_processes = None

    return {
        "checked_at_utc": _utc_timestamp(_utc_now()),
        "git_tree_clean": git_tree_clean,
        "git_error": git_error,
        "sibling_python_or_pytest_processes": sibling_processes,
        "no_active_sibling_processes": sibling_processes == [],
        "power_source": "not_recorded_by_cross_platform_runner",
        "sleep_wake_audit": (
            "Review host power/system event logs after the run; the runner "
            "does not infer sleep contamination from solver wall time."
        ),
    }


def _parity_load_manifest(study_directory):
    study_directory = Path(study_directory)
    return json.loads(
        (study_directory / "study_manifest.json").read_text(encoding="utf-8")
    )


def _parity_load_payloads(study_directory, manifest=None):
    study_directory = Path(study_directory)
    manifest = manifest or _parity_load_manifest(study_directory)
    payloads = {}
    for filename, metadata in sorted((manifest.get("results") or {}).items()):
        path = Path(metadata["path"])
        if not path.is_absolute():
            path = study_directory / path
        if not path.exists():
            raise ValueError(f"Missing parity result artifact: {filename}")
        actual_hash = _sha256_file(path)
        if actual_hash != metadata.get("sha256"):
            raise ValueError(f"Parity result hash mismatch: {filename}")
        payloads[filename] = json.loads(path.read_text(encoding="utf-8"))
    return payloads


def _parity_request_for_cell(manifest, scenario_id, seed, origin):
    scenario = manifest["scenarios"][scenario_id]
    names = SEQUENCE_VARIANTS["full_fixed_cycle"]
    # The sequence-ablation origin explicitly supplies the canonical tuple;
    # the other two origins use the builder's canonical default. Both must
    # resolve to the same request fingerprint.
    return fixed_cycle_control_request(
        profile=manifest["profile"],
        benchmark_directory=_REPOSITORY_ROOT / scenario["benchmark_directory"],
        input_fingerprint=scenario["input_fingerprint"],
        source_seed_fingerprint=scenario["source_seed_fingerprint"],
        cp_sat_random_seed=int(seed),
        total_time_limit_seconds=STARTUP_AWARE_TOTAL_POLICY_SECONDS,
        per_operator_time_limit_seconds=STARTUP_AWARE_MAX_OPERATOR_SECONDS,
        worker_count=SEQUENCE_WORKER_COUNT,
        validation_time_limit_seconds=SEQUENCE_VALIDATION_SECONDS,
        parent_hard_wall_seconds=SEQUENCE_PARENT_HARD_WALL_SECONDS,
        fixed_cycle_names=(names if origin == "sequence_ablation" else None),
    )


def run_fixed_cycle_parity_cell(
    *, study_directory, scenario_id, origin, seed, repeat=0
):
    """Run one origin's control and publish only its external result artifact."""

    if scenario_id not in SEQUENCE_SCENARIOS:
        raise ValueError(f"Unknown parity scenario: {scenario_id}")
    if origin not in PARITY_ORIGINS:
        raise ValueError(f"Unknown parity origin: {origin}")
    if int(seed) not in PARITY_SEEDS:
        raise ValueError(f"Seed is not preregistered: {seed}")
    manifest = _parity_load_manifest(study_directory)
    scenario = manifest["scenarios"][scenario_id]
    filename = _parity_result_filename(scenario_id, origin, seed, repeat)
    result_path = Path(study_directory) / "results" / filename
    if filename in manifest.get("results", {}) or result_path.exists():
        raise FileExistsError(f"Parity result artifact already exists: {result_path}")

    started = perf_counter()
    request = _parity_request_for_cell(manifest, scenario_id, seed, origin)
    try:
        payload = dict(
            run_supervised_calibration_trial(**request["run_kwargs"])
        )
        payload.update({
            "schema": PARITY_RESULT_SCHEMA,
            "study_id": manifest["study_id"],
            "scenario_id": scenario_id,
            "origin": origin,
            "origin_label": PARITY_ORIGIN_LABELS[origin],
            "seed": int(seed),
            "repeat": int(repeat),
            "source_lineage": {
                "input_fingerprint": scenario["input_fingerprint"],
                "source_seed_fingerprint": scenario["source_seed_fingerprint"],
            },
            "sequence": list(SEQUENCE_VARIANTS["full_fixed_cycle"]),
            "fixed_cycle_request": request["request"],
            "fixed_cycle_request_fingerprint": request[
                "request_fingerprint"
            ],
            "parity_cell_key": _parity_cell_key(
                scenario_id, origin, seed, repeat
            ),
            "cell_elapsed_seconds": perf_counter() - started,
        })
    except Exception as error:
        payload = {
            "schema": PARITY_RESULT_SCHEMA,
            "study_id": manifest["study_id"],
            "scenario_id": scenario_id,
            "origin": origin,
            "origin_label": PARITY_ORIGIN_LABELS[origin],
            "seed": int(seed),
            "repeat": int(repeat),
            "status": "source_or_runner_error",
            "error_type": type(error).__name__,
            "error": str(error),
            "source_lineage": {
                "input_fingerprint": scenario["input_fingerprint"],
                "source_seed_fingerprint": scenario["source_seed_fingerprint"],
            },
            "sequence": list(SEQUENCE_VARIANTS["full_fixed_cycle"]),
            "fixed_cycle_request": request["request"],
            "fixed_cycle_request_fingerprint": request["request_fingerprint"],
            "parity_cell_key": _parity_cell_key(
                scenario_id, origin, seed, repeat
            ),
            "cell_elapsed_seconds": perf_counter() - started,
        }

    _json_write_atomic(result_path, payload)
    manifest["results"][filename] = {
        "path": str(result_path),
        "sha256": _sha256_file(result_path),
        "origin": origin,
        "scenario_id": scenario_id,
        "seed": int(seed),
        "repeat": int(repeat),
        "status": payload.get("execution_status") or payload.get("status"),
    }
    _json_write_atomic(Path(study_directory) / "study_manifest.json", manifest)
    return payload


def _parity_compare_cohort(manifest, payloads, seed, repeat=0):
    comparisons = {}
    for scenario_id in SEQUENCE_SCENARIOS:
        selected = {}
        for origin in PARITY_ORIGINS:
            filename = _parity_result_filename(
                scenario_id, origin, seed, repeat
            )
            if filename in payloads:
                selected[origin] = payloads[filename]
        if len(selected) != len(PARITY_ORIGINS):
            continue
        for right_origin in ("sequence_ablation", "parallel_policy"):
            result = compare_fixed_cycle_control_payloads(
                selected["startup_aware_parent"], selected[right_origin]
            )
            result.update({
                "left_origin": "startup_aware_parent",
                "right_origin": right_origin,
                "scenario_id": scenario_id,
                "seed": int(seed),
                "repeat": int(repeat),
            })
            key = (
                f"seed{int(seed)}:{scenario_id}:"
                f"startup_aware_parent-vs-{right_origin}:repeat{int(repeat)}"
            )
            comparisons[key] = result
    return comparisons


def _parity_hard_wall_payload(payload):
    status = str(payload.get("execution_status") or payload.get("status") or "")
    return status in {
        "hard_deadline_terminated",
        "resource_guard_terminated",
        "parent_cancelled",
    }


def _parity_cohort_classification(comparisons, seed, repeat=0):
    cohort = {
        key: value
        for key, value in comparisons.items()
        if value.get("seed") == int(seed)
        and value.get("repeat") == int(repeat)
    }
    if len(cohort) != len(SEQUENCE_SCENARIOS) * 2:
        return "INCOMPLETE_COHORT"
    classes = {item.get("classification") for item in cohort.values()}
    if "CONFIGURATION_NON_PARITY" in classes:
        return "CONFIGURATION_NON_PARITY"
    if "PARITY_MATCH" in classes and len(classes) == 1:
        return "PARITY_MATCH"
    if "VALIDATION_TRANSITION_VARIANCE" in classes:
        return "VALIDATION_TRANSITION_VARIANCE"
    return "TRAJECTORY_NON_PARITY"


def _parity_conclusion(manifest):
    status = manifest.get("status")
    if status == "PARITY_QUALIFIED":
        return "ABLATION HARNESS PARITY RESTORED — CAUSAL SEQUENCE STUDY MAY RESUME"
    if status == "SOLVER_TRANSITION_VARIANCE":
        return "CONFIGURATION PARITY RESTORED BUT SOLVER TRANSITION VARIANCE REQUIRES CONTROLLED REPLICATION"
    if status == "CONFIGURATION_NON_PARITY":
        return "ABLATON/PARALLEL HARNESS REMAINS NON-PARITY — DO NOT USE FOR CAUSAL POLICY RANKING"
    if status == "SUPERVISION_BLOCKED":
        if manifest.get("host_sleep_contaminated"):
            return "CONTROLLED SEED-303 QUALIFICATION REMAINS INCONCLUSIVE"
        return "SUPERVISION/HARD-WALL DEFECT BLOCKS FURTHER TARGET ABLATION"
    return None


def _parity_persist_manifest(study_directory, manifest):
    manifest["conclusion"] = _parity_conclusion(manifest)
    _json_write_atomic(Path(study_directory) / "study_manifest.json", manifest)
    return manifest


def run_fixed_cycle_parity_study(study_directory, *, enforce_preconditions=True):
    """Run the gated, sequential parity qualification study.

    A single call may resume an incomplete external manifest.  It never
    writes an existing startup-aware, sequence-ablation, or parallel-study
    artifact.  Tests may explicitly disable ``enforce_preconditions`` when
    exercising the queue with mocked trials.
    """

    study_directory = Path(study_directory)
    manifest = _parity_load_manifest(study_directory)
    if manifest.get("status") in {
        "PARITY_QUALIFIED",
        "SOLVER_TRANSITION_VARIANCE",
        "CONFIGURATION_NON_PARITY",
        "SUPERVISION_BLOCKED",
    }:
        return _parity_persist_manifest(study_directory, manifest)
    preflight = _parity_preflight()
    manifest["host_preflight"] = preflight
    if enforce_preconditions and (
        preflight.get("git_tree_clean") is not True
        or preflight.get("no_active_sibling_processes") is not True
    ):
        manifest["status"] = "PRECONDITION_BLOCKED"
        return _parity_persist_manifest(study_directory, manifest)

    cutoff = _parse_utc(manifest.get("global_cutoff_at_utc"))
    if cutoff is None:
        raise ValueError("Parity manifest has no global cutoff")
    payloads = _parity_load_payloads(study_directory, manifest)
    manifest["status"] = "RUNNING"
    _parity_persist_manifest(study_directory, manifest)

    def run_cells(cells):
        nonlocal manifest, payloads
        for scenario_id, origin, seed, repeat in cells:
            if _utc_now().timestamp() + PARITY_MIN_CELL_RESERVE_SECONDS >= cutoff.timestamp():
                manifest["status"] = "CUTOFF_REACHED"
                _parity_persist_manifest(study_directory, manifest)
                return False
            filename = _parity_result_filename(
                scenario_id, origin, seed, repeat
            )
            if filename in payloads:
                continue
            payload = run_fixed_cycle_parity_cell(
                study_directory=study_directory,
                scenario_id=scenario_id,
                origin=origin,
                seed=seed,
                repeat=repeat,
            )
            manifest = _parity_load_manifest(study_directory)
            payloads[filename] = payload
            if _parity_hard_wall_payload(payload):
                manifest["status"] = "SUPERVISION_BLOCKED"
                _parity_persist_manifest(study_directory, manifest)
                return False
        return True

    study_seeds = tuple(
        int(seed) for seed in (manifest.get("seeds") or PARITY_SEEDS)
    )
    requires_seed_gate = bool(
        (manifest.get("gating") or {}).get(
            "requires_seed_101_pairwise_parity", True
        )
    )
    for seed_index, seed in enumerate(study_seeds):
        initial_cells = tuple(
            (scenario_id, origin, seed, 0)
            for scenario_id in SEQUENCE_SCENARIOS
            for origin in PARITY_ORIGINS
        )
        if requires_seed_gate and seed_index > 0:
            previous = _parity_cohort_classification(
                manifest.get("comparisons", {}), study_seeds[0], 0
            )
            if previous != "PARITY_MATCH":
                break
        if not run_cells(initial_cells):
            break
        manifest = _parity_load_manifest(study_directory)
        payloads = _parity_load_payloads(study_directory, manifest)
        cohort_comparisons = _parity_compare_cohort(
            manifest, payloads, seed, 0
        )
        manifest["comparisons"].update(cohort_comparisons)
        classification = _parity_cohort_classification(
            manifest["comparisons"], seed, 0
        )
        if classification == "CONFIGURATION_NON_PARITY":
            manifest["status"] = "CONFIGURATION_NON_PARITY"
            break
        if classification in {
            "VALIDATION_TRANSITION_VARIANCE",
            "TRAJECTORY_NON_PARITY",
        }:
            affected_scenarios = {
                result["scenario_id"]
                for result in cohort_comparisons.values()
                if result.get("classification") != "PARITY_MATCH"
            }
            for repeat in (1, 2):
                repeat_cells = tuple(
                    (scenario_id, origin, seed, repeat)
                    for scenario_id in sorted(affected_scenarios)
                    for origin in PARITY_ORIGINS
                )
                if not run_cells(repeat_cells):
                    break
            manifest = _parity_load_manifest(study_directory)
            if manifest.get("status") in {"CUTOFF_REACHED", "SUPERVISION_BLOCKED"}:
                break
            payloads = _parity_load_payloads(study_directory, manifest)
            for repeat in (1, 2):
                manifest["comparisons"].update(
                    _parity_compare_cohort(manifest, payloads, seed, repeat)
                )
            manifest["status"] = "SOLVER_TRANSITION_VARIANCE"
            break
        _parity_persist_manifest(study_directory, manifest)
    else:
        manifest["status"] = "PARITY_QUALIFIED"

    return _parity_persist_manifest(study_directory, manifest)


def summarize_fixed_cycle_parity_study(study_directory):
    """Verify external result hashes and return a compact parity summary."""

    manifest = _parity_load_manifest(study_directory)
    payloads = _parity_load_payloads(study_directory, manifest)
    comparisons = dict(manifest.get("comparisons") or {})
    study_seeds = tuple(
        int(seed) for seed in (manifest.get("seeds") or PARITY_SEEDS)
    )
    for seed in study_seeds:
        for repeat in (0, 1, 2):
            comparisons.update(
                _parity_compare_cohort(manifest, payloads, seed, repeat)
            )
    classifications = [
        item.get("classification") for item in comparisons.values()
    ]
    return {
        "schema": PARITY_SUMMARY_SCHEMA,
        "study_id": manifest["study_id"],
        "status": manifest.get("status"),
        "conclusion": manifest.get("conclusion") or _parity_conclusion(manifest),
        "artifact_integrity": {
            "manifest_result_count": len(manifest.get("results") or {}),
            "loaded_result_count": len(payloads),
            "all_result_hashes_verified": True,
        },
        "cohort_classifications": {
            f"seed{seed}:repeat{repeat}": _parity_cohort_classification(
                comparisons, seed, repeat
            )
            for seed in study_seeds
            for repeat in (0, 1, 2)
        },
        "classification_counts": {
            value: classifications.count(value)
            for value in sorted(set(classifications))
        },
        "comparisons": comparisons,
        "host_preflight": manifest.get("host_preflight"),
    }


def write_fixed_cycle_parity_summary(study_directory):
    study_directory = Path(study_directory)
    summary = summarize_fixed_cycle_parity_study(study_directory)
    summary_path = study_directory / "study_summary.json"
    _json_write_atomic(summary_path, summary)
    manifest = _parity_load_manifest(study_directory)
    manifest["summary_artifact"] = {
        "path": str(summary_path),
        "sha256": _sha256_file(summary_path),
    }
    _json_write_atomic(study_directory / "study_manifest.json", manifest)
    return summary


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
    parser.add_argument(
        "--initialize-parity",
        action="store_true",
        help="Create an external fixed-cycle cross-harness parity study.",
    )
    parser.add_argument(
        "--run-parity",
        action="store_true",
        help="Run the next gated sequential parity cohort.",
    )
    parser.add_argument(
        "--allow-dirty-preflight",
        action="store_true",
        help=(
            "Run with the expected implementation tree changes recorded "
            "instead of enforcing the clean-tree precondition."
        ),
    )
    parser.add_argument(
        "--summarize-parity",
        action="store_true",
        help="Verify parity artifacts and write a compact summary.",
    )
    parser.add_argument(
        "--initialize-seed303-replication",
        action="store_true",
        help="Create a separate seed-303 parity continuation study.",
    )
    parser.add_argument(
        "--parent-study-directory",
        type=Path,
        help="Existing parity study used as the seed-303 continuation parent.",
    )
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
    action_flags = (
        args.initialize,
        args.summarize,
        args.initialize_parity,
        args.run_parity,
        args.summarize_parity,
        args.initialize_seed303_replication,
    )
    if sum(bool(value) for value in action_flags) > 1:
        parser.error("study action flags are mutually exclusive")
    if args.allow_dirty_preflight and not args.run_parity:
        parser.error("--allow-dirty-preflight requires --run-parity")
    if args.initialize_seed303_replication and not args.parent_study_directory:
        parser.error(
            "--initialize-seed303-replication requires "
            "--parent-study-directory"
        )
    if args.parent_study_directory and not args.initialize_seed303_replication:
        parser.error(
            "--parent-study-directory requires "
            "--initialize-seed303-replication"
        )
    if any(value is not None for value in compare_args) and (
        any(action_flags) or args.scenario or args.variant or args.seed
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
    elif args.initialize_parity:
        payload = initialize_fixed_cycle_parity_study(args.study_directory)
    elif args.initialize_seed303_replication:
        payload = initialize_seed303_replication_study(
            args.study_directory,
            parent_study_directory=args.parent_study_directory,
        )
    elif args.run_parity:
        payload = run_fixed_cycle_parity_study(
            args.study_directory,
            enforce_preconditions=not args.allow_dirty_preflight,
        )
    elif args.summarize_parity:
        payload = write_fixed_cycle_parity_summary(args.study_directory)
    else:
        if args.scenario is None or args.variant is None or args.seed is None:
            parser.error("--scenario, --variant, and --seed are required")
        payload = run_sequence_ablation_cell(
            study_directory=args.study_directory,
            scenario_id=args.scenario,
            variant=args.variant,
            seed=args.seed,
        )
    if (
        args.compare_parent is not None
        or any(action_flags)
    ):
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
    "PARITY_GLOBAL_WALL_SECONDS",
    "PARITY_MIN_CELL_RESERVE_SECONDS",
    "PARITY_ORIGINS",
    "PARITY_RESULT_SCHEMA",
    "PARITY_STUDY_ID",
    "PARITY_STUDY_SCHEMA",
    "PARITY_SUMMARY_SCHEMA",
    "build_fixed_cycle_parity_manifest",
    "build_sequence_ablation_manifest",
    "compare_fixed_cycle_control_payloads",
    "fixed_cycle_parity_projection",
    "initialize_fixed_cycle_parity_study",
    "initialize_seed303_replication_study",
    "initialize_sequence_ablation_study",
    "run_fixed_cycle_parity_cell",
    "run_fixed_cycle_parity_study",
    "run_sequence_ablation_cell",
    "sequence_ablation_budget_contract",
    "summarize_fixed_cycle_parity_study",
    "summarize_sequence_ablation_study",
    "write_fixed_cycle_parity_diff",
    "write_fixed_cycle_parity_summary",
    "write_sequence_ablation_summary",
]
