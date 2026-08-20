"""Durable scheduling execution lifecycle and safe Celery dispatch.

The expensive stage services remain usable synchronously by management code and
tests. This module adds the application-facing asynchronous boundary without
moving solver logic into the queue layer or sending ORM objects through Redis.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from backend.apps.common.exceptions import DomainConflictError
from backend.apps.scheduling.codes import (
    SCHEDULING_EXECUTION_ENQUEUE_FAILED,
    SCHEDULING_EXECUTION_IDEMPOTENCY_CONFLICT,
    SCHEDULING_EXECUTION_WORKER_FAILED,
)
from backend.apps.scheduling.constants import (
    SCHEDULING_EXECUTION_OPERATION_SECTION_PLACEMENT,
    SCHEDULING_EXECUTION_OPERATION_STUDENT_ASSIGNMENT,
    SCHEDULING_EXECUTION_OPERATION_TEACHER_ASSIGNMENT,
    SCHEDULING_EXECUTION_STATUS_COMPLETED,
    SCHEDULING_EXECUTION_STATUS_FAILED,
    SCHEDULING_EXECUTION_STATUS_QUEUED,
    SCHEDULING_EXECUTION_STATUS_RUNNING,
)
from backend.apps.scheduling.models import SchedulingExecution


def _stable_fingerprint(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _id(value):
    return value.id if hasattr(value, "id") else value


def section_placement_payload(*, academic_year, input_mode, budget_approval=None):
    """Return only stable identifiers needed to reconstruct placement input."""

    return {
        "academic_year_id": int(_id(academic_year)),
        "input_mode": input_mode,
        "budget_approval_id": int(_id(budget_approval)) if budget_approval else None,
    }


def teacher_assignment_payload(*, academic_year):
    """Return only the target-year identifier for named staffing."""

    return {"academic_year_id": int(_id(academic_year))}


def student_assignment_payload(**values):
    """Normalize student-run request values into a JSON-safe stable payload."""

    payload = dict(values)
    academic_year = payload.pop("academic_year", payload.pop("academic_year_id", None))
    payload["academic_year_id"] = int(_id(academic_year))
    for key in ("provisional_teacher_assignment_run", "source_approval"):
        value = payload.pop(key, payload.get(key + "_id"))
        payload[key + "_id"] = int(_id(value)) if value else None
    payload["scope_student_ids"] = [int(value) for value in payload.get("scope_student_ids", ())]
    payload["scope_course_ids"] = [int(value) for value in payload.get("scope_course_ids", ())]
    payload["scope_section_ids"] = [int(value) for value in payload.get("scope_section_ids", ())]
    payload["priority_request_ids"] = [int(value) for value in payload.get("priority_request_ids", ())]
    if payload.get("selected_lock_ids") is not None:
        payload["selected_lock_ids"] = [int(value) for value in payload["selected_lock_ids"]]
    return payload


def _dispatch_after_commit(execution_id):
    """Publish after commit, recording broker failures durably."""

    from backend.apps.scheduling.tasks import execute_scheduling_execution

    try:
        result = execute_scheduling_execution.apply_async(
            args=[str(execution_id)],
            queue=getattr(settings, "CELERY_TASK_DEFAULT_QUEUE", "scheduling"),
        )
    except Exception as error:  # pragma: no cover - broker outages need integration coverage
        SchedulingExecution.objects.filter(
            pk=execution_id, status=SCHEDULING_EXECUTION_STATUS_QUEUED,
        ).update(
            status=SCHEDULING_EXECUTION_STATUS_FAILED,
            error_code=SCHEDULING_EXECUTION_ENQUEUE_FAILED,
            error_detail={"detail": "The scheduling worker could not be reached."},
            finished_at=timezone.now(),
        )
        return
    SchedulingExecution.objects.filter(pk=execution_id).update(celery_task_id=result.id)


@transaction.atomic
def submit_scheduling_execution(*, operation, payload, created_by, idempotency_key=""):
    """Persist a queued execution and enqueue it only after commit.

    An optional client idempotency key makes a repeated submission return the
    original execution when its request payload is identical. A changed
    payload under the same key is a conflict, not a second solve.
    """

    key = (idempotency_key or "").strip()
    fingerprint = _stable_fingerprint(payload)
    if key:
        existing = SchedulingExecution.objects.filter(
            created_by=created_by,
            operation=operation,
            idempotency_key=key,
        ).first()
        if existing:
            if existing.payload_fingerprint != fingerprint:
                raise DomainConflictError({
                    "code": SCHEDULING_EXECUTION_IDEMPOTENCY_CONFLICT,
                    "detail": "This idempotency key was already used for a different scheduling request.",
                })
            return existing, False
    execution = SchedulingExecution.objects.create(
        operation=operation,
        status=SCHEDULING_EXECUTION_STATUS_QUEUED,
        payload=payload,
        created_by=created_by,
        idempotency_key=key,
        payload_fingerprint=fingerprint,
    )
    transaction.on_commit(lambda: _dispatch_after_commit(execution.pk))
    return execution, True


def mark_execution_running(execution_id, *, task_id=""):
    """Claim a queued execution exactly once before invoking a solver service."""

    now = timezone.now()
    with transaction.atomic():
        execution = SchedulingExecution.objects.select_for_update().get(pk=execution_id)
        if execution.status != SCHEDULING_EXECUTION_STATUS_QUEUED:
            return None
        execution.status = SCHEDULING_EXECUTION_STATUS_RUNNING
        execution.started_at = now
        if task_id:
            execution.celery_task_id = task_id
        execution.save(update_fields=["status", "started_at", "celery_task_id"])
        return execution


def mark_execution_completed(execution_id, *, result_model, result_id):
    SchedulingExecution.objects.filter(pk=execution_id).update(
        status=SCHEDULING_EXECUTION_STATUS_COMPLETED,
        result_model=result_model,
        result_id=result_id,
        finished_at=timezone.now(),
    )


def mark_execution_failed(execution_id, *, error_code=SCHEDULING_EXECUTION_WORKER_FAILED, detail=None):
    SchedulingExecution.objects.filter(pk=execution_id).update(
        status=SCHEDULING_EXECUTION_STATUS_FAILED,
        error_code=error_code,
        error_detail=detail or {"detail": "The scheduling worker could not complete the operation."},
        finished_at=timezone.now(),
    )


def execution_result_status(execution):
    """Expose solver status separately from delivery status."""

    if not execution.result_model or execution.result_id is None:
        return None
    from backend.apps.scheduling.models import (
        SectionPlacementRun,
        StudentAssignmentRun,
        TeacherAssignmentRun,
    )

    models = {
        "SectionPlacementRun": SectionPlacementRun,
        "TeacherAssignmentRun": TeacherAssignmentRun,
        "StudentAssignmentRun": StudentAssignmentRun,
    }
    model = models.get(execution.result_model)
    if model is None:
        return None
    result = model.objects.filter(pk=execution.result_id).values("status").first()
    return result["status"] if result else None
