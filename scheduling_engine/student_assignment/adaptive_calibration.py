"""Matched offline calibration support for the v2 adaptive allocator.

This module owns experiment protocol data only.  It does not add scheduling
constraints, alter Objective Semantics, or authorize candidates.  Every trial
delegates to the existing adaptive runtime, whose CP-SAT and full-model
validation boundaries remain authoritative.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import hashlib
import json

from .adaptive_runtime import AdaptiveSessionResult, run_adaptive_local_search_diagnostic
from .adaptive_search import DEFAULT_ADAPTIVE_OPERATOR_PORTFOLIO
from .runtime import semantic_student_assignment_input_fingerprint


CALIBRATION_PROTOCOL_VERSION = "adaptive-calibration-v1"

# These are pre-registered diagnostic profiles over the existing canonical v2
# 0--10 score representation.  They are not production presets and do not
# change the meaning of any objective.
CALIBRATION_PROFILES = {
    "balanced": {
        "section_utilization_balance": 6,
        "student_semester_balance": 6,
        "course_sequence_preferences": 6,
        "difficulty_balance": 6,
        "course_category_diversity": 6,
    },
    "student_quality_heavy": {
        "section_utilization_balance": 2,
        "student_semester_balance": 10,
        "course_sequence_preferences": 8,
        "difficulty_balance": 8,
        "course_category_diversity": 8,
    },
    "utilization_heavy": {
        "section_utilization_balance": 10,
        "student_semester_balance": 2,
        "course_sequence_preferences": 2,
        "difficulty_balance": 2,
        "course_category_diversity": 2,
    },
    "difficulty_category_heavy": {
        "section_utilization_balance": 2,
        "student_semester_balance": 2,
        "course_sequence_preferences": 2,
        "difficulty_balance": 10,
        "course_category_diversity": 10,
    },
    "sequence_heavy": {
        "section_utilization_balance": 2,
        "student_semester_balance": 2,
        "course_sequence_preferences": 10,
        "difficulty_balance": 2,
        "course_category_diversity": 2,
    },
}

# The session sizes are intentionally separate from the policy.  All policies
# receive the same outer budget; these values only give a selected family a
# useful reusable-session granularity during offline calibration.
CALIBRATION_SESSION_OVERRIDES = {
    "r2": {
        "session_time_limit_seconds": 600.0,
        "session_max_attempts": 5,
        "per_attempt_cp_sat_limit_seconds": 120.0,
    },
    "targeted_r4_s1": {
        "session_time_limit_seconds": 180.0,
        "session_max_attempts": 5,
        "per_attempt_cp_sat_limit_seconds": 30.0,
    },
    "targeted_r8_s1": {
        "session_time_limit_seconds": 180.0,
        "session_max_attempts": 5,
        "per_attempt_cp_sat_limit_seconds": 30.0,
    },
    "targeted_r4_s2": {
        "session_time_limit_seconds": 180.0,
        "session_max_attempts": 5,
        "per_attempt_cp_sat_limit_seconds": 30.0,
    },
    "targeted_r8_s2": {
        "session_time_limit_seconds": 180.0,
        "session_max_attempts": 5,
        "per_attempt_cp_sat_limit_seconds": 30.0,
    },
    "targeted_utilization_r16_s2": {
        "session_time_limit_seconds": 180.0,
        "session_max_attempts": 5,
        "per_attempt_cp_sat_limit_seconds": 30.0,
    },
    "targeted_utilization_r16_s4": {
        "session_time_limit_seconds": 180.0,
        "session_max_attempts": 5,
        "per_attempt_cp_sat_limit_seconds": 30.0,
    },
    "targeted_utilization_r32_s4": {
        "session_time_limit_seconds": 180.0,
        "session_max_attempts": 5,
        "per_attempt_cp_sat_limit_seconds": 30.0,
    },
    "targeted_utilization_r32_s6": {
        "session_time_limit_seconds": 180.0,
        "session_max_attempts": 5,
        "per_attempt_cp_sat_limit_seconds": 30.0,
    },
    "targeted_utilization_r64_s6": {
        "session_time_limit_seconds": 180.0,
        "session_max_attempts": 5,
        "per_attempt_cp_sat_limit_seconds": 30.0,
    },
    "targeted_utilization_r64_s8": {
        "session_time_limit_seconds": 180.0,
        "session_max_attempts": 5,
        "per_attempt_cp_sat_limit_seconds": 30.0,
    },
    "targeted_utilization_r64_s10": {
        "session_time_limit_seconds": 180.0,
        "session_max_attempts": 5,
        "per_attempt_cp_sat_limit_seconds": 30.0,
    },
    "grade_bounded_g9": {
        "session_time_limit_seconds": 240.0,
        "session_max_attempts": 1,
        "per_attempt_cp_sat_limit_seconds": 180.0,
    },
    "grade_bounded_g10": {
        "session_time_limit_seconds": 240.0,
        "session_max_attempts": 1,
        "per_attempt_cp_sat_limit_seconds": 180.0,
    },
    "grade_bounded_g11": {
        "session_time_limit_seconds": 240.0,
        "session_max_attempts": 1,
        "per_attempt_cp_sat_limit_seconds": 180.0,
    },
    "grade_bounded_g12": {
        "session_time_limit_seconds": 240.0,
        "session_max_attempts": 1,
        "per_attempt_cp_sat_limit_seconds": 180.0,
    },
}

CALIBRATION_FIXED_CYCLES = {
    "r2_only": ("r2",),
    "student_repair_only": ("targeted_r4_s2",),
    # A named R8-only control keeps the broader student-pressure family
    # independently measurable without changing the ordinary scheduler.
    "student_repair_r8_only": ("targeted_r8_s2",),
    "utilization_only": ("targeted_utilization_r64_s8",),
    "fixed_cycle": (
        "targeted_r4_s2",
        "targeted_utilization_r64_s8",
        "r2",
    ),
}


def profile_fingerprint(profile_name):
    """Return a stable identity for one pre-registered score profile."""

    try:
        scores = CALIBRATION_PROFILES[profile_name]
    except KeyError as error:
        raise ValueError(f"Unknown calibration profile: {profile_name!r}") from error
    payload = json.dumps(
        {"protocol": CALIBRATION_PROTOCOL_VERSION, "scores": scores},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def apply_calibration_profile(data, profile_name):
    """Return a DTO with one explicit v2 score profile applied."""

    try:
        scores = CALIBRATION_PROFILES[profile_name]
    except KeyError as error:
        raise ValueError(f"Unknown calibration profile: {profile_name!r}") from error
    return replace(
        data,
        objective_semantics_version="v2",
        objective_importance_scores=dict(scores),
    )


def _operator_by_name(name, portfolio):
    for spec in portfolio:
        if spec.name == name:
            return spec
    raise ValueError(f"Operator {name!r} is not in the supplied portfolio")


def build_calibration_policy(
    policy_name,
    *,
    portfolio=DEFAULT_ADAPTIVE_OPERATOR_PORTFOLIO,
):
    """Return the runtime selector configuration for a matched control."""

    if policy_name == "adaptive":
        return {"selection_policy": "adaptive", "fixed_cycle": ()}
    if policy_name == "stateless_role":
        return {"selection_policy": "stateless_role", "fixed_cycle": ()}
    if policy_name in CALIBRATION_FIXED_CYCLES:
        return {
            "selection_policy": "fixed_cycle",
            "fixed_cycle": tuple(
                _operator_by_name(name, portfolio)
                for name in CALIBRATION_FIXED_CYCLES[policy_name]
            ),
        }
    raise ValueError(f"Unknown calibration policy: {policy_name!r}")


@dataclass(frozen=True)
class AdaptiveCalibrationTrialRecord:
    """Bounded JSON-safe facts from one matched policy trial."""

    schema: str
    protocol_version: str
    policy: str
    profile: str
    profile_fingerprint: str
    input_fingerprint: str
    source_seed_fingerprint: str
    configured_budget_seconds: float
    per_operator_time_limit_seconds: float
    worker_count: int
    cp_sat_random_seed: int | None
    cp_sat_max_deterministic_time_seconds: float | None
    initial_substantive_value: float
    final_substantive_value: float
    final_assignment_count: int
    final_unmet_count: int
    final_special_commitment_count: int
    candidate_complete: bool
    attempts: tuple
    decisions: tuple
    timing: dict
    final_components: dict
    resource: dict

    def to_dict(self):
        return asdict(self)

    def to_json(self):
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


def run_matched_calibration_trial(
    data,
    *,
    initial_result,
    initial_source_decisions,
    policy,
    profile="balanced",
    total_time_limit_seconds=180.0,
    per_operator_time_limit_seconds=60.0,
    worker_count=8,
    portfolio=DEFAULT_ADAPTIVE_OPERATOR_PORTFOLIO,
    session_overrides=CALIBRATION_SESSION_OVERRIDES,
    collect_resource_telemetry=True,
    cp_sat_random_seed=None,
    cp_sat_max_deterministic_time_seconds=None,
    **kwargs,
):
    """Run one policy from one complete incumbent under a shared budget."""

    data = apply_calibration_profile(data, profile)
    if initial_result.status != "complete" or initial_result.unmet_requests:
        raise ValueError("Calibration requires a complete, unmet-free incumbent")
    source_decisions = tuple(initial_source_decisions)
    if not source_decisions:
        raise ValueError("Calibration requires semantic source decisions")
    policy_config = build_calibration_policy(policy, portfolio=portfolio)
    result = run_adaptive_local_search_diagnostic(
        data,
        initial_result=initial_result,
        initial_source_decisions=source_decisions,
        total_time_limit_seconds=total_time_limit_seconds,
        per_operator_time_limit_seconds=per_operator_time_limit_seconds,
        worker_count=worker_count,
        portfolio=portfolio,
        session_overrides=session_overrides,
        collect_resource_telemetry=collect_resource_telemetry,
        cp_sat_random_seed=cp_sat_random_seed,
        cp_sat_max_deterministic_time_seconds=(
            cp_sat_max_deterministic_time_seconds
        ),
        selection_policy=policy_config["selection_policy"],
        fixed_cycle=policy_config["fixed_cycle"],
        **kwargs,
    )
    return build_calibration_trial_record(
        data,
        initial_result=initial_result,
        initial_source_decisions=source_decisions,
        policy=policy,
        profile=profile,
        result=result,
        total_time_limit_seconds=total_time_limit_seconds,
        per_operator_time_limit_seconds=per_operator_time_limit_seconds,
        worker_count=worker_count,
        cp_sat_random_seed=cp_sat_random_seed,
        cp_sat_max_deterministic_time_seconds=(
            cp_sat_max_deterministic_time_seconds
        ),
    )


def _weighted_value(result):
    components = dict(result.objective_components or {})
    weighted = components.get("weighted_normalized_contributions")
    if weighted is not None:
        return float(sum(float(value or 0) for value in weighted.values()))
    return float(
        sum(
            float(components.get(key, 0) or 0)
            for key in (
                "section_utilization_balance_penalty",
                "student_semester_balance_penalty",
                "difficulty_balance_penalty",
                "course_category_diversity_penalty",
            )
        )
    )


def build_calibration_trial_record(
    data,
    *,
    initial_result,
    initial_source_decisions,
    policy,
    profile,
    result: AdaptiveSessionResult,
    total_time_limit_seconds,
    per_operator_time_limit_seconds,
    worker_count,
    cp_sat_random_seed=None,
    cp_sat_max_deterministic_time_seconds=None,
):
    """Translate a runtime result into a bounded calibration record."""

    # Import lazily because the public student_assignment package is also
    # imported by generated fixture modules used by stage2_benchmark. Keeping
    # this benchmark-only helper out of module initialization avoids a package
    # import cycle while retaining one canonical semantic fingerprint.
    from .stage2_benchmark import semantic_stage1_seed_source_fingerprint

    record = result.record
    return AdaptiveCalibrationTrialRecord(
        schema="student_assignment_adaptive_calibration_trial_v1",
        protocol_version=CALIBRATION_PROTOCOL_VERSION,
        policy=policy,
        profile=profile,
        profile_fingerprint=profile_fingerprint(profile),
        input_fingerprint=semantic_student_assignment_input_fingerprint(data),
        source_seed_fingerprint=semantic_stage1_seed_source_fingerprint(
            data, initial_source_decisions
        ),
        configured_budget_seconds=float(total_time_limit_seconds),
        per_operator_time_limit_seconds=float(per_operator_time_limit_seconds),
        worker_count=int(worker_count),
        cp_sat_random_seed=(
            int(cp_sat_random_seed) if cp_sat_random_seed is not None else None
        ),
        cp_sat_max_deterministic_time_seconds=(
            cp_sat_max_deterministic_time_seconds
        ),
        initial_substantive_value=_weighted_value(initial_result),
        final_substantive_value=_weighted_value(result.result),
        final_assignment_count=len(result.result.assignments),
        final_unmet_count=len(result.result.unmet_requests),
        final_special_commitment_count=len(result.result.commitment_assignments),
        candidate_complete=(
            result.result.status == "complete"
            and not result.result.unmet_requests
        ),
        attempts=tuple(record.attempts),
        decisions=tuple(record.decisions),
        timing={
            "policy_selection_seconds": record.policy_selection_seconds,
            "operator_execution_seconds": record.operator_execution_seconds,
            "finalization_seconds": record.finalization_seconds,
            "external_overrun_seconds": record.external_overrun_seconds,
            "total_elapsed_seconds": record.elapsed_seconds,
            "phase_timings": dict(record.phase_timings),
        },
        final_components=dict(record.final_components),
        resource=dict(record.resource),
    )


__all__ = [
    "CALIBRATION_SESSION_OVERRIDES",
    "CALIBRATION_FIXED_CYCLES",
    "CALIBRATION_PROFILES",
    "CALIBRATION_PROTOCOL_VERSION",
    "AdaptiveCalibrationTrialRecord",
    "apply_calibration_profile",
    "build_calibration_policy",
    "build_calibration_trial_record",
    "profile_fingerprint",
    "run_matched_calibration_trial",
]
