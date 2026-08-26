"""Solver-neutral guidance for targeted utilization-cluster diagnostics.

Section utilization remains a global Objective Semantics v2 metric.  This
module only identifies students whose currently assigned source decisions have
cheap, optimistic opportunities to affect imbalanced parallel sections.  The
facts here never authorize a move; CP-SAT and the unchanged full-model
validator remain the only authorities.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass


@dataclass(frozen=True)
class DeliveryGroupUtilizationPressure:
    """Deterministic, solver-neutral pressure facts for one delivery group."""

    delivery_group_id: int
    section_counts: tuple[tuple[int, int], ...]
    section_capacities: tuple[tuple[int, int], ...]
    pairwise_penalty: int
    total_penalty_share: float
    overfull_section_ids: tuple[int, ...]
    underfull_section_ids: tuple[int, ...]
    movable_student_ids: tuple[int, ...]
    relevant_student_ids: tuple[int, ...]


@dataclass(frozen=True)
class StudentUtilizationLeverage:
    """Optimistic utilization leverage for one student.

    The move facts intentionally ignore collisions, locks, prerequisites,
    capacity contention, and chain feasibility.  They are search guidance,
    not a student-local objective contribution or a feasibility claim.
    """

    student_id: int
    total_positive_leverage: int
    strongest_single_move: int
    relevant_group_count: int
    alternate_section_opportunity_count: int
    delivery_group_ids: tuple[int, ...]
    move_facts: tuple[dict, ...]
    rank: int = 0

    @property
    def rank_key(self):
        return (
            -self.total_positive_leverage,
            -self.strongest_single_move,
            -self.relevant_group_count,
            -self.alternate_section_opportunity_count,
            self.student_id,
        )


@dataclass(frozen=True)
class UtilizationClusterSelection:
    """A bounded target scope and compact evidence explaining its selection."""

    selected_student_ids: tuple[int, ...]
    policy: str
    target_scope_size: int
    pressure_facts: tuple[DeliveryGroupUtilizationPressure, ...]
    leverage_facts: tuple[StudentUtilizationLeverage, ...]
    guidance_facts: dict


def _source_map(source_decisions):
    if isinstance(source_decisions, dict):
        return dict(source_decisions)
    return dict(tuple(source_decisions or ()))


def _pairwise_penalty(counts):
    values = tuple(counts.values())
    return sum(
        abs(left - right)
        for index, left in enumerate(values)
        for right in values[index + 1:]
    )


def _moved_penalty(counts, source_section_id, target_section_id):
    updated = dict(counts)
    updated[source_section_id] = updated.get(source_section_id, 0) - 1
    updated[target_section_id] = updated.get(target_section_id, 0) + 1
    return _pairwise_penalty(updated)


def _request_can_use_section(request, section):
    if request.delivery_kind == "online" or section.section_id <= 0:
        return False
    return (
        request.course_offering_id in section.member_course_offering_ids
        or request.course_id in section.member_course_ids
    )


def _pressure_rows(data, quality_report):
    entities = quality_report.get("section_utilization_balance", {}).get(
        "entities", {}
    )
    sections_by_group = defaultdict(list)
    sections_by_id = {}
    for section in data.sections:
        if section.section_id <= 0:
            continue
        sections_by_group[section.delivery_group_id].append(section)
        sections_by_id[section.section_id] = section

    rows = []
    counts_by_group = {}
    for group_id, sections in sorted(sections_by_group.items()):
        if len(sections) < 2:
            continue
        entity = entities.get(str(group_id), {})
        raw_counts = entity.get("section_enrollment_counts", {})
        counts = {
            section.section_id: int(raw_counts.get(str(section.section_id), 0))
            for section in sorted(sections, key=lambda item: item.section_id)
        }
        penalty = int(entity.get("pairwise_absolute_difference", _pairwise_penalty(counts)))
        maximum = max(counts.values(), default=0)
        minimum = min(counts.values(), default=0)
        rows.append({
            "delivery_group_id": group_id,
            "sections": tuple(sorted(sections, key=lambda item: item.section_id)),
            "counts": counts,
            "capacities": {
                section.section_id: int(section.capacity_max)
                for section in sections
            },
            "pairwise_penalty": penalty,
            "maximum": maximum,
            "minimum": minimum,
        })
        counts_by_group[group_id] = counts
    return rows, sections_by_id, counts_by_group


def build_utilization_cluster_guidance(
    data,
    quality_report,
    source_decisions,
    *,
    target_scope_size,
    policy="interaction_aware",
    fixed_student_ids=(),
):
    """Build deterministic utilization pressure and cluster-selection facts."""

    target_scope_size = max(0, int(target_scope_size))
    if policy not in {
        "top_individual",
        "delivery_group_focused",
        "interaction_aware",
        "mixed",
    }:
        raise ValueError(f"Unsupported utilization cluster policy: {policy}")

    source_map = _source_map(source_decisions)
    requests_by_id = {request.request_id: request for request in data.requests}
    rows, sections_by_id, counts_by_group = _pressure_rows(data, quality_report)
    row_by_group = {
        row["delivery_group_id"]: row
        for row in rows
    }
    group_by_section = {
        section_id: row["delivery_group_id"]
        for row in rows
        for section_id in row["counts"]
    }
    total_penalty = sum(row["pairwise_penalty"] for row in rows)

    moves_by_student = defaultdict(dict)
    group_students = defaultdict(set)
    for source_key, source_value in sorted(source_map.items(), key=repr):
        if not isinstance(source_key, tuple) or source_key[0] != "course":
            continue
        request = requests_by_id.get(source_key[1])
        if request is None or not isinstance(source_value, tuple) or len(source_value) < 2:
            continue
        student_id, source_section_id = source_value[:2]
        if source_section_id is None or source_section_id not in group_by_section:
            continue
        source_section = sections_by_id.get(source_section_id)
        if source_section is None:
            continue
        group_id = group_by_section[source_section_id]
        row = row_by_group[group_id]
        counts = counts_by_group[group_id]
        for alternate in row["sections"]:
            if alternate.section_id == source_section_id:
                continue
            if not _request_can_use_section(request, alternate):
                continue
            before = row["pairwise_penalty"]
            after = _moved_penalty(counts, source_section_id, alternate.section_id)
            leverage = max(0, before - after)
            move_key = (group_id, source_section_id, alternate.section_id)
            existing = moves_by_student[student_id].get(move_key)
            move = {
                "delivery_group_id": group_id,
                "request_id": request.request_id,
                "from_section_id": source_section_id,
                "to_section_id": alternate.section_id,
                "current_penalty": before,
                "hypothetical_penalty": after,
                "positive_leverage": leverage,
            }
            if existing is None or leverage > existing["positive_leverage"]:
                moves_by_student[student_id][move_key] = move
            group_students[group_id].add(student_id)

    leverage_records = []
    for student_id, moves in sorted(moves_by_student.items()):
        ordered_moves = tuple(sorted(
            moves.values(),
            key=lambda item: (
                -item["positive_leverage"],
                -item["current_penalty"],
                item["delivery_group_id"],
                item["from_section_id"],
                item["to_section_id"],
                item["request_id"],
            ),
        ))
        positive = tuple(item for item in ordered_moves if item["positive_leverage"] > 0)
        groups = tuple(sorted({item["delivery_group_id"] for item in ordered_moves}))
        leverage_records.append(StudentUtilizationLeverage(
            student_id=student_id,
            total_positive_leverage=sum(item["positive_leverage"] for item in positive),
            strongest_single_move=max(
                (item["positive_leverage"] for item in positive),
                default=0,
            ),
            relevant_group_count=len(groups),
            alternate_section_opportunity_count=len(ordered_moves),
            delivery_group_ids=groups,
            move_facts=ordered_moves,
        ))
    leverage_records = sorted(leverage_records, key=lambda item: item.rank_key)
    leverage_by_student = {
        item.student_id: StudentUtilizationLeverage(
            **{**item.__dict__, "rank": index}
        )
        for index, item in enumerate(leverage_records, start=1)
    }

    group_pressure = []
    for row in sorted(rows, key=lambda item: (-item["pairwise_penalty"], item["delivery_group_id"])):
        counts = row["counts"]
        maximum = row["maximum"]
        minimum = row["minimum"]
        group_id = row["delivery_group_id"]
        group_pressure.append(DeliveryGroupUtilizationPressure(
            delivery_group_id=group_id,
            section_counts=tuple(sorted(counts.items())),
            section_capacities=tuple(sorted(row["capacities"].items())),
            pairwise_penalty=row["pairwise_penalty"],
            total_penalty_share=(
                row["pairwise_penalty"] / total_penalty if total_penalty else 0.0
            ),
            overfull_section_ids=tuple(sorted(section_id for section_id, value in counts.items() if value == maximum)),
            underfull_section_ids=tuple(sorted(section_id for section_id, value in counts.items() if value == minimum)),
            movable_student_ids=tuple(sorted(group_students.get(group_id, set()))),
            relevant_student_ids=tuple(sorted(
                item.student_id
                for item in leverage_records
                if group_id in item.delivery_group_ids
            )),
        ))

    if fixed_student_ids:
        selected = tuple(sorted(set(fixed_student_ids)))[:target_scope_size]
        selection_reason = "fixed_scope"
    else:
        eligible = set(leverage_by_student)
        if policy == "top_individual":
            ordered_ids = [item.student_id for item in leverage_records]
            selection_reason = policy
        else:
            top_group = group_pressure[0] if group_pressure else None
            focused = [
                item for item in leverage_records
                if top_group and top_group.delivery_group_id in item.delivery_group_ids
            ]
            focused_ids = {item.student_id for item in focused}
            if policy == "delivery_group_focused":
                ordered_ids = [item.student_id for item in focused] + [
                    item.student_id for item in leverage_records
                    if item.student_id not in focused_ids
                ]
            else:
                selected_working = []
                remaining = set(eligible)
                while remaining and len(selected_working) < target_scope_size:
                    def interaction_key(student_id):
                        item = leverage_by_student[student_id]
                        overlap = sum(
                            bool(set(item.delivery_group_ids) & set(leverage_by_student[other].delivery_group_ids))
                            for other in selected_working
                        )
                        focused_bonus = int(
                            top_group is not None
                            and top_group.delivery_group_id in item.delivery_group_ids
                        )
                        return (
                            -focused_bonus,
                            -overlap,
                            *item.rank_key,
                        )
                    chosen = min(remaining, key=interaction_key)
                    selected_working.append(chosen)
                    remaining.remove(chosen)
                ordered_ids = selected_working + [
                    item.student_id for item in leverage_records
                    if item.student_id not in selected_working
                ]
            if policy == "mixed":
                ordered_ids = [item.student_id for item in leverage_records]
            selection_reason = policy
        selected = tuple(ordered_ids[:target_scope_size])

    selected = tuple(sorted(set(selected)))
    selected_facts = tuple(
        leverage_by_student[student_id]
        for student_id in selected
        if student_id in leverage_by_student
    )
    guidance_facts = {
        "policy": policy,
        "selection_reason": selection_reason,
        "target_scope_size": target_scope_size,
        "selected_student_ids": selected,
        "delivery_group_count": len(group_pressure),
        "total_pairwise_utilization_penalty": total_penalty,
        "top_delivery_groups": tuple({
            "delivery_group_id": item.delivery_group_id,
            "pairwise_penalty": item.pairwise_penalty,
            "total_penalty_share": item.total_penalty_share,
            "section_counts": item.section_counts,
            "section_capacities": item.section_capacities,
            "overfull_section_ids": item.overfull_section_ids,
            "underfull_section_ids": item.underfull_section_ids,
            "movable_student_count": len(item.movable_student_ids),
            # Keep the persisted diagnostic bounded.  The complete relevant
            # set remains available in the pure-engine pressure fact, while
            # session records only need a deterministic sample plus its count.
            "relevant_student_count": len(item.relevant_student_ids),
            "relevant_student_ids": item.relevant_student_ids[:20],
        } for item in group_pressure[:10]),
        "selected_leverage": tuple({
            "student_id": item.student_id,
            "total_positive_leverage": item.total_positive_leverage,
            "strongest_single_move": item.strongest_single_move,
            "relevant_group_count": item.relevant_group_count,
            "alternate_section_opportunity_count": item.alternate_section_opportunity_count,
            "delivery_group_ids": item.delivery_group_ids,
            "move_facts": item.move_facts[:10],
        } for item in selected_facts),
        "guidance_only": True,
        "objective_attribution": False,
    }
    return UtilizationClusterSelection(
        selected_student_ids=selected,
        policy=policy,
        target_scope_size=target_scope_size,
        pressure_facts=tuple(group_pressure),
        leverage_facts=tuple(leverage_records),
        guidance_facts=guidance_facts,
    )


def select_utilization_cluster_targets(
    data,
    quality_report,
    source_decisions,
    *,
    target_scope_size,
    policy="interaction_aware",
    fixed_student_ids=(),
):
    """Return a bounded utilization-driven target scope and evidence."""

    return build_utilization_cluster_guidance(
        data,
        quality_report,
        source_decisions,
        target_scope_size=target_scope_size,
        policy=policy,
        fixed_student_ids=fixed_student_ids,
    )


__all__ = [
    "DeliveryGroupUtilizationPressure",
    "StudentUtilizationLeverage",
    "UtilizationClusterSelection",
    "build_utilization_cluster_guidance",
    "select_utilization_cluster_targets",
]
