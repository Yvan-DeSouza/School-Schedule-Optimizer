"""Celery entrypoints for expensive scheduling solves.

Tasks receive one durable execution ID rather than a DTO graph or ORM object.
The worker reloads the exact persisted payload and then calls the existing
review-first service, so approval remains a separate counselor operation.
"""

from celery import shared_task

from backend.apps.common.exceptions import DomainError
from backend.apps.scheduling.models import (
    SectionBudgetApproval,
    SchedulingExecution,
    StudentAssignmentApproval,
    TeacherAssignmentRun,
)
from backend.apps.scheduling.codes import (
    SCHEDULING_EXECUTION_DOMAIN_FAILED,
    SCHEDULING_EXECUTION_WORKER_FAILED,
)
from backend.apps.scheduling.constants import (
    SCHEDULING_EXECUTION_OPERATION_SECTION_PLACEMENT,
    SCHEDULING_EXECUTION_OPERATION_STUDENT_ASSIGNMENT,
    SCHEDULING_EXECUTION_OPERATION_TEACHER_ASSIGNMENT,
    STUDENT_ASSIGNMENT_DEFAULT_MAX_PRIORITY_REQUESTS,
)
from backend.apps.scheduling.services.execution import (
    mark_execution_completed,
    mark_execution_failed,
    mark_execution_running,
)


def _failure_detail(error):
    detail = getattr(error, "detail", None)
    if isinstance(detail, dict):
        return detail
    return {"detail": "The scheduling operation failed during worker execution."}


@shared_task(
    bind=True,
    name="backend.apps.scheduling.tasks.execute_scheduling_execution",
    ignore_result=True,
    acks_late=False,
    autoretry_for=(),
)
def execute_scheduling_execution(self, execution_id):
    """Execute one stage and persist delivery state without automatic retry."""

    execution = mark_execution_running(execution_id, task_id=getattr(self.request, "id", ""))
    if execution is None:
        # A duplicate delivery cannot run an already-started or completed
        # operation a second time. The durable execution row is authoritative.
        return
    try:
        payload = execution.payload
        if execution.operation == SCHEDULING_EXECUTION_OPERATION_SECTION_PLACEMENT:
            from backend.apps.scheduling.services.section_placement import create_section_placement_run

            run = create_section_placement_run(
                academic_year_id=payload["academic_year_id"],
                input_mode=payload["input_mode"],
                budget_approval=(
                    SectionBudgetApproval.objects.get(pk=payload["budget_approval_id"])
                    if payload.get("budget_approval_id") is not None else None
                ),
                created_by=execution.created_by,
            )
            result_model = "SectionPlacementRun"
        elif execution.operation == SCHEDULING_EXECUTION_OPERATION_TEACHER_ASSIGNMENT:
            from backend.apps.scheduling.services.teacher_assignment import create_teacher_assignment_run

            run = create_teacher_assignment_run(
                academic_year_id=payload["academic_year_id"],
                created_by=execution.created_by,
            )
            result_model = "TeacherAssignmentRun"
        elif execution.operation == SCHEDULING_EXECUTION_OPERATION_STUDENT_ASSIGNMENT:
            from backend.apps.scheduling.services.student_assignment import create_student_assignment_run

            run = create_student_assignment_run(
                academic_year=payload["academic_year_id"],
                staffing_mode=payload["staffing_mode"],
                provisional_teacher_assignment_run=(
                    TeacherAssignmentRun.objects.get(pk=payload["provisional_teacher_assignment_run_id"])
                    if payload.get("provisional_teacher_assignment_run_id") is not None else None
                ),
                soft_constraint_importance=payload["soft_constraint_importance"],
                created_by=execution.created_by,
                scope_type=payload.get("scope_type", "full"),
                source_approval=(
                    StudentAssignmentApproval.objects.get(pk=payload["source_approval_id"])
                    if payload.get("source_approval_id") is not None else None
                ),
                scope_student_ids=payload.get("scope_student_ids", ()),
                scope_course_ids=payload.get("scope_course_ids", ()),
                scope_section_ids=payload.get("scope_section_ids", ()),
                priority_request_ids=payload.get("priority_request_ids", ()),
                priority_request_limit=payload.get(
                    "priority_request_limit",
                    STUDENT_ASSIGNMENT_DEFAULT_MAX_PRIORITY_REQUESTS,
                ),
                schedule_preservation_level=payload.get("schedule_preservation_level", "none"),
                selected_lock_ids=payload.get("selected_lock_ids"),
            )
            result_model = "StudentAssignmentRun"
        else:
            raise ValueError("Unknown scheduling execution operation.")
    except Exception as error:
        mark_execution_failed(
            execution_id,
            error_code=SCHEDULING_EXECUTION_DOMAIN_FAILED if isinstance(error, DomainError)
            else SCHEDULING_EXECUTION_WORKER_FAILED,
            detail=_failure_detail(error),
        )
        raise
    mark_execution_completed(execution_id, result_model=result_model, result_id=run.pk)
    return str(run.pk)
