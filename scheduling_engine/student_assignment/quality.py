"""Solver-aligned, measurement-only student-schedule quality evaluation.

This module deliberately does not build a CP-SAT model and does not participate
in search.  It reconstructs the existing objective expressions from a complete
candidate so benchmark and review code can explain what changed between the
validated Stage 1 seed and the Stage 2 recommendation.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from math import ceil

from ..dto import StudentAssignmentInputDTO
from .occupancy import (
    fixed_enrollment_occupied_half_segments,
    occupied_half_segments,
)


# The version is bumped when report semantics change, so consumers can
# distinguish the fixed-context/semester-aligned payload from earlier reports.
QUALITY_REPORT_VERSION = "student_schedule_quality_v3"


def _rounded(value):
    """Keep JSON reports readable without changing integer solver quantities."""

    if isinstance(value, float):
        return round(value, 6)
    return value


def _distribution(values):
    """Return deterministic descriptive statistics using nearest-rank percentiles."""

    numbers = sorted(float(value) for value in values)
    if not numbers:
        return {
            "count": 0,
            "minimum": None,
            "mean": None,
            "median": None,
            "p75": None,
            "p90": None,
            "p95": None,
            "maximum": None,
        }

    def percentile(percent):
        index = max(0, ceil(len(numbers) * percent) - 1)
        return _rounded(numbers[index])

    middle = len(numbers) // 2
    median = (
        numbers[middle]
        if len(numbers) % 2
        else (numbers[middle - 1] + numbers[middle]) / 2
    )
    return {
        "count": len(numbers),
        "minimum": _rounded(numbers[0]),
        "mean": _rounded(sum(numbers) / len(numbers)),
        "median": _rounded(median),
        "p75": percentile(0.75),
        "p90": percentile(0.90),
        "p95": percentile(0.95),
        "maximum": _rounded(numbers[-1]),
    }


def _segments(segment, *, is_online=False):
    # The solver's category-presence expression uses physical supervision
    # occupancy for online requests, including an online half-semester course.
    # This intentionally mirrors request_occupied_half_segments(), rather than
    # substituting a more intuitive academic-duration interpretation.
    if is_online:
        return ("first_half", "second_half")
    return occupied_half_segments(segment)


def _active_fixed_enrollments(data):
    return tuple(
        row for row in data.fixed_enrollments
        if row.is_active and not row.is_historical
    )


def _student_ids(data, assignments, commitment_assignments):
    return tuple(sorted(
        {
            request.student_id for request in data.requests
        } | {
            row.student_id for row in _active_fixed_enrollments(data)
        } | {
            request.student_id for request in data.schedule_commitment_requests
        } | {
            commitment.student_id
            for commitment in data.fixed_schedule_commitments
            if commitment.is_active and not commitment.is_historical
        } | {
            assignment.student_id for assignment in assignments
        } | {
            commitment.student_id for commitment in commitment_assignments
        }
    ))


def _focus_student_ids(data):
    return {
        request.student_id
        for request in data.schedule_commitment_requests
        if request.commitment_type == "focus"
    } | {
        commitment.student_id
        for commitment in data.fixed_schedule_commitments
        if commitment.is_active
        and not commitment.is_historical
        and commitment.commitment_kind == "focus"
    }


def _course_maps(data):
    requests = {request.request_id: request for request in data.requests}
    difficulty = {
        item.course_id: item.effective_difficulty
        for item in data.course_difficulties
    }
    categories = {
        item.course_id: item.category
        for item in data.course_difficulties
    }
    return requests, difficulty, categories


def _semester_by_timeslot(data):
    return {slot.id: slot.semester for slot in data.timeslots}


def _candidate_maps(data, assignments, commitment_assignments):
    requests, difficulty, categories = _course_maps(data)
    assignments_by_request = {
        assignment.request_id: assignment for assignment in assignments
    }
    return (
        requests,
        difficulty,
        categories,
        assignments_by_request,
        tuple(commitment_assignments),
    )


def _section_utilization(data, assignments):
    counts = defaultdict(int)
    for row in _active_fixed_enrollments(data):
        counts[row.section_id] += 1
    for assignment in assignments:
        section_id = (
            assignment.section_id
            if assignment.section_id is not None
            else -assignment.online_supervision_session_id
        )
        counts[section_id] += 1

    sections_by_id = {section.section_id: section for section in data.sections}
    groups = defaultdict(list)
    for section in data.sections:
        groups[section.delivery_group_id].append(section)

    group_metrics = {}
    pairwise_penalty = 0
    ranges = []
    average_deviations = []
    perfectly_balanced_count = 0
    within_one_count = 0
    for group_id, sections in sorted(groups.items()):
        ordered = sorted(sections, key=lambda item: item.section_id)
        values = [counts[section.section_id] for section in ordered]
        pairwise = sum(
            abs(left - right)
            for index, left in enumerate(values)
            for right in values[index + 1:]
        )
        pairwise_penalty += pairwise
        imbalance_range = max(values, default=0) - min(values, default=0)
        ranges.append(imbalance_range)
        average = sum(values) / len(values) if values else 0
        average_deviation = (
            sum(abs(value - average) for value in values) / len(values)
            if values else 0
        )
        average_deviations.append(average_deviation)
        perfectly_balanced_count += int(imbalance_range == 0)
        within_one_count += int(imbalance_range <= 1)
        group_metrics[str(group_id)] = {
            "section_enrollment_counts": {
                str(section.section_id): counts[section.section_id]
                for section in ordered
            },
            "pairwise_absolute_difference": pairwise,
            "range": imbalance_range,
            "average_absolute_deviation": _rounded(average_deviation),
        }

    return {
        "solver_aligned_penalty": pairwise_penalty,
        "delivery_group_count": len(group_metrics),
        # The solver objective is the sum of these per-group pairwise
        # contributions.  Keep their distribution so diagnostic comparisons
        # can reconcile exactly to the authoritative aggregate; ``range`` is
        # still retained below as a counselor-readable shape descriptor.
        "pairwise_imbalance_distribution": _distribution(
            metric["pairwise_absolute_difference"]
            for metric in group_metrics.values()
        ),
        "delivery_group_imbalance_distribution": _distribution(ranges),
        "average_section_deviation_distribution": _distribution(average_deviations),
        "perfectly_balanced_group_count": perfectly_balanced_count,
        "within_one_enrollment_group_count": within_one_count,
        "within_one_enrollment_group_percentage": (
            _rounded(within_one_count / len(group_metrics))
            if group_metrics else None
        ),
        "entities": group_metrics,
    }


def _student_loads(data, assignments, commitment_assignments):
    requests, _difficulty, _categories, assignments_by_request, commitment_rows = (
        _candidate_maps(data, assignments, commitment_assignments)
    )
    semesters = _semester_by_timeslot(data)
    loads = {
        student_id: {"semester_1": 0, "semester_2": 0}
        for student_id in _student_ids(data, assignments, commitment_assignments)
        if student_id not in _focus_student_ids(data)
    }

    def add(student_id, semester, amount):
        if student_id in loads and semester in (1, 2):
            loads[student_id][f"semester_{semester}"] += amount

    for row in _active_fixed_enrollments(data):
        add(row.student_id, row.semester, round(row.credit_value * 2))
    for request_id, assignment in assignments_by_request.items():
        request = requests[request_id]
        if request.delivery_kind != "co_op":
            add(assignment.student_id, assignment.semester, round(request.credit_value * 2))
    for commitment in data.fixed_schedule_commitments:
        if not commitment.is_active or commitment.is_historical:
            continue
        if commitment.commitment_kind != "co_op":
            continue
        for semester in set(
            semesters.get(timeslot_id)
            for timeslot_id, _segment in commitment.occupancy
        ):
            add(commitment.student_id, semester, round(commitment.credit_value * 2))
    for commitment in commitment_rows:
        if commitment.commitment_kind != "co_op":
            continue
        request = requests.get(commitment.course_request_id or commitment.request_id)
        amount = round((request.credit_value if request else 2.0) * 2)
        for semester in set(
            semesters.get(timeslot_id)
            for timeslot_id, _segment in commitment.occupancy
        ):
            add(commitment.student_id, semester, amount)

    for student_load in loads.values():
        student_load["absolute_difference"] = abs(
            student_load["semester_1"] - student_load["semester_2"]
        )
    differences = [item["absolute_difference"] for item in loads.values()]
    return {
        "solver_aligned_penalty": sum(differences),
        "distribution": _distribution(differences),
        "perfectly_balanced_count": sum(value == 0 for value in differences),
        "entities": {str(student_id): value for student_id, value in loads.items()},
    }


def _difficulty_loads(data, assignments, commitment_assignments):
    requests, difficulty, _categories, assignments_by_request, commitment_rows = (
        _candidate_maps(data, assignments, commitment_assignments)
    )
    semesters = _semester_by_timeslot(data)
    loads = {
        student_id: {"semester_1": 0, "semester_2": 0}
        for student_id in _student_ids(data, assignments, commitment_assignments)
        if student_id not in _focus_student_ids(data)
    }

    def add(student_id, semester, amount):
        if student_id in loads and semester in (1, 2):
            loads[student_id][f"semester_{semester}"] += amount

    for row in _active_fixed_enrollments(data):
        add(
            row.student_id,
            row.semester,
            round(difficulty.get(row.course_id, 0) * row.credit_value),
        )
    for request_id, assignment in assignments_by_request.items():
        request = requests[request_id]
        if request.delivery_kind != "co_op":
            add(
                assignment.student_id,
                assignment.semester,
                round(difficulty.get(request.course_id, 0) * request.credit_value),
            )
    for commitment in data.fixed_schedule_commitments:
        if not commitment.is_active or commitment.is_historical:
            continue
        if commitment.commitment_kind == "study":
            amount = 1
        elif commitment.commitment_kind == "co_op":
            amount = round(difficulty.get(commitment.course_id, 0) * commitment.credit_value)
        else:
            continue
        for semester in set(
            semesters.get(timeslot_id)
            for timeslot_id, _segment in commitment.occupancy
        ):
            add(commitment.student_id, semester, amount)
    for commitment in commitment_rows:
        if commitment.commitment_kind == "study":
            amount = 1
        elif commitment.commitment_kind == "co_op":
            request = requests.get(commitment.course_request_id or commitment.request_id)
            amount = round(
                difficulty.get(request.course_id, 0) * (request.credit_value if request else 2.0)
            )
        else:
            continue
        for semester in set(
            semesters.get(timeslot_id)
            for timeslot_id, _segment in commitment.occupancy
        ):
            add(commitment.student_id, semester, amount)

    for student_load in loads.values():
        student_load["absolute_difference"] = abs(
            student_load["semester_1"] - student_load["semester_2"]
        )
    differences = [item["absolute_difference"] for item in loads.values()]
    return {
        "solver_aligned_penalty": sum(differences),
        "distribution": _distribution(differences),
        "perfectly_balanced_count": sum(value == 0 for value in differences),
        "entities": {str(student_id): value for student_id, value in loads.items()},
    }


def _category_diversity(data, assignments, commitment_assignments):
    requests, _difficulty, categories, assignments_by_request, _commitments = (
        _candidate_maps(data, assignments, commitment_assignments)
    )
    focus_students = _focus_student_ids(data)
    presence = defaultdict(set)
    course_ids_by_student = defaultdict(set)

    for row in _active_fixed_enrollments(data):
        if row.student_id in focus_students:
            continue
        course_ids_by_student[row.student_id].add(row.course_id)
        for segment in fixed_enrollment_occupied_half_segments(row):
            presence[row.student_id, row.course_id, row.semester].add(segment)
    for request_id, assignment in assignments_by_request.items():
        request = requests[request_id]
        if request.student_id in focus_students or request.delivery_kind == "co_op":
            continue
        course_ids_by_student[request.student_id].add(request.course_id)
        for segment in _segments(
            assignment.half_semester_segment,
            is_online=request.delivery_kind == "online",
        ):
            presence[request.student_id, request.course_id, assignment.semester].add(segment)

    similarity = {
        tuple(sorted((item.category_a, item.category_b))): item.similarity_score
        for item in data.course_category_relationships
    }

    def pair_score(left, right):
        left_category = categories.get(left, "")
        right_category = categories.get(right, "")
        if not left_category or not right_category:
            return 0
        if left_category == right_category:
            return 100
        return similarity.get(tuple(sorted((left_category, right_category))), 0)

    penalties = defaultdict(int)
    pair_penalties = {}
    for student_id, course_ids in course_ids_by_student.items():
        ordered = sorted(course_ids)
        for index, left in enumerate(ordered):
            for right in ordered[index + 1:]:
                score = pair_score(left, right)
                if not score:
                    continue
                for semester in (1, 2):
                    shared_half_count = len(
                        presence[student_id, left, semester]
                        & presence[student_id, right, semester]
                    )
                    # CP-SAT first sums the shared-half Boolean values and
                    # then applies one integer division to that total.  Do
                    # not floor each half independently: odd relationship
                    # scores would otherwise under-report a full/full overlap.
                    value = (score * shared_half_count) // 2
                    penalties[student_id] += value
                    pair_penalties[
                        f"{student_id}:{left}:{right}:{semester}"
                    ] = value

    values = list(penalties.values())
    return {
        "solver_aligned_penalty": sum(values),
        "distribution": _distribution(values),
        "entities": {str(student_id): penalties[student_id] for student_id in sorted(course_ids_by_student)},
        "pair_penalties": pair_penalties,
    }


def _request_fulfillment(data, assignments, commitment_assignments):
    """Report the existing fulfillment tiers without rebuilding objectives.

    The solver already treats mandatory requests, nominated priority requests,
    primary priority tiers, and approved backups as separate lexicographic
    objectives.  This evaluator records the same facts from the returned
    candidate so a Stage 1/Stage 2 comparison does not hide a fulfillment
    trade-off behind the balancing metrics.
    """

    assigned_request_ids = {assignment.request_id for assignment in assignments}
    assigned_request_ids.update(
        commitment.course_request_id
        for commitment in commitment_assignments
        if commitment.course_request_id is not None
    )
    priority_request_ids = set(data.priority_request_ids)
    requests_by_id = {request.request_id: request for request in data.requests}
    entities = {}
    counts = {
        "mandatory": 0,
        "priority_primary": 0,
        "primary": 0,
        "approved_backup": 0,
    }
    eligible_counts = {
        "mandatory": 0,
        "priority_primary": 0,
        "primary": 0,
        "approved_backup": 0,
    }

    for request_id, request in sorted(requests_by_id.items()):
        if request.is_mandatory:
            tier = "mandatory"
        elif request.is_primary and request_id in priority_request_ids:
            tier = "priority_primary"
        elif request.is_primary:
            tier = "primary"
        else:
            tier = "approved_backup"
        fulfilled = int(request_id in assigned_request_ids)
        entities[str(request_id)] = {
            "student_id": request.student_id,
            "tier": tier,
            "fulfilled": fulfilled,
        }
        eligible_counts[tier] += 1
        counts[tier] += fulfilled

    commitment_entities = {}
    co_op_request_ids = {
        request.request_id
        for request in data.requests
        if request.delivery_kind == "co_op"
    }
    requested_commitments = {
        **{
            ("commitment", request.request_id): request.commitment_type
            for request in data.schedule_commitment_requests
        },
        **{
            ("course", request_id): "co_op"
            for request_id in co_op_request_ids
        },
    }
    assigned_commitment_sources = {
        (
            "course", commitment.course_request_id
        ) if commitment.course_request_id is not None else (
            "commitment", commitment.request_id
        )
        for commitment in commitment_assignments
    }
    for source_key, commitment_kind in sorted(requested_commitments.items()):
        fulfilled = int(source_key in assigned_commitment_sources)
        commitment_entities[f"{source_key[0]}:{source_key[1]}"] = {
            "commitment_kind": commitment_kind,
            "source_kind": source_key[0],
            "source_request_id": source_key[1],
            "fulfilled": fulfilled,
        }

    return {
        "solver_aligned_counts": counts,
        "eligible_counts": eligible_counts,
        "unmet_counts": {
            tier: eligible_counts[tier] - counts[tier]
            for tier in counts
        },
        "special_commitments": {
            "requested_count": len(requested_commitments),
            "fulfilled_count": len(
                requested_commitments.keys() & assigned_commitment_sources
            ),
            "unmet_count": len(
                requested_commitments.keys() - assigned_commitment_sources
            ),
            "entities": commitment_entities,
        },
        "entities": entities,
    }


def _sequence_quality(
    data,
    assignments,
    sequence_opportunities=None,
):
    requests, _difficulty, _categories = _course_maps(data)
    course_by_student = defaultdict(dict)
    for row in _active_fixed_enrollments(data):
        course_by_student[row.student_id][row.course_id] = row.semester
    for assignment in assignments:
        request = requests[assignment.request_id]
        course_by_student[assignment.student_id][request.course_id] = assignment.semester

    if sequence_opportunities is None:
        opportunities = []
        for preference in data.soft_sequence_preferences:
            for student_id, courses in course_by_student.items():
                if preference.earlier_course_id in courses or preference.later_course_id in courses:
                    opportunities.append((
                        student_id,
                        preference.earlier_course_id,
                        preference.later_course_id,
                    ))
    else:
        opportunities = list(sequence_opportunities)

    entities = {}
    for student_id, earlier, later in opportunities:
        courses = course_by_student[student_id]
        satisfied = int(
            courses.get(earlier) == 1 and courses.get(later) == 2
        )
        entities[f"{student_id}:{earlier}:{later}"] = satisfied
    satisfied = sum(entities.values())
    applicable_students = {key.split(":", 1)[0] for key in entities}
    satisfied_students = {
        key.split(":", 1)[0]
        for key, value in entities.items()
        if value
    }
    return {
        "solver_aligned_satisfied": satisfied,
        "solver_aligned_penalty": -satisfied,
        "eligible_opportunity_count": len(opportunities),
        "applicable_student_count": len(applicable_students),
        "satisfied_opportunity_count": satisfied,
        "unsatisfied_opportunity_count": len(opportunities) - satisfied,
        "students_with_satisfied_opportunity_count": len(satisfied_students),
        "satisfaction_rate": (
            _rounded(satisfied / len(opportunities)) if opportunities else None
        ),
        "entities": entities,
    }


def _preservation_quality(data, assignments):
    if data.schedule_preservation_level == "none":
        return {
            "applicable": False,
            "solver_aligned_penalty": 0,
            "movable_enrollment_count": 0,
            "preserved_enrollment_count": 0,
            "moved_enrollment_count": 0,
            "preservation_rate": None,
            "affected_student_count": 0,
            "moves_per_affected_student": _distribution(()),
            "entities": {},
        }
    requests = {request.request_id: request for request in data.requests}
    assignments_by_request = {row.request_id: row for row in assignments}
    movable = {}
    for row in _active_fixed_enrollments(data):
        request = next(
            (
                item for item in requests.values()
                if item.current_enrollment_id == row.enrollment_id
            ),
            None,
        )
        if row.is_locked or not row.is_in_scope or request is None:
            continue
        movable[row.enrollment_id] = (row, request)
    moved_by_student = defaultdict(int)
    preserved = 0
    moved = 0
    for enrollment_id, (enrollment, request) in movable.items():
        assignment = assignments_by_request.get(request.request_id)
        if assignment is not None and assignment.section_id == enrollment.section_id:
            preserved += 1
        elif assignment is not None:
            moved += 1
            moved_by_student[enrollment.student_id] += 1
    total = preserved + moved
    return {
        "applicable": True,
        "solver_aligned_penalty": moved,
        "movable_enrollment_count": total,
        "preserved_enrollment_count": preserved,
        "moved_enrollment_count": moved,
        "preservation_rate": _rounded(preserved / total) if total else None,
        "affected_student_count": len(moved_by_student),
        "moves_per_affected_student": _distribution(moved_by_student.values()),
        "entities": {str(student_id): count for student_id, count in moved_by_student.items()},
    }


def evaluate_student_assignment_quality(
    data: StudentAssignmentInputDTO,
    *,
    assignments=(),
    commitment_assignments=(),
    sequence_opportunities=None,
    solver_objective_components=None,
    include_entity_metrics=True,
    fixed_enrollments=None,
    fixed_schedule_commitments=None,
):
    """Evaluate one complete candidate without influencing CP-SAT.

    ``sequence_opportunities`` may be supplied by the solver when exact static
    candidate applicability is required.  Standalone callers can omit it and
    receive a conservative applicability calculation from the candidate.
    """

    assignments = tuple(assignments)
    commitment_assignments = tuple(commitment_assignments)
    # A rerun snapshot contains both fixed context and movable/replaced active
    # facts.  The solver has already resolved that distinction; accept the
    # resolved rows so diagnostic reconstruction does not count a movable old
    # enrollment or commitment alongside its replacement.  The original data
    # remains available below for schedule-preservation facts, which are about
    # those movable rows by definition.
    evaluation_data = data
    if fixed_enrollments is not None or fixed_schedule_commitments is not None:
        evaluation_data = replace(
            data,
            fixed_enrollments=(
                tuple(data.fixed_enrollments)
                if fixed_enrollments is None else tuple(fixed_enrollments)
            ),
            fixed_schedule_commitments=(
                tuple(data.fixed_schedule_commitments)
                if fixed_schedule_commitments is None
                else tuple(fixed_schedule_commitments)
            ),
        )
    metrics = {
        "version": QUALITY_REPORT_VERSION,
        "request_fulfillment": _request_fulfillment(
            evaluation_data, assignments, commitment_assignments,
        ),
        "section_utilization_balance": _section_utilization(
            evaluation_data, assignments,
        ),
        "student_semester_load_balance": _student_loads(
            evaluation_data, assignments, commitment_assignments,
        ),
        "course_sequence_preferences": _sequence_quality(
            evaluation_data, assignments, sequence_opportunities,
        ),
        "difficulty_balance": _difficulty_loads(
            evaluation_data, assignments, commitment_assignments,
        ),
        "course_category_diversity": _category_diversity(
            evaluation_data, assignments, commitment_assignments,
        ),
        "schedule_preservation": _preservation_quality(data, assignments),
    }
    if not include_entity_metrics:
        metrics = _without_entity_detail(metrics)
    if solver_objective_components is not None:
        # CP-SAT remains authoritative for aggregate objective values.  The
        # per-entity reconstruction is intentionally retained as an audit
        # signal: it makes an adapter/evaluator mismatch visible instead of
        # allowing a descriptive calculation to masquerade as the solver's
        # actual value.
        objective_metric_keys = {
            "section_utilization_balance": "section_utilization_balance_penalty",
            "student_semester_load_balance": "student_semester_balance_penalty",
            "difficulty_balance": "difficulty_balance_penalty",
            "course_category_diversity": "course_category_diversity_penalty",
            "schedule_preservation": "schedule_preservation_move_penalty",
        }
        for metric_key, objective_key in objective_metric_keys.items():
            if objective_key not in solver_objective_components:
                continue
            metric = metrics[metric_key]
            reconstructed = metric["solver_aligned_penalty"]
            authoritative = solver_objective_components[objective_key]
            metric["reconstructed_penalty"] = reconstructed
            metric["reconstruction_delta"] = authoritative - reconstructed
            metric["solver_aligned_penalty"] = authoritative
        if "soft_sequence_preferences_satisfied" in solver_objective_components:
            metric = metrics["course_sequence_preferences"]
            reconstructed = metric["solver_aligned_penalty"]
            authoritative = -solver_objective_components[
                "soft_sequence_preferences_satisfied"
            ]
            metric["reconstructed_penalty"] = reconstructed
            metric["reconstruction_delta"] = authoritative - reconstructed
            metric["solver_aligned_penalty"] = authoritative
    return metrics


def _compare_maps(before, after, *, lower_is_better=True):
    keys = sorted(set(before) | set(after))
    improved = unchanged = worsened = 0
    raw_deltas = []
    improvement_magnitudes = []
    worsening_magnitudes = []
    for key in keys:
        left = before.get(key, 0)
        right = after.get(key, 0)
        raw_deltas.append(right - left)
        if right == left:
            unchanged += 1
        elif (right < left) == lower_is_better:
            improved += 1
            improvement_magnitudes.append(
                (left - right) if lower_is_better else (right - left)
            )
        else:
            worsened += 1
            worsening_magnitudes.append(
                (right - left) if lower_is_better else (left - right)
            )
    denominator = sum(before.values())
    return {
        "improved": improved,
        "unchanged": unchanged,
        "worsened": worsened,
        "change": {
            "sum_delta": sum(raw_deltas),
            "mean_delta": (
                sum(raw_deltas) / len(raw_deltas) if raw_deltas else None
            ),
            "percentage_change": (
                (sum(after.values()) - denominator) / abs(denominator)
                if denominator else None
            ),
            "mean_improvement": (
                sum(improvement_magnitudes) / len(improvement_magnitudes)
                if improvement_magnitudes else None
            ),
            "mean_worsening": (
                sum(worsening_magnitudes) / len(worsening_magnitudes)
                if worsening_magnitudes else None
            ),
        },
    }


def compare_student_assignment_quality(stage_1, stage_2):
    """Compare independent entity outcomes without changing solver behavior."""

    mappings = (
        ("request_fulfillment", "fulfilled", False),
        # Section utilization is optimized as pairwise absolute difference,
        # not max-minus-min range.  Comparing the same per-group contribution
        # is required for aggregate/entity parity.
        ("section_utilization_balance", "pairwise_absolute_difference", True),
        ("student_semester_load_balance", "absolute_difference", True),
        ("difficulty_balance", "absolute_difference", True),
        ("course_category_diversity", None, True),
        ("course_sequence_preferences", None, False),
        ("schedule_preservation", None, True),
    )
    comparison = {}
    for metric_name, value_key, lower_is_better in mappings:
        before = stage_1.get(metric_name, {}).get("entities", {})
        after = stage_2.get(metric_name, {}).get("entities", {})
        if value_key is not None:
            before = {key: value.get(value_key, 0) for key, value in before.items()}
            after = {key: value.get(value_key, 0) for key, value in after.items()}
        comparison[metric_name] = _compare_maps(
            before, after, lower_is_better=lower_is_better,
        )
        if metric_name == "section_utilization_balance":
            # Preserve the range-based view as descriptive context without
            # confusing it with the solver-aligned improvement count.
            before_ranges = stage_1.get(metric_name, {}).get("entities", {})
            after_ranges = stage_2.get(metric_name, {}).get("entities", {})
            comparison[metric_name]["range_comparison"] = _compare_maps(
                {
                    key: value.get("range", 0)
                    for key, value in before_ranges.items()
                },
                {
                    key: value.get("range", 0)
                    for key, value in after_ranges.items()
                },
                lower_is_better=True,
            )
    return comparison


def compact_student_assignment_quality(report):
    """Remove per-entity detail before embedding a report in a run payload."""

    return _without_entity_detail(report)


def _without_entity_detail(value):
    """Strip nested entity and pair detail from a report copy."""

    if isinstance(value, dict):
        return {
            key: _without_entity_detail(item)
            for key, item in value.items()
            if key not in {"entities", "pair_penalties"}
        }
    if isinstance(value, list):
        return [_without_entity_detail(item) for item in value]
    return value
