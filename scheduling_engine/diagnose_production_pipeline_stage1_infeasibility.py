"""Diagnostic-only hard-feasibility reconstruction for the frozen pipeline run.

This tool never invokes Django, a production adapter, placement, staffing, or
the production student-assignment solver.  It reconstructs the exact candidate
relations recorded by the immutable isolated-feasibility artifact and checks
bounded hard-model layers.  It is deliberately not part of the product path.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from time import monotonic

from ortools.sat.python import cp_model

from .benchmark_individual_feasibility import (
    IndividualCandidate,
    IndividualDecisionGroup,
    IndividualFeasibilityResult,
)
from .paul_desmarais_production_pipeline_source import build_source_spec


ARTIFACT = (
    Path("scheduling_engine/benchmarks/production_pipeline")
    / "paul_desmarais_shaped_g9_12_production_pipeline_v1"
    / "qualification_failure.json"
)
FULL_CAPACITY = 40
SEQUENCE_CAPACITY = 30
ONLINE_CAPACITY = 16


def _status_name(status: int) -> str:
    return {
        cp_model.OPTIMAL: "optimal",
        cp_model.FEASIBLE: "feasible",
        cp_model.INFEASIBLE: "infeasible",
        cp_model.UNKNOWN: "unknown",
        cp_model.MODEL_INVALID: "model_invalid",
    }.get(status, "unknown")


def _load_groups(path: Path):
    payload = json.loads(path.read_text(encoding="utf-8"))
    namespace = {
        "__builtins__": {},
        "IndividualCandidate": IndividualCandidate,
        "IndividualDecisionGroup": IndividualDecisionGroup,
        "IndividualFeasibilityResult": IndividualFeasibilityResult,
    }
    results = [eval(row, namespace, {}) for row in payload["isolated_feasibility"]["results"]]
    if any(row.status != "feasible" for row in results):
        raise ValueError("Frozen artifact is not an all-feasible isolated preflight.")
    return payload, results


def _source_request_courses():
    spec = build_source_spec()
    courses = {course.key: course for course in spec.courses}
    # The qualification harness creates this source list in its stable order in
    # a newly-created database.  The frozen preflight uses those IDs (1..N).
    return {
        request_id: int(courses[request.course_key].source_course_id)
        for request_id, request in enumerate(spec.requests, start=1)
    }


def _group_kind(group: IndividualDecisionGroup) -> str:
    if group.source_kind in {"study", "focus", "connected_two_credit_co_op"}:
        return group.source_kind
    kinds = {candidate.candidate_kind for candidate in group.candidates}
    if kinds == {"online_supervision_section"}:
        return "online"
    if group.source_kind == "paired_half_course":
        return "half_pair"
    if kinds == {"instructional_section"}:
        return "ordinary"
    raise ValueError(f"Unexpected candidate group {group.source_kind!r}/{kinds!r}")


def _section_capacities(results, request_courses):
    capacities = {}
    for result in results:
        for group in result.decision_groups:
            kind = _group_kind(group)
            if kind in {"study", "focus", "connected_two_credit_co_op"}:
                continue
            course_ids = {
                request_courses[request_id]
                for request_id in group.source_request_ids
                if request_id in request_courses
            }
            for candidate in group.candidates:
                for identity in candidate.identity:
                    if identity < 0:
                        capacity = ONLINE_CAPACITY
                    elif course_ids & {17, 18}:
                        capacity = SEQUENCE_CAPACITY
                    else:
                        capacity = FULL_CAPACITY
                    prior = capacities.setdefault(identity, capacity)
                    if prior != capacity:
                        raise ValueError(f"Conflicting inferred capacity for section {identity}")
    return capacities


def _grade_ten_half_pair_witness(results):
    """Return the compact capacity/occupancy cut behind the frozen failure."""

    rows = []
    full_position_counts = defaultdict(int)
    for result in results:
        if not 352 <= result.student_id <= 696:
            continue
        groups = result.decision_groups
        half_group = next(group for group in groups if _group_kind(group) == "half_pair")
        full_position_counts[sum(
            _group_kind(group) in {"ordinary", "online"} for group in groups
        )] += 1
        if not rows:
            for candidate in half_group.candidates:
                timeslots = tuple(sorted({timeslot_id for timeslot_id, _segment in candidate.occupancy}))
                rows.append({
                    "pair_section_ids": candidate.identity,
                    "semester": candidate.semester,
                    "timeslot_ids": timeslots,
                    "co_timed": len(timeslots) == 1,
                })
    co_timed_count = sum(row["co_timed"] for row in rows)
    return {
        "grade_ten_half_pair_students": sum(full_position_counts.values()),
        "full_position_group_count_distribution": dict(sorted(full_position_counts.items())),
        "available_full_year_positions": 8,
        "split_pair_consumed_positions": 2,
        "co_timed_pair_count": co_timed_count,
        "split_pair_count": len(rows) - co_timed_count,
        "co_timed_pair_capacity": co_timed_count * FULL_CAPACITY,
        "co_timed_seat_deficit": sum(full_position_counts.values()) - co_timed_count * FULL_CAPACITY,
        "pair_candidates": rows,
    }


def solve_layer(results, *, included_kinds, request_courses, time_limit_seconds, grades=None, unsat_core=False):
    """Solve exact frozen candidate/occupancy/capacity constraints by family."""

    groups = [
        (result.student_id, group, _group_kind(group))
        for result in results
        if grades is None or (9 + ((result.student_id - 1) // 350)) in grades
        for group in result.decision_groups
        if _group_kind(group) in included_kinds
    ]
    capacities = _section_capacities(results, request_courses)
    model = cp_model.CpModel()
    by_resource = defaultdict(list)
    by_student_occupancy = defaultdict(list)
    variable_count = 0
    assumption_sources = {}
    for group_index, (student_id, group, kind) in enumerate(groups):
        choices = []
        for candidate_index, candidate in enumerate(group.candidates):
            variable = model.NewBoolVar(f"g_{group_index}_{candidate_index}")
            choices.append(variable)
            variable_count += 1
            for resource_id in candidate.identity:
                if resource_id in capacities:
                    by_resource[resource_id].append(variable)
            for occupied in candidate.occupancy:
                by_student_occupancy[student_id, occupied].append(variable)
        exact = model.AddExactlyOne(choices)
        if unsat_core and kind == "half_pair":
            assumption = model.NewBoolVar(f"require_half_{student_id}_{group_index}")
            exact.OnlyEnforceIf(assumption)
            model.AddAssumption(assumption)
            assumption_sources[assumption.Index()] = student_id
    for resource_id, variables in by_resource.items():
        model.Add(sum(variables) <= capacities[resource_id])
    for variables in by_student_occupancy.values():
        model.Add(sum(variables) <= 1)
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(time_limit_seconds)
    solver.parameters.num_search_workers = 8
    solver.parameters.random_seed = 0
    started = monotonic()
    status = solver.Solve(model)
    result = {
        "kinds": sorted(included_kinds),
        "status": _status_name(status),
        "group_count": len(groups),
        "variable_count": variable_count,
        "constraint_count": len(model.Proto().constraints),
        "seconds": round(monotonic() - started, 6),
    }
    if status == cp_model.INFEASIBLE and unsat_core:
        core_student_ids = sorted({
            assumption_sources[abs(literal)]
            for literal in solver.SufficientAssumptionsForInfeasibility()
            if abs(literal) in assumption_sources
        })
        result["sufficient_half_pair_core_count"] = len(core_student_ids)
        result["sufficient_half_pair_core_student_ids"] = core_student_ids
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", type=Path, default=ARTIFACT)
    parser.add_argument("--time-limit-seconds", type=float, default=30.0)
    parser.add_argument(
        "--kinds",
        help="Comma-separated one diagnostic layer; omitting it runs the fixed ladder.",
    )
    parser.add_argument("--grades", help="Optional comma-separated Grades 9-12.")
    parser.add_argument("--unsat-core", action="store_true")
    args = parser.parse_args()
    payload, results = _load_groups(args.artifact)
    request_courses = _source_request_courses()
    families = ("ordinary", "half_pair", "online", "connected_two_credit_co_op", "study", "focus")
    summary = {
        "lineage_id": payload["lineage_id"],
        "final_input_fingerprint": payload["final_staffing_input"]["fingerprint"],
        "isolated_student_count": len(results),
        "group_count_by_kind": {
            kind: sum(
                _group_kind(group) == kind
                for result in results for group in result.decision_groups
            )
            for kind in families
        },
        "grade_ten_half_pair_witness": _grade_ten_half_pair_witness(results),
        "layers": [],
    }
    layers = [
        {"ordinary"},
        {"ordinary", "half_pair"},
        {"ordinary", "half_pair", "online"},
        {"ordinary", "half_pair", "online", "connected_two_credit_co_op"},
        {"ordinary", "half_pair", "online", "connected_two_credit_co_op", "study"},
        set(families),
    ]
    if args.kinds:
        layers = [{item for item in args.kinds.split(",") if item}]
    grades = ({int(item) for item in args.grades.split(",") if item} if args.grades else None)
    for layer in layers:
        summary["layers"].append(solve_layer(
            results,
            included_kinds=layer,
            request_courses=request_courses,
            time_limit_seconds=args.time_limit_seconds,
            grades=grades,
            unsat_core=args.unsat_core,
        ))
    print(json.dumps(summary, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
