"""Persistence orchestration for immutable planning runs.

This module deliberately never imports scheduling_engine; engine_adapter is the
single Django-to-engine boundary.
"""

from backend.apps.common.constants import (
    SECTION_PLANNING_RUN_STATUS_COMPLETE,
    SECTION_PLANNING_RUN_STATUS_INFEASIBLE,
)
from backend.apps.scheduling.models import SectionPlanningRun
from backend.apps.scheduling.services.engine_adapter import (
    get_section_count_plan_with_snapshot,
)


def create_section_planning_run(*, academic_year_id, created_by, course_constraints, teacher_capacity_adjustments):
    scenario = {
        "course_constraints": list(course_constraints),
        "teacher_capacity_adjustments": list(teacher_capacity_adjustments),
    }
    result, snapshot = get_section_count_plan_with_snapshot(
        academic_year_id,
        course_constraints=course_constraints,
        teacher_capacity_adjustments=teacher_capacity_adjustments,
    )
    status = (
        SECTION_PLANNING_RUN_STATUS_COMPLETE
        if result["status"] == "complete"
        else SECTION_PLANNING_RUN_STATUS_INFEASIBLE
    )
    return SectionPlanningRun.objects.create(
        academic_year_id=academic_year_id,
        created_by=created_by,
        status=status,
        scenario_constraints=scenario,
        input_snapshot=snapshot,
        result=result,
        solver_metadata={"engine": "ortools-cp-sat", "objective": "lexicographic"},
    )
