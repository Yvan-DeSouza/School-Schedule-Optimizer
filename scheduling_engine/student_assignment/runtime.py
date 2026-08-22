"""Small, Django-free runtime helpers for bounded diagnostic solves."""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import json
from time import monotonic


@dataclass(frozen=True)
class MonotonicDeadline:
    """A single wall-clock budget shared by nested diagnostic operations.

    CP-SAT receives a derived remaining allowance for each solve.  Keeping the
    deadline outside the solver prevents nested probes and validation solves
    from accidentally receiving the full experiment allowance independently.
    The class does not interrupt native solver calls; it makes their requested
    limits and the surrounding operation timing explicit.
    """

    requested_seconds: float
    started_at: float = field(default_factory=monotonic)

    @classmethod
    def start(cls, requested_seconds):
        return cls(max(0.0, float(requested_seconds)))

    @property
    def deadline_at(self):
        return self.started_at + self.requested_seconds

    def elapsed(self):
        return max(0.0, monotonic() - self.started_at)

    def remaining(self):
        return max(0.0, self.deadline_at - monotonic())

    def allowance(self, requested_seconds=None):
        """Return the smaller of a child allowance and global remaining time."""

        remaining = self.remaining()
        if requested_seconds is None:
            return remaining
        return max(0.0, min(remaining, float(requested_seconds)))


class OperationTimer:
    """Accumulate externally measured wall time by named operation."""

    def __init__(self):
        self._values = {}

    def measure(self, name):
        timer = self

        class _Measurement:
            def __enter__(self):
                self.started_at = monotonic()
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                timer._values[name] = timer._values.get(name, 0.0) + (
                    monotonic() - self.started_at
                )
                return False

        return _Measurement()

    def snapshot(self):
        return dict(sorted(self._values.items()))


