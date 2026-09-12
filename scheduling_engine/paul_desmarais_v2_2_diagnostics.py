"""Reproduction and reduced diagnosis for the preserved Paul-Desmarais v2.2 case.

This module is research/fixture tooling only.  It does not modify the
production student-assignment model and its normal command does not invoke the
full 1,400-student Stage 1 solver.
"""

from __future__ import annotations

import argparse
from collections import defaultdict, deque
from dataclasses import replace
import json
from time import monotonic

from ortools.sat.python import cp_model

from .benchmark_individual_feasibility import preflight_individual_feasibility
from .dto import (
    StudentAssignmentInputDTO,
    StudentAssignmentRequestDTO,
    StudentAssignmentSectionDTO,
    TimeSlotDTO,
)
from .paul_desmarais_stress_benchmark import (
    V2_2_AUTHORITATIVE_FINGERPRINT,
    V2_3_AUTHORITATIVE_FINGERPRINT,
    V2_3_BENCHMARK_ID,
    V2_3_BENCHMARK_VERSION,
    build_paul_desmarais_shaped_g9_12_stress_v2_3_fixture,
    reconstruct_paul_desmarais_v2_2,
    summarize_paul_desmarais_shaped_g9_12_stress_fixture,
)
from .student_assignment.validation import validate_input


def _ordinary_requests(data):
    return tuple(
        request
        for request in data.requests
        if request.is_in_scope
        and (request.is_mandatory or request.is_primary)
        and request.delivery_kind == "normal_instruction"
        and request.duration == "full_semester"
    )


def _positive_sections_by_offering(data):
    validated = validate_input(data)
    return {
        offering_id: tuple(section for section in sections if section.section_id > 0)
        for offering_id, sections in validated.items()
    }


class _Dinic:
    def __init__(self, node_count):
        self.graph = [[] for _ in range(node_count)]

    def add_edge(self, source, target, capacity):
        forward = [target, int(capacity), len(self.graph[target])]
        reverse = [source, 0, len(self.graph[source])]
        self.graph[source].append(forward)
        self.graph[target].append(reverse)

    def max_flow(self, source, sink):
        total = 0
        while True:
            level = [-1] * len(self.graph)
            level[source] = 0
            queue = deque([source])
            while queue:
                node = queue.popleft()
                for target, capacity, _reverse in self.graph[node]:
                    if capacity and level[target] < 0:
                        level[target] = level[node] + 1
                        queue.append(target)
            if level[sink] < 0:
                return total
            cursor = [0] * len(self.graph)

            def send(node, available):
                if node == sink:
                    return available
                while cursor[node] < len(self.graph[node]):
                    edge = self.graph[node][cursor[node]]
                    target, capacity, reverse_index = edge
                    if capacity and level[target] == level[node] + 1:
                        pushed = send(target, min(available, capacity))
                        if pushed:
                            edge[1] -= pushed
                            self.graph[target][reverse_index][1] += pushed
                            return pushed
                    cursor[node] += 1
                return 0

            while True:
                pushed = send(source, 10**9)
                if not pushed:
                    break
                total += pushed


def capacity_only_matching(data):
    """Solve ordinary request-to-section b-matching without student collisions."""

    requests = _ordinary_requests(data)
    sections_by_offering = _positive_sections_by_offering(data)
    section_by_id = {}
    request_sections = []
    for request in requests:
        candidates = sections_by_offering.get(request.course_offering_id, ())
        request_sections.append(tuple(section.section_id for section in candidates))
        for section in candidates:
            section_by_id[section.section_id] = section

    source = 0
    request_start = 1
    section_start = request_start + len(requests)
    section_ids = tuple(sorted(section_by_id))
    section_index = {section_id: section_start + index for index, section_id in enumerate(section_ids)}
    sink = section_start + len(section_ids)
    flow = _Dinic(sink + 1)
    for index, candidate_ids in enumerate(request_sections):
        flow.add_edge(source, request_start + index, 1)
        for section_id in candidate_ids:
            flow.add_edge(request_start + index, section_index[section_id], 1)
    for section_id in section_ids:
        flow.add_edge(
            section_index[section_id],
            sink,
            section_by_id[section_id].capacity_max,
        )
    maximum = flow.max_flow(source, sink)
    return {
        "required_group_count": len(requests),
        "maximum_assignable_group_count": maximum,
        "deficit": len(requests) - maximum,
        "feasible": maximum == len(requests),
    }


