"""Deterministic, solver-neutral guidance for targeted repair diagnostics.

The functions in this module rank students from already-evaluated quality
facts.  They do not inspect CP-SAT variables, choose assignments, or authorize
changes.  A later diagnostic operator may use the ranking to select a bounded
student neighborhood, but CP-SAT plus the unchanged full-model validator
remains the only authority for a candidate.
"""

from __future__ import annotations

from dataclasses import dataclass

from .objective_semantics import normalize_penalty, resolve_importance_scores


_STUDENT_LOCAL_COMPONENTS = (
    ("student_semester_load_balance", "student_semester_balance"),
    ("difficulty_balance", "difficulty_balance"),
    ("course_category_diversity", "course_category_diversity"),
    ("course_sequence_preferences", "course_sequence_preferences"),
)


@dataclass(frozen=True)
class StudentTargetPressure:
    """One deterministic student-local repair-pressure record.

    ``weighted_current_penalty`` is an exact attribution of the currently
    observed student-local v2 penalties using the same normalized component
    values and canonical counselor scores as the aggregate objective.  It is
    not a claim that the student can improve by that amount.

    ``opportunity_signal`` is intentionally cheap and conservative: it counts
    non-zero local penalty components plus unsatisfied applicable sequence
    opportunities.  It helps break ties between equally pressured students,
    but it is not a solver result or a mobility guarantee.
    """

    student_id: int
    weighted_current_penalty: int
    opportunity_signal: int
    nonzero_component_count: int
    component_penalties: tuple[tuple[str, int], ...]
    component_weighted_penalties: tuple[tuple[str, int], ...]
    sequence_opportunity_count: int
    sequence_unsatisfied_count: int
    rank: int = 0

    @property
    def rank_key(self):
        return (
            -self.weighted_current_penalty,
            -self.opportunity_signal,
            -self.nonzero_component_count,
            self.student_id,
        )


def _student_ids(data, quality_report):
    ids = {
        request.student_id
        for request in data.requests
    }
    ids.update(row.student_id for row in data.fixed_enrollments)
    ids.update(row.student_id for row in data.schedule_commitment_requests)
    ids.update(row.student_id for row in data.fixed_schedule_commitments)
    for metric_name in (
        "student_semester_load_balance",
        "difficulty_balance",
        "course_category_diversity",
    ):
        ids.update(
            int(student_id)
            for student_id in quality_report.get(metric_name, {}).get(
                "entities", {}
            )
        )
    return tuple(sorted(ids))


def _metric_entities(quality_report, metric_name):
    return quality_report.get(metric_name, {}).get("entities", {})


def _sequence_student_penalties(quality_report):
    entities = _metric_entities(quality_report, "course_sequence_preferences")
    opportunity_counts = {}
    unsatisfied_counts = {}
    for key, satisfied in entities.items():
        student_id = int(str(key).split(":", 1)[0])
        opportunity_counts[student_id] = opportunity_counts.get(student_id, 0) + 1
        if not satisfied:
            unsatisfied_counts[student_id] = unsatisfied_counts.get(student_id, 0) + 1
    return opportunity_counts, unsatisfied_counts


