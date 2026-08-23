"""Clean-process-friendly Stage 2 quality/runtime experiment helpers.

This module is deliberately diagnostic-only.  It prepares a detached DTO,
invokes the existing student-assignment engine, and returns normalized facts
for comparing solver horizons and incumbent strategies.  It does not change
the production entry point, objective definitions, or persisted workflow.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from time import perf_counter

from ..realistic_student_assignment_validation import (
    build_production_shaped_medium_fixture,
    summarize_production_shaped_medium_fixture,
)
from ..student_assignment.quality import evaluate_student_assignment_quality
from ..student_assignment.runtime import semantic_student_assignment_input_fingerprint
from .core import (
    run_student_assignment_adaptive_local_bootstrap_diagnostic,
    run_student_assignment_local_bootstrap_diagnostic,
    run_student_assignment_stage2_diagnostic,
    run_substantive_soft_tier_probe,
)


@dataclass(frozen=True)
class Stage2ExperimentConfig:
    """One bounded, reproducible diagnostic trial configuration."""

    stage1_time_limit_seconds: float = 30.0
    stage1_validation_time_limit_seconds: float = 15.0
    stage1_worker_count: int = 8
    stage1_validation_worker_count: int = 8
    stage2_time_limit_seconds: float = 60.0
    stage2_worker_count: int = 8
    strategy: str = "ordinary"
    neighborhood_radius: int = 2
    adaptive_radii: tuple[int, ...] = (2, 4)
    max_iterations: int = 2
    per_probe_time_limit_seconds: float = 15.0
    timeline_max_events: int = 128


def run_stage2_experiment(
    data,
    config: Stage2ExperimentConfig,
    *,
    alternate_source_decisions=(),
    alternate_source_variable_values=None,
):
    """Run one diagnostic trial and return JSON-compatible facts.

    ``alternate_source_decisions`` and ``alternate_source_variable_values``
    are optional diagnostic-only inputs.  They let paired ordinary/retention
    trials start from the same already-validated Stage 1 source assignment,
    so differences are attributable to the Stage 2 policy rather than to a
    new parallel Stage 1 search result.
    """

    return _run_stage2_experiment(
        data,
        config,
        alternate_source_decisions=alternate_source_decisions,
        alternate_source_variable_values=alternate_source_variable_values,
    )


def _run_stage2_experiment(
    data,
    config: Stage2ExperimentConfig,
    *,
    alternate_source_decisions,
    alternate_source_variable_values,
):
    """Implementation shared by ordinary and paired diagnostic trials."""

    data = replace(data, time_limit_seconds=config.stage1_time_limit_seconds)
    input_fingerprint = semantic_student_assignment_input_fingerprint(data)
    started = perf_counter()

    common = {
        "hard_feasibility_time_limit_seconds": config.stage1_time_limit_seconds,
        "hard_feasibility_validation_time_limit_seconds": (
            config.stage1_validation_time_limit_seconds
        ),
        "hard_feasibility_worker_count": config.stage1_worker_count,
        "hard_feasibility_validation_worker_count": (
            config.stage1_validation_worker_count
        ),
        "optimization_worker_count": config.stage2_worker_count,
        "timeline_max_events": config.timeline_max_events,
        "alternate_source_decisions": alternate_source_decisions,
        "alternate_source_variable_values": alternate_source_variable_values,
    }
    if config.strategy == "ordinary":
        result = run_student_assignment_stage2_diagnostic(
            data,
            total_time_limit_seconds=config.stage2_time_limit_seconds,
            retain_incumbent_on_non_improvement=False,
            **common,
        )
    elif config.strategy == "retention":
        result = run_student_assignment_stage2_diagnostic(
            data,
            total_time_limit_seconds=config.stage2_time_limit_seconds,
            retain_incumbent_on_non_improvement=True,
            **common,
        )
    elif config.strategy == "local":
        result = run_student_assignment_local_bootstrap_diagnostic(
            data,
            neighborhood_radius=config.neighborhood_radius,
            time_limit_seconds=config.per_probe_time_limit_seconds,
            total_time_limit_seconds=config.stage2_time_limit_seconds,
            worker_count=config.stage2_worker_count,
            hard_feasibility_time_limit_seconds=config.stage1_time_limit_seconds,
            hard_feasibility_validation_time_limit_seconds=(
                config.stage1_validation_time_limit_seconds
            ),
            hard_feasibility_worker_count=config.stage1_worker_count,
            hard_feasibility_validation_worker_count=(
                config.stage1_validation_worker_count
            ),
            timeline_max_events=config.timeline_max_events,
        )
    elif config.strategy == "adaptive":
        result = run_student_assignment_adaptive_local_bootstrap_diagnostic(
            data,
            neighborhood_radii=config.adaptive_radii,
            max_iterations=config.max_iterations,
            per_probe_time_limit_seconds=config.per_probe_time_limit_seconds,
            total_time_limit_seconds=config.stage2_time_limit_seconds,
            worker_count=config.stage2_worker_count,
            hard_feasibility_time_limit_seconds=config.stage1_time_limit_seconds,
            hard_feasibility_validation_time_limit_seconds=(
                config.stage1_validation_time_limit_seconds
            ),
            hard_feasibility_worker_count=config.stage1_worker_count,
            hard_feasibility_validation_worker_count=(
                config.stage1_validation_worker_count
            ),
            timeline_max_events=config.timeline_max_events,
        )
    else:
        raise ValueError(f"Unknown Stage 2 diagnostic strategy: {config.strategy}")

    final_quality = evaluate_student_assignment_quality(
        data,
        assignments=result.assignments,
        commitment_assignments=result.commitment_assignments,
    )
    facts = summarize_production_shaped_medium_fixture(data, result)
    facts.update({
        "strategy": config.strategy,
        "input_semantic_fingerprint": input_fingerprint,
        "elapsed_seconds": perf_counter() - started,
        "objective_vector": list(
            result.optimization_facts.get("stage_2", {}).get(
                "objective_values", ()
            )
        ),
        "optimization_facts": result.optimization_facts,
        "quality": final_quality,
        "unmet_diagnostic_codes": sorted({
            item.diagnostic_code for item in result.unmet_requests
        }),
    })
    return facts


def prepare_validated_stage1_seed(data, config: Stage2ExperimentConfig):
    """Prepare one validated Stage 1 source seed for paired diagnostics.

    The helper uses the existing CP-SAT substantive probe boundary only to
    obtain the validated seed facts.  It never creates a heuristic schedule
    and is not used by the ordinary production entry point.
    """

    data = replace(data, time_limit_seconds=config.stage1_time_limit_seconds)
    result = run_substantive_soft_tier_probe(
        data,
        threshold=None,
        time_limit_seconds=config.stage1_time_limit_seconds,
        worker_count=config.stage1_worker_count,
        hard_feasibility_time_limit_seconds=config.stage1_time_limit_seconds,
        hard_feasibility_validation_time_limit_seconds=(
            config.stage1_validation_time_limit_seconds
        ),
        hard_feasibility_worker_count=config.stage1_worker_count,
        hard_feasibility_validation_worker_count=(
            config.stage1_validation_worker_count
        ),
    )
    if not result.seed_validated:
        raise RuntimeError(
            "The diagnostic seed preparation did not produce a validated CP-SAT seed."
        )
    return {
        "input_semantic_fingerprint": semantic_student_assignment_input_fingerprint(data),
        "seed_objective_vector": result.seed_objective_vector,
        "seed_source_decisions": result.seed_source_decisions,
        "seed_source_variable_values": result.seed_source_variable_values,
        "seed_summary": result.seed_summary,
    }


def run_production_shaped_medium_experiment(
    *,
    student_count=120,
    config: Stage2ExperimentConfig | None = None,
):
    """Build the deterministic medium fixture and run one trial."""

    return run_stage2_experiment(
        build_production_shaped_medium_fixture(student_count=student_count),
        config or Stage2ExperimentConfig(),
    )


def run_strict_substantive_probe(data, config: Stage2ExperimentConfig):
    """Run a strict ``seed substantive value - 1`` diagnostic query."""

    data = replace(data, time_limit_seconds=config.stage1_time_limit_seconds)
    started = perf_counter()
    result = run_substantive_soft_tier_probe(
        data,
        threshold=None,
        strict_improvement=True,
        time_limit_seconds=config.stage2_time_limit_seconds,
        worker_count=config.stage2_worker_count,
        hard_feasibility_time_limit_seconds=config.stage1_time_limit_seconds,
        hard_feasibility_validation_time_limit_seconds=(
            config.stage1_validation_time_limit_seconds
        ),
        hard_feasibility_worker_count=config.stage1_worker_count,
        hard_feasibility_validation_worker_count=(
            config.stage1_validation_worker_count
        ),
    )
    return {
        "strategy": "strict_substantive_probe",
        "input_semantic_fingerprint": semantic_student_assignment_input_fingerprint(data),
        "elapsed_seconds": perf_counter() - started,
        "status": result.status,
        "seed_validated": result.seed_validated,
        "baseline_substantive_value": result.baseline_substantive_value,
        "requested_threshold": result.requested_threshold,
        "candidate_substantive_value": result.candidate_substantive_value,
        "complete_candidate_found": result.complete_candidate_found,
        "seed_assignment_count": result.seed_assignment_count,
        "candidate_assignment_count": result.candidate_assignment_count,
        "changed_source_decision_count": result.changed_source_decision_count,
        "seed_component_values": result.seed_component_values,
        "candidate_component_values": result.candidate_component_values,
        "component_deltas": result.component_deltas,
        "model_variable_count": result.model_variable_count,
        "model_constraint_count": result.model_constraint_count,
        "model_family_variable_counts": result.model_family_variable_counts,
        "conflicts": result.conflicts,
        "branches": result.branches,
        "solver_wall_time_seconds": result.solver_wall_time_seconds,
        "timings": result.timings,
    }


def run_component_minimum_probe(data, config: Stage2ExperimentConfig, component_name):
    """Minimize one existing substantive component for diagnosis only."""

    data = replace(data, time_limit_seconds=config.stage1_time_limit_seconds)
    started = perf_counter()
    result = run_substantive_soft_tier_probe(
        data,
        threshold=None,
        time_limit_seconds=config.stage2_time_limit_seconds,
        worker_count=config.stage2_worker_count,
        minimize_component=component_name,
        hard_feasibility_time_limit_seconds=config.stage1_time_limit_seconds,
        hard_feasibility_validation_time_limit_seconds=(
            config.stage1_validation_time_limit_seconds
        ),
        hard_feasibility_worker_count=config.stage1_worker_count,
        hard_feasibility_validation_worker_count=(
            config.stage1_validation_worker_count
        ),
    )
    return {
        "strategy": "component_minimum_probe",
        "component": component_name,
        "input_semantic_fingerprint": semantic_student_assignment_input_fingerprint(data),
        "elapsed_seconds": perf_counter() - started,
        "status": result.status,
        "seed_validated": result.seed_validated,
        "seed_value": result.seed_component_values.get(component_name),
        "best_value": result.minimized_component_value,
        "best_bound": result.best_bound,
        "candidate_found": result.complete_candidate_found,
        "candidate_assignment_count": result.candidate_assignment_count,
        "changed_source_decision_count": result.changed_source_decision_count,
        "component_deltas": result.component_deltas,
        "model_variable_count": result.model_variable_count,
        "model_constraint_count": result.model_constraint_count,
        "conflicts": result.conflicts,
        "branches": result.branches,
        "solver_wall_time_seconds": result.solver_wall_time_seconds,
        "timings": result.timings,
    }


if __name__ == "__main__":  # pragma: no cover - manual experiment surface
    import argparse
    import json

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--students", type=int, default=120)
    parser.add_argument("--strategy", choices=("ordinary", "retention", "local", "adaptive"), default="ordinary")
    parser.add_argument("--stage1-seconds", type=float, default=30.0)
    parser.add_argument("--stage1-validation-seconds", type=float, default=15.0)
    parser.add_argument("--stage2-seconds", type=float, default=60.0)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--radius", type=int, default=2)
    args = parser.parse_args()
    config = Stage2ExperimentConfig(
        stage1_time_limit_seconds=args.stage1_seconds,
        stage1_validation_time_limit_seconds=args.stage1_validation_seconds,
        stage2_time_limit_seconds=args.stage2_seconds,
        stage1_worker_count=args.workers,
        stage1_validation_worker_count=args.workers,
        stage2_worker_count=args.workers,
        strategy=args.strategy,
        neighborhood_radius=args.radius,
    )
    print(json.dumps(
        run_production_shaped_medium_experiment(
            student_count=args.students,
            config=config,
        ),
        indent=2,
        default=str,
    ))
