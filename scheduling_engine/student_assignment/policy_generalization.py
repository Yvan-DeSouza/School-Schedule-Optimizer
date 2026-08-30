"""Deterministic scenario definitions for offline policy generalization work.

This module only composes the existing DTO fixture builders and records their
semantic identity.  It does not construct a solver model, add constraints, or
authorize a schedule.  Each scenario is intended to be prepared and validated
through the existing Stage 1 and adaptive-policy diagnostic boundaries before
any policy comparison is run.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from ..realistic_student_assignment_validation import (
    build_mixed_grade_v2_fixture,
    summarize_mixed_grade_v2_fixture,
    summarize_production_shaped_medium_fixture,
)
from .runtime import semantic_student_assignment_input_fingerprint


POLICY_GENERALIZATION_SUITE_SCHEMA = (
    "student_assignment_policy_generalization_suite_v1"
)
POLICY_GENERALIZATION_FIXTURE_VERSION = "mixed_grade_v2_fixture_v1"
POLICY_GENERALIZATION_PROFILE = "balanced"
POLICY_GENERALIZATION_POLICIES = ("adaptive", "stateless_role", "fixed_cycle")
POLICY_GENERALIZATION_TOTAL_SECONDS = 120.0
POLICY_GENERALIZATION_PER_OPERATOR_SECONDS = 30.0
POLICY_GENERALIZATION_WORKER_COUNT = 1
POLICY_GENERALIZATION_RANDOM_SEED = 101


@dataclass(frozen=True)
class PolicyGeneralizationScenario:
    """One pre-registered semantic condition for policy comparison."""

    scenario_id: str
    description: str
    student_count: int
    special_profile_cycle: int = 100
    scenario_version: str = "v1"
    generation_seed: int | None = None

    def to_dict(self):
        return asdict(self)


# The three scenarios deliberately vary independent, meaningful pressures:
# the reference condition, more ordinary demand/capacity contention, and a
# denser recurrence of the already-defined special cohorts.  They share the
# same course topology, hard rules, and objective profile.
DEFAULT_POLICY_GENERALIZATION_SCENARIOS = (
    PolicyGeneralizationScenario(
        scenario_id="reference_medium",
        description="Reference mixed-grade medium condition.",
        student_count=240,
    ),
    PolicyGeneralizationScenario(
        scenario_id="population_pressure_medium",
        description="Higher student and ordinary-demand pressure.",
        student_count=320,
    ),
    PolicyGeneralizationScenario(
        scenario_id="special_commitment_pressure_medium",
        description="Denser recurrence of the existing special cohorts.",
        student_count=240,
        special_profile_cycle=50,
    ),
)

# Promote only semantically matched variants after the medium gate.  The
# reference and ordinary-population pressures are kept distinct at near-target
# scale because this fixture builder's only ordinary-pressure control is the
# population size.  The denser special cohort is also run at the exact target
# population, where its defining pressure remains meaningful.
TARGET_POLICY_GENERALIZATION_SCENARIOS = (
    PolicyGeneralizationScenario(
        scenario_id="reference_target",
        description="Exact-target reference mixed-grade condition.",
        student_count=1400,
        scenario_version="target-v1",
    ),
    PolicyGeneralizationScenario(
        scenario_id="reference_near_target",
        description="Near-target reference mixed-grade condition.",
        student_count=800,
        scenario_version="target-v1",
    ),
    PolicyGeneralizationScenario(
        scenario_id="population_pressure_near_target",
        description="Near-target higher student and ordinary-demand pressure.",
        student_count=1050,
        scenario_version="target-v1",
    ),
    PolicyGeneralizationScenario(
        scenario_id="special_commitment_pressure_target",
        description="Exact-target denser recurrence of the existing special cohorts.",
        student_count=1400,
        special_profile_cycle=50,
        scenario_version="target-v1",
    ),
)


def build_policy_generalization_scenario(scenario):
    """Build one scenario through the existing DTO-only fixture path."""

    if not isinstance(scenario, PolicyGeneralizationScenario):
        raise TypeError("scenario must be a PolicyGeneralizationScenario")
    return build_mixed_grade_v2_fixture(
        student_count=scenario.student_count,
        special_profile_cycle=scenario.special_profile_cycle,
    )


def summarize_policy_generalization_scenario(scenario, data=None):
    """Return provenance and structural facts for one scenario."""

    if data is None:
        data = build_policy_generalization_scenario(scenario)
    return {
        "schema": POLICY_GENERALIZATION_SUITE_SCHEMA,
        "fixture_version": POLICY_GENERALIZATION_FIXTURE_VERSION,
        "scenario": scenario.to_dict(),
        "input_fingerprint": semantic_student_assignment_input_fingerprint(data),
        "mixed_grade_summary": summarize_mixed_grade_v2_fixture(data),
        "production_shaped_summary": summarize_production_shaped_medium_fixture(
            data
        ),
    }


def build_policy_generalization_suite(scenarios=None):
    """Build all selected scenarios with their validated structural identity."""

    selected = tuple(
        DEFAULT_POLICY_GENERALIZATION_SCENARIOS
        if scenarios is None
        else scenarios
    )
    return tuple(
        (
            scenario,
            build_policy_generalization_scenario(scenario),
        )
        for scenario in selected
    )


__all__ = [
    "DEFAULT_POLICY_GENERALIZATION_SCENARIOS",
    "TARGET_POLICY_GENERALIZATION_SCENARIOS",
    "POLICY_GENERALIZATION_FIXTURE_VERSION",
    "POLICY_GENERALIZATION_PER_OPERATOR_SECONDS",
    "POLICY_GENERALIZATION_POLICIES",
    "POLICY_GENERALIZATION_PROFILE",
    "POLICY_GENERALIZATION_RANDOM_SEED",
    "POLICY_GENERALIZATION_SUITE_SCHEMA",
    "POLICY_GENERALIZATION_TOTAL_SECONDS",
    "POLICY_GENERALIZATION_WORKER_COUNT",
    "PolicyGeneralizationScenario",
    "build_policy_generalization_scenario",
    "build_policy_generalization_suite",
    "summarize_policy_generalization_scenario",
]