def semantic_student_assignment_input_fingerprint(data):
    """Return a diagnostic fingerprint that omits opaque database identities.

    The detached student-assignment DTO does not carry catalog codes or
    natural student numbers.  This fingerprint therefore canonicalizes the
    identifier namespaces by sorted semantic occurrence and records the
    scheduling facts that those identifiers connect.  It is intended to
    compare equivalent fixture builds, not to replace the immutable run
    snapshot or to identify persisted records.
    """

    def rank(values):
        return {value: index for index, value in enumerate(sorted(set(values)))}

    student_rank = rank(
        [item.student_id for item in data.requests]
        + [item.student_id for item in data.fixed_enrollments]
        + [item.student_id for item in data.schedule_commitment_requests]
        + [item.student_id for item in data.fixed_schedule_commitments]
        + list(data.student_ids_with_alternate_requests)
    )
    course_values = (
        [item.course_id for item in data.requests]
        + [course_id for item in data.sections for course_id in item.member_course_ids]
        + [item.course_id for item in data.course_difficulties]
        + [item.course_id for item in data.fixed_enrollments]
        + [item.course_id for item in data.fixed_schedule_commitments if item.course_id is not None]
    )
    course_rank = rank(course_values)
    offering_rank = rank(
        [item.course_offering_id for item in data.requests]
        + [offering_id for item in data.sections for offering_id in item.member_course_offering_ids]
        + [item.course_offering_id for item in data.fixed_enrollments]
        + [item.course_offering_id for item in data.fixed_schedule_commitments if item.course_offering_id is not None]
    )
    teacher_rank = rank(
        [item.teacher_id for item in data.sections if item.teacher_id is not None]
        + [item.supervisor_id for item in data.online_supervision_sessions if item.supervisor_id is not None]
        + [item.teacher_id for item in data.student_assignment_locks if item.teacher_id is not None]
    )
    timeslot_rank = {
        item.id: index
        for index, item in enumerate(
            sorted(
                data.timeslots,
                key=lambda item: (item.semester, item.block, item.is_available, item.id),
            )
        )
    }
    section_records = [
        (
            tuple(sorted(offering_rank[value] for value in item.member_course_offering_ids)),
            tuple(sorted(course_rank[value] for value in item.member_course_ids)),
            item.semester,
            timeslot_rank.get(item.timeslot_id),
            item.capacity_max,
            item.target_capacity,
            item.half_semester_segment,
            item.half_semester_pair_key,
            teacher_rank.get(item.teacher_id),
            item.delivery_group_id,
            item.section_id,
        )
        for item in data.sections
    ]
    delivery_group_rank = rank(item[9] for item in section_records)
    section_rank = {
        item[10]: index
        for index, item in enumerate(
            sorted(
                section_records,
                key=lambda item: (
                    repr(item[:9]),
                    delivery_group_rank[item[9]],
                    item[10],
                ),
            )
        )
    }
    request_records = [
        (
            student_rank[item.student_id],
            course_rank[item.course_id],
            offering_rank[item.course_offering_id],
            item.is_primary,
            item.is_mandatory,
            item.priority_tier,
            item.assignment_basis,
            item.delivery_kind,
            item.duration,
            item.credit_value,
            item.half_semester_segment,
            course_rank.get(item.paired_half_course_id),
        )
        for item in data.requests
    ]
    request_rank = {
        request.request_id: index
        for index, (request, _record) in enumerate(
            sorted(
                zip(data.requests, request_records),
                key=lambda pair: (pair[1], pair[0].request_id),
            )
        )
    }
    commitment_rank = {
        item.request_id: index
        for index, item in enumerate(
            sorted(
                data.schedule_commitment_requests,
                key=lambda item: (
                    student_rank[item.student_id],
                    item.commitment_type,
                    item.is_in_scope,
                    item.request_id,
                ),
            )
        )
    }

    payload = {
        "request_records": sorted(request_records),
        "section_records": [
            (
                tuple(sorted(offering_rank[value] for value in item.member_course_offering_ids)),
                tuple(sorted(course_rank[value] for value in item.member_course_ids)),
                item.semester,
                timeslot_rank.get(item.timeslot_id),
                item.capacity_max,
                item.target_capacity,
                item.half_semester_segment,
                item.half_semester_pair_key,
                teacher_rank.get(item.teacher_id),
                delivery_group_rank[item.delivery_group_id],
            )
            for item in data.sections
        ],
        "timeslots": sorted(
            (item.semester, item.block, item.is_available)
            for item in data.timeslots
        ),
        "online_supervision_sessions": sorted(
            (
                item.semester,
                timeslot_rank.get(item.timeslot_id),
                item.capacity_max,
                item.target_capacity,
                teacher_rank.get(item.supervisor_id),
                item.is_in_scope,
            )
            for item in data.online_supervision_sessions
        ),
        "ordinary_locks": sorted(
            (
                item.lock_type,
                student_rank.get(item.student_id),
                section_rank.get(item.section_id),
                course_rank.get(item.course_id),
                teacher_rank.get(item.teacher_id),
                tuple(sorted(student_rank.get(student_id) for student_id in item.member_student_ids)),
                item.is_active,
            )
            for item in data.student_assignment_locks
        ),
        "fixed_enrollments": sorted(
            (
                student_rank[item.student_id],
                section_rank.get(item.section_id),
                offering_rank[item.course_offering_id],
                course_rank[item.course_id],
                item.semester,
                timeslot_rank.get(item.timeslot_id),
                item.is_active,
                item.is_locked,
                item.is_historical,
                item.is_in_scope,
                item.half_semester_segment,
                item.credit_value,
                item.delivery_kind,
            )
            for item in data.fixed_enrollments
        ),
        "commitment_requests": sorted(
            (
                student_rank[item.student_id],
                item.commitment_type,
                item.is_in_scope,
            )
            for item in data.schedule_commitment_requests
        ),
        "special_locks": sorted(
            (
                item.lock_type,
                item.lock_mode,
                commitment_rank.get(item.schedule_commitment_request_id),
                request_rank.get(item.course_request_id),
                timeslot_rank.get(item.timeslot_id),
                item.semester,
            )
            for item in data.special_commitment_locks
        ),
        "fixed_commitments": sorted(
            (
                student_rank[item.student_id],
                item.commitment_kind,
                commitment_rank.get(item.schedule_commitment_request_id),
                request_rank.get(item.course_request_id),
                tuple(sorted((timeslot_rank.get(slot), segment) for slot, segment in item.occupancy)),
                item.is_active,
                item.is_locked,
                item.is_historical,
                item.is_in_scope,
            )
            for item in data.fixed_schedule_commitments
        ),
        "configuration": (
            data.section_utilization_balance_importance,
            data.student_semester_balance_importance,
            data.course_sequence_preferences_importance,
            data.difficulty_balance_importance,
            data.course_category_diversity_importance,
            data.schedule_preservation_level,
        ),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return sha256(encoded.encode("utf-8")).hexdigest()