def _status_name(status):
    return {
        cp_model.OPTIMAL: "optimal",
        cp_model.FEASIBLE: "feasible",
        cp_model.INFEASIBLE: "infeasible",
        cp_model.UNKNOWN: "unknown",
        cp_model.MODEL_INVALID: "model_invalid",
    }.get(status, "unknown")


def collision_diagnostic(data, *, time_limit_seconds=60.0):
    """Solve the ordinary capacity-plus-student-collision diagnostic clone."""

    requests = _ordinary_requests(data)
    sections_by_offering = _positive_sections_by_offering(data)
    model = cp_model.CpModel()
    by_course_cell = defaultdict(list)
    by_student_cell = defaultdict(list)
    variable_count = 0
    section_capacity = defaultdict(int)
    seen_sections = set()
    for sections in sections_by_offering.values():
        for section in sections:
            if section.section_id in seen_sections:
                continue
            seen_sections.add(section.section_id)
            for course_id in section.member_course_ids:
                section_capacity[course_id, section.semester, section.timeslot_id] += section.capacity_max

    for request in requests:
        cells = sorted({
            (section.semester, section.timeslot_id)
            for section in sections_by_offering.get(request.course_offering_id, ())
        })
        variables = [
            model.NewBoolVar(f"ordinary_{request.request_id}_{semester}_{timeslot_id}")
            for semester, timeslot_id in cells
        ]
        variable_count += len(variables)
        model.AddExactlyOne(variables)
        for variable, (semester, timeslot_id) in zip(variables, cells):
            by_course_cell[request.course_id, semester, timeslot_id].append(variable)
            by_student_cell[request.student_id, timeslot_id].append(variable)

    for key, variables in by_course_cell.items():
        model.Add(sum(variables) <= section_capacity[key])
    for variables in by_student_cell.values():
        model.Add(sum(variables) <= 1)

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(time_limit_seconds)
    solver.parameters.num_search_workers = 8
    solver.parameters.random_seed = 0
    started = monotonic()
    status = solver.Solve(model)
    return {
        "status": _status_name(status),
        "required_group_count": len(requests),
        "variable_count": variable_count,
        "constraint_count": len(model.Proto().constraints),
        "external_seconds": monotonic() - started,
        "solver_seconds": solver.WallTime(),
    }


def mth1w_cgc1w_witness(data):
    """Return the stable two-course deficiency witness from the full fixture."""

    sections_by_offering = _positive_sections_by_offering(data)
    students_by_course = defaultdict(set)
    requests_by_course = defaultdict(list)
    for request in _ordinary_requests(data):
        students_by_course[request.course_id].add(request.student_id)
        requests_by_course[request.course_id].append(request)
    common_students = students_by_course[1] & students_by_course[5]
    sections_by_course = {}
    for course_id in (1, 5):
        sections_by_course[course_id] = {
            section.section_id: section
            for request in requests_by_course[course_id]
            for section in sections_by_offering[request.course_offering_id]
        }
    cells = sorted({
        (section.semester, section.timeslot_id)
        for section in sections_by_course[1].values()
    })
    reachable_capacity = {
        candidate_cell: sum(
            section.capacity_max
            for course_id in (1, 5)
            for section in sections_by_course[course_id].values()
            if (section.semester, section.timeslot_id) == candidate_cell
        )
        for candidate_cell in cells
    }
    cell = min(reachable_capacity, key=lambda item: (reachable_capacity[item], item))
    return {
        "courses": ("MTH1W", "CGC1W"),
        "course_ids": (1, 5),
        "shared_student_count": len(common_students),
        "reachable_cells": cells,
        "witness_cell": cell,
        "reachable_capacity": reachable_capacity[cell],
        "deficiency": len(common_students) - reachable_capacity[cell],
    }


