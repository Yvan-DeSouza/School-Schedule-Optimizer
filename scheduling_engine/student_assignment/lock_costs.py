"""Bounded per-lock counterfactual evidence for student-assignment review."""

from __future__ import annotations

from dataclasses import replace

from ..dto import StudentAssignmentLockCostDTO
from .locks import active_locks


def build_lock_costs(data, result, *, solve_without_lock_costs):
    """Measure each lock's cost with an internal deterministic relaxation.

    This is result evidence, not the counselor-facing what-if workflow. Each
    comparison removes exactly one active lock from the same immutable input.
    A request counts only when it is unresolved with that lock and becomes
    assigned without it, so overlapping locks are never presented as a claim
    that their individual counts sum to the total unmet demand.
    """

    base_unmet_request_ids = {item.request_id for item in result.unmet_requests}
    costs = []
    for lock in active_locks(data):
        relaxed_data = replace(
            data,
            student_assignment_locks=tuple(
                item for item in data.student_assignment_locks if item.lock_id != lock.lock_id
            ),
            # Lock-cost evidence is bounded independently of the main run so a
            # large number of locks cannot turn a review request into an
            # unbounded sequence of counterfactual solves.
            time_limit_seconds=min(data.time_limit_seconds, 5.0),
        )
        # Lock-cost comparison is an internal counterfactual measurement, not
        # a counselor-visible immutable run. Avoid materializing another full
        # candidate ledger for every one-lock relaxation.
        relaxed_result = solve_without_lock_costs(relaxed_data)
        newly_assigned = {
            item.request_id for item in relaxed_result.assignments
        } & base_unmet_request_ids
        costs.append(StudentAssignmentLockCostDTO(
            lock_id=lock.lock_id,
            attributable_request_count=len(newly_assigned),
            unresolved_request_ids=tuple(sorted(newly_assigned)),
        ))
    return tuple(costs)