def rank_students_by_quality_pressure(
    data,
    quality_report,
    *,
    limit: int | None = None,
):
    """Rank students for a later targeted repair experiment.

    Section utilization is deliberately excluded because it is a global
    pairwise section-load metric and cannot be truthfully allocated to one
    student without changing its definition.  The returned records use only
    quality facts already produced by the evaluator; no solve or candidate
    simulation occurs.
    """

    semantics = quality_report.get("objective_semantics", {})
    components = semantics.get("components", {})
    labels = {
        "section_utilization_balance": data.section_utilization_balance_importance,
        "student_semester_balance": data.student_semester_balance_importance,
        "course_sequence_preferences": data.course_sequence_preferences_importance,
        "difficulty_balance": data.difficulty_balance_importance,
        "course_category_diversity": data.course_category_diversity_importance,
    }
    scores = resolve_importance_scores(
        labels=labels,
        scores=(
            data.objective_importance_scores
            if data.objective_semantics_version == "v2"
            else None
        ),
    )
    sequence_opportunities, sequence_unsatisfied = _sequence_student_penalties(
        quality_report
    )
    records = []
    for student_id in _student_ids(data, quality_report):
        raw_penalties = {}
        weighted_penalties = {}
        for metric_name, score_key in _STUDENT_LOCAL_COMPONENTS:
            entities = _metric_entities(quality_report, metric_name)
            entity = entities.get(str(student_id), 0)
            if isinstance(entity, dict):
                if metric_name == "student_semester_load_balance":
                    raw = int(entity.get("absolute_difference", 0))
                elif metric_name == "difficulty_balance":
                    raw = int(entity.get("absolute_difference", 0))
                elif metric_name == "course_category_diversity":
                    raw = int(entity.get("penalty", entity.get("value", 0)))
                else:
                    raw = 0
            else:
                # Sequence entities are keyed by student/course/course, not
                # by student.  Its per-student raw penalty is reconstructed
                # from the exact satisfied flags above.
                raw = sequence_unsatisfied.get(student_id, 0) if metric_name == "course_sequence_preferences" else 0
            if metric_name == "course_sequence_preferences":
                raw = sequence_unsatisfied.get(student_id, 0)
            raw_penalties[metric_name] = raw
            component = components.get(metric_name, {})
            denominator = component.get("denominator")
            normalized = normalize_penalty(raw, denominator or 0)
            weighted_penalties[metric_name] = int(normalized * scores.get(score_key, 0))

        nonzero = sum(value > 0 for value in raw_penalties.values())
        opportunity_signal = nonzero + sequence_unsatisfied.get(student_id, 0)
        records.append(
            StudentTargetPressure(
                student_id=student_id,
                weighted_current_penalty=sum(weighted_penalties.values()),
                opportunity_signal=opportunity_signal,
                nonzero_component_count=nonzero,
                component_penalties=tuple(sorted(raw_penalties.items())),
                component_weighted_penalties=tuple(sorted(weighted_penalties.items())),
                sequence_opportunity_count=sequence_opportunities.get(student_id, 0),
                sequence_unsatisfied_count=sequence_unsatisfied.get(student_id, 0),
            )
        )
    ordered = sorted(records, key=lambda item: item.rank_key)
    return tuple(
        StudentTargetPressure(**{**record.__dict__, "rank": index})
        for index, record in enumerate(ordered, start=1)
        if limit is None or index <= max(0, int(limit))
    )


def reconcile_student_quality_pressure(quality_report, ranked_students):
    """Report local weighted totals against aggregate v2 facts.

    The reconciliation intentionally excludes section utilization because it
    is global.  Per-student normalization is rounded independently, so a
    small integer delta from aggregate normalized values is expected and is
    exposed rather than hidden.
    """

    components = quality_report.get("objective_semantics", {}).get(
        "components", {}
    )
    local_component_names = {
        "student_semester_load_balance",
        "difficulty_balance",
        "course_category_diversity",
        "course_sequence_preferences",
    }
    aggregate_weighted = sum(
        int(facts.get("weighted_normalized_contribution", 0) or 0)
        for name, facts in components.items()
        if name in local_component_names
    )
    student_weighted = sum(
        int(item.weighted_current_penalty) for item in ranked_students
    )
    return {
        "student_local_weighted_total": student_weighted,
        "aggregate_student_local_weighted_total": aggregate_weighted,
        "rounding_delta": aggregate_weighted - student_weighted,
        "excluded_components": ("section_utilization_balance",),
    }


__all__ = [
    "StudentTargetPressure",
    "rank_students_by_quality_pressure",
    "reconcile_student_quality_pressure",
]