def correlated_pair_capacity_checks(data):
    """Check shared-student pairs that reuse the same two timing cells."""

    sections_by_offering = _positive_sections_by_offering(data)
    requests_by_course = defaultdict(list)
    students_by_course = defaultdict(set)
    for request in _ordinary_requests(data):
        requests_by_course[request.course_id].append(request)
        students_by_course[request.course_id].add(request.student_id)
    sections_by_course = {}
    cells_by_course = {}
    capacity_by_course_cell = {}
    for course_id, requests in requests_by_course.items():
        sections = {
            section.section_id: section
            for request in requests
            for section in sections_by_offering[request.course_offering_id]
        }
        sections_by_course[course_id] = sections
        cells = tuple(sorted({
            (section.semester, section.timeslot_id)
            for section in sections.values()
        }))
        cells_by_course[course_id] = cells
        capacity_by_course_cell[course_id] = {
            cell: sum(
                section.capacity_max
                for section in sections.values()
                if (section.semester, section.timeslot_id) == cell
            )
            for cell in cells
        }

    checks = []
    course_ids = sorted(requests_by_course)
    for index, left_id in enumerate(course_ids):
        for right_id in course_ids[index + 1:]:
            cells = cells_by_course[left_id]
            if len(cells) != 2 or cells != cells_by_course[right_id]:
                continue
            shared = students_by_course[left_id] & students_by_course[right_id]
            if not shared:
                continue
            capacity = {
                cell: (
                    capacity_by_course_cell[left_id][cell]
                    + capacity_by_course_cell[right_id][cell]
                )
                for cell in cells
            }
            margins = {cell: capacity[cell] - len(shared) for cell in cells}
            checks.append({
                "course_ids": (left_id, right_id),
                "shared_student_count": len(shared),
                "cells": cells,
                "capacity_by_cell": capacity,
                "margin_by_cell": margins,
                "sufficient": all(margin >= 0 for margin in margins.values()),
            })
    return tuple(checks)


def v2_3_cross_grade_shared_course_witness(data):
    """Return the smallest remaining Grade 9/10 cross-grade witness."""

    sections_by_offering = _positive_sections_by_offering(data)
    grades = dict(data.student_grades)
    ordinary = _ordinary_requests(data)
    fra1 = {
        request.student_id
        for request in ordinary
        if request.course_id == 3 and grades[request.student_id] == 9
    }
    fra2 = {
        request.student_id
        for request in ordinary
        if request.course_id == 11 and grades[request.student_id] == 10
    }
    tij9 = {
        request.student_id
        for request in ordinary
        if request.course_id == 7 and grades[request.student_id] == 9
    }
    tij10 = {
        request.student_id
        for request in ordinary
        if request.course_id == 7 and grades[request.student_id] == 10
    }
    left_courses = (3, 11)
    left_cells = {}
    left_capacity_by_cell = {}
    for course_id in left_courses:
        sections = {
            section.section_id: section
            for request in ordinary
            if request.course_id == course_id
            for section in sections_by_offering[request.course_offering_id]
        }
        cells = tuple(sorted({
            (section.semester, section.timeslot_id)
            for section in sections.values()
        }))
        left_cells[course_id] = cells
        left_capacity_by_cell[course_id] = {
            cell: sum(
                section.capacity_max
                for section in sections.values()
                if (section.semester, section.timeslot_id) == cell
            )
            for cell in cells
        }
    shared_by_grade = {
        9: len(fra1 & tij9),
        10: len(fra2 & tij10),
    }
    s2d = (2, 8)
    s1c = (1, 3)
    required_tij_s2d = sum(
        shared_count - left_capacity_by_cell[course_id][s2d]
        for shared_count, course_id in zip(shared_by_grade.values(), left_courses)
    )
    tij_s2d_capacity = sum(
        section.capacity_max
        for section in {
            section.section_id: section
            for request in ordinary
            if request.course_id == 7
            for section in sections_by_offering[request.course_offering_id]
        }.values()
        if (section.semester, section.timeslot_id) == s2d
    )
    return {
        "left_courses": ("FRA1W", "FRA2D"),
        "shared_course": "TIJ1O",
        "shared_by_grade": shared_by_grade,
        "left_candidate_cells": {
            3: left_cells[3],
            11: left_cells[11],
        },
        "left_s2d_capacity": {
            3: left_capacity_by_cell[3][s2d],
            11: left_capacity_by_cell[11][s2d],
        },
        "witness_cell": s2d,
        "paired_side_cell": s1c,
        "required_shared_course_use_in_s2d": required_tij_s2d,
        "shared_course_s2d_capacity": tij_s2d_capacity,
        "deficiency": required_tij_s2d - tij_s2d_capacity,
    }


