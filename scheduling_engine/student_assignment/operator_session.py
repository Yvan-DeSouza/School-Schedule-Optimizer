"""Pure-engine contracts for continuous diagnostic operator sessions.

This module describes a session and its diagnostic record. It does not build
CP-SAT models or decide whether a candidate is valid; the existing student
assignment core remains the authority for both operations.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
from uuid import uuid4

from .runtime import semantic_student_assignment_input_fingerprint
from ..constants import VALID_STUDENT_GRADE_LEVELS


OPERATOR_FAMILIES = (
    "r2",
    "targeted_r4_s1",
    "targeted_r8_s1",
    "targeted_r4_s2",
    "targeted_r8_s2",
    "targeted_utilization_r16_s2",
    "targeted_utilization_r16_s4",
    "targeted_utilization_r32_s4",
    "targeted_utilization_r32_s6",
    "targeted_utilization_r64_s6",
    "targeted_utilization_r64_s8",
    "targeted_utilization_r64_s10",
    "grade_bounded_g9",
    "grade_bounded_g10",
    "grade_bounded_g11",
    "grade_bounded_g12",
)
TARGET_POLICIES = ("dynamic", "fixed")


def operator_session_target_count(operator_family):
    """Return the semantic student scope for a supported operator family."""

    return {
        "r2": None,
        "targeted_r4_s1": 1,
        "targeted_r8_s1": 1,
        "targeted_r4_s2": 2,
        "targeted_r8_s2": 2,
        "targeted_utilization_r16_s2": 2,
        "targeted_utilization_r16_s4": 4,
        "targeted_utilization_r32_s4": 4,
        "targeted_utilization_r32_s6": 6,
        "targeted_utilization_r64_s6": 6,
        "targeted_utilization_r64_s8": 8,
        "targeted_utilization_r64_s10": 10,
        "grade_bounded_g9": None,
        "grade_bounded_g10": None,
        "grade_bounded_g11": None,
        "grade_bounded_g12": None,
    }[operator_family]


def select_operator_session_targets(
    operator_family,
    *,
    target_policy,
    ranked_student_ids=(),
    fixed_student_ids=(),
):
    """Select bounded targets without authorizing a schedule.

    Dynamic targets are recalculated by the engine from the current incumbent
    after each adopted candidate. Fixed targets are normalized once and reused
    for every attempt. The returned IDs only restrict a diagnostic CP-SAT
    neighborhood; CP-SAT and full-model validation remain authoritative.
    """

    count = operator_session_target_count(operator_family)
    if count is None:
        return ()
    if target_policy == "fixed":
        selected = tuple(sorted(set(fixed_student_ids), key=repr))
        return selected if len(selected) == count else ()
    if target_policy != "dynamic":
        raise ValueError(f"Unsupported target policy: {target_policy}")
    return tuple(
        sorted(set(tuple(ranked_student_ids)[:count]), key=repr)
    )


@dataclass(frozen=True)
class ContinuousOperatorSessionConfig:
    """Validated configuration for one fixed-family local session."""

    operator_family: str = "r2"
    total_time_limit_seconds: float = 600.0
    max_attempts: int = 10
    per_attempt_time_limit_seconds: float = 60.0
    worker_count: int = 8
    target_policy: str = "dynamic"
    selected_student_ids: tuple = ()
    utilization_cluster_policy: str = "interaction_aware"
    selected_grade: int | None = None
    minimum_next_attempt_seconds: float = 1.0
    collect_resource_telemetry: bool = True
    hard_feasibility_validation_time_limit_seconds: float | None = None
    hard_feasibility_validation_worker_count: int | None = None
    candidate_validation_time_limit_seconds: float | None = None
    cp_sat_random_seed: int | None = None
    cp_sat_max_deterministic_time_seconds: float | None = None

    def __post_init__(self):
        if self.operator_family not in OPERATOR_FAMILIES:
            raise ValueError(f"Unsupported operator family: {self.operator_family}")
        if self.target_policy not in TARGET_POLICIES:
            raise ValueError(f"Unsupported target policy: {self.target_policy}")
        if self.total_time_limit_seconds <= 0:
            raise ValueError("total_time_limit_seconds must be positive")
        if self.max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        if self.per_attempt_time_limit_seconds <= 0:
            raise ValueError("per_attempt_time_limit_seconds must be positive")
        if self.worker_count <= 0:
            raise ValueError("worker_count must be positive")
        if self.minimum_next_attempt_seconds < 0:
            raise ValueError("minimum_next_attempt_seconds cannot be negative")
        if (
            self.candidate_validation_time_limit_seconds is not None
            and self.candidate_validation_time_limit_seconds <= 0
        ):
            raise ValueError(
                "candidate_validation_time_limit_seconds must be positive"
            )
        if (
            self.cp_sat_max_deterministic_time_seconds is not None
            and self.cp_sat_max_deterministic_time_seconds <= 0
        ):
            raise ValueError(
                "cp_sat_max_deterministic_time_seconds must be positive"
            )
        if self.utilization_cluster_policy not in {
            "top_individual",
            "delivery_group_focused",
            "interaction_aware",
            "mixed",
        }:
            raise ValueError(
                "Unsupported utilization_cluster_policy: "
                f"{self.utilization_cluster_policy}"
            )
        if self.operator_family == "r2" and self.selected_student_ids:
            raise ValueError("r2 does not accept targeted student IDs")
        grade_bounded = self.operator_family.startswith("grade_bounded_")
        if grade_bounded:
            if self.selected_grade not in VALID_STUDENT_GRADE_LEVELS:
                raise ValueError(
                    "Grade-bounded operators require selected_grade 9, 10, 11, or 12"
                )
            operator_grade = int(self.operator_family.rsplit("g", 1)[1])
            if self.selected_grade != operator_grade:
                raise ValueError(
                    f"{self.operator_family} requires selected_grade {operator_grade}"
                )
            if self.selected_student_ids:
                raise ValueError("Grade-bounded operators do not accept student IDs")
            if self.target_policy != "fixed":
                raise ValueError("Grade-bounded operators require fixed targeting")
        elif self.selected_grade is not None:
            raise ValueError("selected_grade is only valid for grade-bounded operators")
        if (
            self.target_policy == "fixed"
            and self.operator_family != "r2"
            and not grade_bounded
        ):
            required = operator_session_target_count(self.operator_family)
            if len(tuple(self.selected_student_ids)) != required:
                raise ValueError(
                    f"{self.operator_family} fixed targeting requires {required} students"
                )

    @property
    def neighborhood_radius(self):
        return {
            "r2": 2,
            "targeted_r4_s1": 4,
            "targeted_r8_s1": 8,
            "targeted_r4_s2": 4,
            "targeted_r8_s2": 8,
            "targeted_utilization_r16_s2": 16,
            "targeted_utilization_r16_s4": 16,
            "targeted_utilization_r32_s4": 32,
            "targeted_utilization_r32_s6": 32,
            "targeted_utilization_r64_s6": 64,
            "targeted_utilization_r64_s8": 64,
            "targeted_utilization_r64_s10": 64,
            "grade_bounded_g9": None,
            "grade_bounded_g10": None,
            "grade_bounded_g11": None,
            "grade_bounded_g12": None,
        }[self.operator_family]

    @property
    def max_changed_students(self):
        return {
            "r2": None,
            "targeted_r4_s1": 1,
            "targeted_r8_s1": 1,
            "targeted_r4_s2": 2,
            "targeted_r8_s2": 2,
            "targeted_utilization_r16_s2": 2,
            "targeted_utilization_r16_s4": 4,
            "targeted_utilization_r32_s4": 4,
            "targeted_utilization_r32_s6": 6,
            "targeted_utilization_r64_s6": 6,
            "targeted_utilization_r64_s8": 8,
            "targeted_utilization_r64_s10": 10,
            "grade_bounded_g9": None,
            "grade_bounded_g10": None,
            "grade_bounded_g11": None,
            "grade_bounded_g12": None,
        }[self.operator_family]

    @property
    def targeted(self):
        return self.operator_family != "r2"


@dataclass(frozen=True)
class ContinuousOperatorSessionRecord:
    """JSON-safe facts from one completed continuous session."""

    session_id: str
    input_fingerprint: str
    source_seed_fingerprint: str | None
    objective_semantics_version: str
    operator_family: str
    target_policy: str
    configured_total_seconds: float
    max_attempts: int
    per_attempt_limit_seconds: float
    attempt_count: int
    adopted_count: int
    initial_substantive_value: float | None
    final_substantive_value: float | None
    cumulative_gain: float
    total_elapsed_seconds: float
    total_cp_sat_seconds: float
    total_validation_seconds: float
    external_overrun_seconds: float
    stopping_reason: str
    cp_sat_random_seed: int | None = None
    cp_sat_max_deterministic_time_seconds: float | None = None
    selected_grade: int | None = None
    attempts: tuple[dict, ...] = ()
    target_history: tuple[tuple, ...] = ()
    session_context_reused: bool = True
    resource: dict = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)

    def to_json(self):
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


def build_continuous_operator_session_record(
    data,
    result,
    *,
    session_id=None,
    seed_source_fingerprint=None,
):
    """Translate engine session facts into a stable diagnostic record."""

    facts = dict(result.optimization_facts or {})
    local = dict(facts.get("stage_2_local_bootstrap") or {})
    attempts = tuple(local.get("iterations", ()))
    initial = local.get("baseline_substantive_value")
    final = local.get("candidate_substantive_value")
    cp_sat = sum(float(item.get("solver_wall_time_seconds", 0) or 0) for item in attempts)
    validation = sum(float(item.get("validation_elapsed_seconds", 0) or 0) for item in attempts)
    elapsed = float(
        local.get("session_elapsed_seconds")
        or local.get("cumulative_session_elapsed_seconds")
        or local.get("deadline_elapsed_seconds")
        or 0.0
    )
    configured = float(
        local.get("configured_session_budget_seconds")
        or local.get("deadline_requested_time_limit_seconds")
        or 0.0
    )
    return ContinuousOperatorSessionRecord(
        session_id=str(session_id or uuid4()),
        input_fingerprint=semantic_student_assignment_input_fingerprint(data),
        source_seed_fingerprint=(
            seed_source_fingerprint
            or local.get("source_seed_fingerprint")
            or None
        ),
        objective_semantics_version=data.objective_semantics_version,
        operator_family=local.get("operator_family") or "r2",
        target_policy=local.get("target_policy") or "dynamic",
        configured_total_seconds=configured,
        max_attempts=int(local.get("max_iterations", len(attempts)) or len(attempts)),
        per_attempt_limit_seconds=float(local.get("time_limit_seconds", 0) or 0),
        attempt_count=len(attempts),
        adopted_count=sum(bool(item.get("adopted")) for item in attempts),
        initial_substantive_value=initial,
        final_substantive_value=final,
        cumulative_gain=(
            max(0.0, float(initial) - float(final))
            if initial is not None and final is not None
            else 0.0
        ),
        total_elapsed_seconds=elapsed,
        total_cp_sat_seconds=cp_sat,
        total_validation_seconds=validation,
        external_overrun_seconds=float(local.get("external_overrun_seconds", 0) or 0),
        stopping_reason=local.get("stopping_reason") or "diagnostic_stop",
        cp_sat_random_seed=(
            int(local["cp_sat_random_seed"])
            if local.get("cp_sat_random_seed") is not None
            else None
        ),
        cp_sat_max_deterministic_time_seconds=(
            float(local["cp_sat_max_deterministic_time_seconds"])
            if local.get("cp_sat_max_deterministic_time_seconds") is not None
            else None
        ),
        selected_grade=local.get("selected_grade"),
        attempts=attempts,
        target_history=tuple(local.get("session_target_history") or ()),
        session_context_reused=bool(local.get("session_context_reused", False)),
        resource=dict(local.get("memory") or {}),
    )


__all__ = [
    "ContinuousOperatorSessionConfig",
    "ContinuousOperatorSessionRecord",
    "build_continuous_operator_session_record",
    "OPERATOR_FAMILIES",
    "TARGET_POLICIES",
    "operator_session_target_count",
    "select_operator_session_targets",
]
