"""Focused tests for durable asynchronous scheduling execution behavior."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from backend.apps.common.exceptions import DomainError
from backend.apps.scheduling.constants import (
    SCHEDULING_EXECUTION_OPERATION_SECTION_PLACEMENT,
    SCHEDULING_EXECUTION_STATUS_COMPLETED,
    SCHEDULING_EXECUTION_STATUS_FAILED,
)
from backend.apps.scheduling.models import SchedulingExecution
from backend.apps.scheduling.services.execution import (
    section_placement_payload,
    submit_scheduling_execution,
)
from backend.apps.scheduling.codes import SCHEDULING_EXECUTION_IDEMPOTENCY_CONFLICT
from backend.apps.scheduling.tasks import execute_scheduling_execution


@pytest.mark.django_db
def test_submission_persists_before_enqueue_and_sends_only_execution_id(counselor_user, academic_year):
    payload = section_placement_payload(
        academic_year=academic_year,
        input_mode="fixed_semester",
    )
    sent = {}

    def fake_apply_async(*, args, queue):
        sent.update(args=args, queue=queue)
        return SimpleNamespace(id="celery-task-1")

    with patch(
        "backend.apps.scheduling.tasks.execute_scheduling_execution.apply_async",
        side_effect=fake_apply_async,
    ):
        with patch("backend.apps.scheduling.services.execution.transaction.on_commit", side_effect=lambda callback: callback()):
            execution, created = submit_scheduling_execution(
                operation=SCHEDULING_EXECUTION_OPERATION_SECTION_PLACEMENT,
                payload=payload,
                created_by=counselor_user,
            )

    assert created is True
    assert execution.status == "queued"
    assert sent == {"args": [str(execution.id)], "queue": "scheduling"}
    execution.refresh_from_db()
    assert execution.celery_task_id == "celery-task-1"
    assert execution.payload == payload


def test_celery_discovers_the_scheduling_task_after_django_is_ready():
    from config.celery import app

    app.autodiscover_tasks(force=True)

    assert "backend.apps.scheduling.tasks.execute_scheduling_execution" in app.tasks


@pytest.mark.django_db
def test_idempotency_returns_same_execution_and_rejects_changed_payload(counselor_user, academic_year):
    payload = section_placement_payload(academic_year=academic_year, input_mode="fixed_semester")
    with patch(
        "backend.apps.scheduling.tasks.execute_scheduling_execution.apply_async",
        return_value=SimpleNamespace(id="celery-task-repeat"),
    ):
        with patch("backend.apps.scheduling.services.execution.transaction.on_commit", side_effect=lambda callback: callback()):
            first, created = submit_scheduling_execution(
                operation=SCHEDULING_EXECUTION_OPERATION_SECTION_PLACEMENT,
                payload=payload,
                created_by=counselor_user,
                idempotency_key="repeat-1",
            )
    assert created is True
    second, created = submit_scheduling_execution(
        operation=SCHEDULING_EXECUTION_OPERATION_SECTION_PLACEMENT,
        payload=payload,
        created_by=counselor_user,
        idempotency_key="repeat-1",
    )
    assert created is False
    assert second.pk == first.pk

    with pytest.raises(DomainError) as error:
        submit_scheduling_execution(
            operation=SCHEDULING_EXECUTION_OPERATION_SECTION_PLACEMENT,
            payload={**payload, "input_mode": "annual_total"},
            created_by=counselor_user,
            idempotency_key="repeat-1",
        )
    assert error.value.detail["code"] == SCHEDULING_EXECUTION_IDEMPOTENCY_CONFLICT


@pytest.mark.django_db
def test_worker_completion_keeps_solver_status_separate_from_execution_status(counselor_user, academic_year):
    execution = SchedulingExecution.objects.create(
        operation=SCHEDULING_EXECUTION_OPERATION_SECTION_PLACEMENT,
        payload={"academic_year_id": academic_year.id, "input_mode": "fixed_semester"},
        created_by=counselor_user,
    )
    fake_run = SimpleNamespace(pk=42, status="infeasible")
    with patch(
        "backend.apps.scheduling.services.section_placement.create_section_placement_run",
        return_value=fake_run,
    ):
        execute_scheduling_execution.run(str(execution.pk))

    execution.refresh_from_db()
    assert execution.status == SCHEDULING_EXECUTION_STATUS_COMPLETED
    assert execution.result_model == "SectionPlacementRun"
    assert execution.result_id == 42


@pytest.mark.django_db
def test_worker_failure_is_persisted_and_is_not_retried(counselor_user, academic_year):
    execution = SchedulingExecution.objects.create(
        operation=SCHEDULING_EXECUTION_OPERATION_SECTION_PLACEMENT,
        payload={"academic_year_id": academic_year.id, "input_mode": "fixed_semester"},
        created_by=counselor_user,
    )
    with patch(
        "backend.apps.scheduling.services.section_placement.create_section_placement_run",
        side_effect=RuntimeError("boom"),
    ), pytest.raises(RuntimeError):
        execute_scheduling_execution.run(str(execution.pk))

    execution.refresh_from_db()
    assert execution.status == SCHEDULING_EXECUTION_STATUS_FAILED
    assert execution.result_id is None


@pytest.mark.django_db
def test_api_returns_queued_execution_and_exposes_read_only_status(
    authenticated_client, counselor_user, staff_user, student_user, academic_year,
):
    response = authenticated_client(counselor_user).post(
        "/api/planning/section-placement-runs/",
        {"academic_year": academic_year.id, "input_mode": "fixed_semester"},
        format="json",
        HTTP_IDEMPOTENCY_KEY="api-repeat-1",
    )

    assert response.status_code == 202
    execution_id = response.data["id"]
    assert response.data["status"] == "queued"
    repeated = authenticated_client(counselor_user).post(
        "/api/planning/section-placement-runs/",
        {"academic_year": academic_year.id, "input_mode": "fixed_semester"},
        format="json",
        HTTP_IDEMPOTENCY_KEY="api-repeat-1",
    )
    assert repeated.status_code == 200
    assert repeated.data["id"] == execution_id
    status_response = authenticated_client(staff_user).get(
        f"/api/planning/executions/{execution_id}/"
    )
    assert status_response.status_code == 200
    assert status_response.data["operation"] == "section_placement"
    assert status_response.data["solver_status"] is None
    assert authenticated_client(student_user).get(
        f"/api/planning/executions/{execution_id}/"
    ).status_code == 403