def run_v2_3_diagnostics():
    fixture = build_paul_desmarais_shaped_g9_12_stress_v2_3_fixture()
    audit = summarize_paul_desmarais_shaped_g9_12_stress_fixture(fixture)
    preflight = preflight_individual_feasibility(fixture.input_data)
    capacity = capacity_only_matching(fixture.input_data)
    correlated = correlated_pair_capacity_checks(fixture.input_data)
    collision = collision_diagnostic(fixture.input_data)
    topology = audit["topology"]
    return {
        "fixture_id": fixture.benchmark_id,
        "version": fixture.benchmark_version,
        "fingerprint": fixture.fixture_fingerprint,
        "static_audit": {
            "student_count": audit["student_count"],
            "grade_distribution": audit["grade_distribution"],
            "section_counts": topology["section_counts"],
            "ordinary_total_seats": sum(
                section.capacity_max
                for section in fixture.input_data.sections
                if section.section_id > 0
            ),
            "zero_demand_courses_omitted": topology["zero_demand_courses_omitted"],
        },
        "isolated_preflight": {
            "student_count": preflight["student_count"],
            "feasible_count": preflight["feasible_count"],
            "infeasible_count": preflight["infeasible_count"],
            "unresolved_count": preflight["unresolved_count"],
        },
        "capacity_only_matching": capacity,
        "correlated_pair_checks": {
            "checked_count": len(correlated),
            "failing_count": sum(not item["sufficient"] for item in correlated),
            "checks": correlated,
        },
        "collision_diagnostic": collision,
        "remaining_witness": v2_3_cross_grade_shared_course_witness(
            fixture.input_data
        ),
    }


def build_small_collision_regression_input() -> StudentAssignmentInputDTO:
    """Build three individually feasible programs with a two-cell conflict."""

    requests = tuple(
        StudentAssignmentRequestDTO(
            request_id=(student_id - 1) * 2 + course_id,
            student_id=student_id,
            course_id=course_id,
            course_offering_id=1000 + course_id,
            is_primary=True,
            is_mandatory=True,
            priority_tier=1,
        )
        for student_id in (1, 2, 3)
        for course_id in (1, 2)
    )
    sections = (
        StudentAssignmentSectionDTO(1, 1, (1001,), (1,), 1, 1, 1, 1),
        StudentAssignmentSectionDTO(2, 1, (1001,), (1,), 2, 2, 2, 2),
        StudentAssignmentSectionDTO(3, 2, (1002,), (2,), 1, 1, 1, 1),
        StudentAssignmentSectionDTO(4, 2, (1002,), (2,), 2, 2, 2, 2),
    )
    timeslots = (
        TimeSlotDTO(1, 1, 1, "A"),
        TimeSlotDTO(2, 1, 2, "A"),
    )
    return StudentAssignmentInputDTO(
        academic_year_id=1,
        requests=requests,
        sections=sections,
        fixed_enrollments=(),
        hard_prerequisites=(),
        soft_sequence_preferences=(),
        section_utilization_balance_importance="not_important",
        student_semester_balance_importance="not_important",
        course_sequence_preferences_importance="not_important",
        timeslots=timeslots,
        student_grades=((1, 9), (2, 9), (3, 9)),
    )


def run_v2_2_diagnostics():
    fixture = reconstruct_paul_desmarais_v2_2()
    audit = summarize_paul_desmarais_shaped_g9_12_stress_fixture(fixture)
    preflight = preflight_individual_feasibility(fixture.input_data)
    capacity = capacity_only_matching(fixture.input_data)
    collision = collision_diagnostic(fixture.input_data)
    topology = audit["topology"]
    return {
        "fixture_id": fixture.benchmark_id,
        "fingerprint": fixture.fixture_fingerprint,
        "static_audit": {
            "student_count": audit["student_count"],
            "grade_distribution": audit["grade_distribution"],
            "section_counts": topology["section_counts"],
            "ordinary_total_seats": sum(
                section.capacity_max
                for section in fixture.input_data.sections
                if section.section_id > 0
            ),
            "ordinary_full_course_seats": sum(
                item["capacity_max_total"]
                for item in topology["course_topology"].values()
            ),
            "half_pair": topology["half_pair"],
            "online_supervision": topology["online_supervision"],
            "zero_demand_courses_omitted": topology["zero_demand_courses_omitted"],
        },
        "isolated_preflight": {
            "student_count": preflight["student_count"],
            "feasible_count": preflight["feasible_count"],
            "infeasible_count": preflight["infeasible_count"],
            "unresolved_count": preflight["unresolved_count"],
        },
        "capacity_only_matching": capacity,
        "collision_diagnostic": collision,
        "known_witness": mth1w_cgc1w_witness(fixture.input_data),
    }


def _run_explicit_stage1():
    from .student_assignment.core import _solve_student_assignment

    fixture = reconstruct_paul_desmarais_v2_2()
    data = replace(fixture.input_data, time_limit_seconds=120.0)
    result = _solve_student_assignment(
        data,
        include_lock_costs=False,
        include_candidate_ledger=False,
        use_hard_feasibility_bootstrap=True,
        local_only=True,
        hard_feasibility_time_limit_seconds=120.0,
        hard_feasibility_worker_count=8,
    )
    stage1 = result.optimization_facts.get("stage_1", {})
    return {
        "fixture_id": fixture.benchmark_id,
        "fingerprint": fixture.fixture_fingerprint,
        "effective_time_limit_seconds": data.time_limit_seconds,
        "worker_count": stage1.get("timings", {}).get("seed_worker_count", 8),
        "result_status": result.status,
        "result_solver_outcome": result.solver_outcome,
        "stage1_solver_outcome": stage1.get("solver_outcome"),
        "complete_seed_produced": stage1.get("complete_seed_produced"),
        "seed_validated": stage1.get("seed_validated_against_full_model"),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--full-stage1",
        action="store_true",
        help="Run the explicit historical 120-second Stage 1-only reproduction.",
    )
    args = parser.parse_args(argv)
    result = run_v2_2_diagnostics()
    small = build_small_collision_regression_input()
    small_preflight = preflight_individual_feasibility(small)
    result["small_collision_regression"] = {
        "student_count": small_preflight["student_count"],
        "feasible_count": small_preflight["feasible_count"],
        "capacity_only_matching": capacity_only_matching(small),
        "collision_diagnostic": collision_diagnostic(small),
    }
    if args.full_stage1:
        result["full_stage1"] = _run_explicit_stage1()
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
