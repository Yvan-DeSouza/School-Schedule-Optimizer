"""Replay the real r5 placement through fresh production ORM materialization."""

from dataclasses import asdict
import json
from pathlib import Path

import pytest

from backend.apps.constraints.conflict_matrix import create_course_conflict_matrix
from backend.apps.courses.services.offerings import ensure_academic_year_offerings
from backend.apps.scheduling.models import (
    OnlineSupervisionSession,
    SectionPlacementApprovalAssignment,
    SectionPlacementRun,
    SectionSchedule,
)
from backend.apps.scheduling.services.engine_adapter import (
    load_section_placement_input,
    placement_input_fingerprint,
)
from backend.apps.scheduling.services.online_supervision import (
    approve_online_supervision_plan_run,
    create_online_supervision_plan_run,
)
from backend.apps.scheduling.services.section_budget_planning import (
    approve_section_budget_run,
    create_section_budget_run,
)
from backend.apps.scheduling.services.section_placement import approve_section_placement_run
from backend.apps.scheduling.services.staffing_planning import create_staffing_plan_run
from scheduling_engine.dto import PlacementAssignmentDTO, PlacementResultDTO
from scheduling_engine.placement_research_artifacts import (
    load_frozen_placement_input,
    load_frozen_placement_result,
    placement_input_semantic_fingerprint,
    placement_pair_summary,
    placement_result_semantic_fingerprint,
    rebind_placement_result,
)

from backend.tests.paul_desmarais_production_pipeline_qualification import (
    EXPECTED_SOURCE_FINGERPRINT,
    _assert_source_boundary,
    _materialize_source,
)
from scheduling_engine.paul_desmarais_production_pipeline_source import build_source_spec


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
R5_ROOT = (
    REPOSITORY_ROOT
    / "scheduling_engine/benchmarks/production_pipeline"
    / "paul_desmarais_shaped_g9_12_production_pipeline_v1"
    / "attempts/r5_lossless_checkpoint"
)


def _materialized_result(*, data, frozen_result, academic_year, approval):
    section_rows = {
        (row.section.delivery_group_id, row.annual_index): (
            row.section,
            row.timeslot,
        )
        for row in SectionPlacementApprovalAssignment.objects.filter(
            approval=approval,
        ).select_related("section", "timeslot")
    }
    online_rows = {
        session.id: session.timeslot
        for session in OnlineSupervisionSession.objects.filter(
            academic_year=academic_year,
            placement_approval=approval,
        )
    }
    assignments = []
    for item in frozen_result.assignments:
        if item.online_supervision_session_id is not None:
            slot = online_rows[item.online_supervision_session_id]
            assignments.append(PlacementAssignmentDTO(
                unit_key=item.unit_key,
                section_id=None,
                delivery_group_id=item.delivery_group_id,
                semester=slot.semester,
                timeslot_id=slot.id,
                block=slot.block,
                annual_index=item.annual_index,
                online_supervision_session_id=item.online_supervision_session_id,
            ))
            continue
        section, slot = section_rows[item.delivery_group_id, item.annual_index]
        assignments.append(PlacementAssignmentDTO(
            unit_key=item.unit_key,
            section_id=section.id,
            delivery_group_id=section.delivery_group_id,
            semester=section.semester,
            timeslot_id=slot.id,
            block=slot.block,
            annual_index=item.annual_index,
            online_supervision_session_id=None,
        ))
    return PlacementResultDTO(
        status=frozen_result.status,
        solver_outcome=frozen_result.solver_outcome,
        assignments=tuple(assignments),
        unplaced_unit_keys=(),
        diagnostics=frozen_result.diagnostics,
        objective_components=dict(frozen_result.objective_components),
        staffing_summary=dict(frozen_result.staffing_summary),
    )


@pytest.mark.django_db(transaction=True)
def test_r5_frozen_placement_replays_without_placement_cp_sat(counselor_user):
    frozen_input = load_frozen_placement_input(
        R5_ROOT / "frozen_section_placement_input.json",
    )
    frozen_result = load_frozen_placement_result(
        R5_ROOT / "frozen_section_placement_result.json",
        data=frozen_input["data"],
        expected_input_fingerprint=frozen_input["semantic_fingerprint"],
    )
    spec = build_source_spec()
    assert spec.fingerprint == EXPECTED_SOURCE_FINGERPRINT
    context = _materialize_source(spec, counselor_user)
    academic_year = context["academic_year"]
    assert len(context["students"]) == 1400
    _assert_source_boundary(academic_year)
    ensure_academic_year_offerings(academic_year, actor=counselor_user)

    online_run = create_online_supervision_plan_run(
        academic_year=academic_year,
        created_by=counselor_user,
    )
    assert online_run.status == "complete"
    approve_online_supervision_plan_run(
        online_run,
        approved_by=counselor_user,
        reason="Approve replay-only online supervision source.",
    )
    budget_run = create_section_budget_run(
        academic_year=academic_year,
        created_by=counselor_user,
        budget_type="ceiling",
        section_budget=400,
        backup_policy="ignore",
        backup_overrides=(),
        offering_constraints=(),
    )
    assert budget_run.status == "complete"
    budget_approval = approve_section_budget_run(
        budget_run,
        approved_by=counselor_user,
        reason="Approve replay-only annual section counts.",
    )
    staffing_constraints = [
        {
            "offering_id": item.delivery_group_id,
            "exact_sections": item.approved_annual_count,
            "semester_1_count": item.approved_semester_1_count,
            "semester_2_count": item.approved_semester_2_count,
        }
        for item in budget_approval.offering_approvals.all()
    ]
    staffing_run = create_staffing_plan_run(
        academic_year=academic_year,
        created_by=counselor_user,
        budget_approval=budget_approval,
        backup_policy="ignore",
        backup_overrides=(),
        offering_constraints=staffing_constraints,
        teacher_capacity_adjustments=(),
    )
    assert staffing_run.status == "complete"
    matrix = create_course_conflict_matrix(
        academic_year=academic_year,
        initialization_mode="fresh_current_demand",
        actor=counselor_user,
    )
    target_data, target_matrix, target_roster = load_section_placement_input(
        academic_year_id=academic_year.id,
        input_mode="annual_total",
        budget_approval=budget_approval,
        conflict_matrix=matrix,
    )
    assert target_matrix.id == matrix.id
    assert placement_input_semantic_fingerprint(target_data) == frozen_input["semantic_fingerprint"]
    rebound = rebind_placement_result(
        frozen_input["data"], frozen_result["result"], target_data,
    )
    assert placement_result_semantic_fingerprint(
        target_data, rebound,
    ) == frozen_result["semantic_fingerprint"]
    pair_summary = placement_pair_summary(target_data, rebound)
    assert pair_summary["pair_count"] == 10
    assert pair_summary["co_timed_pair_count"] == 10
    assert pair_summary["split_pair_count"] == 0
    assert pair_summary["missing_pair_count"] == 0
    assert rebound.staffing_summary["witness_proven"] is True

    snapshot = asdict(target_data)
    snapshot["fingerprint"] = placement_input_fingerprint(snapshot)
    snapshot["matrix_revision"] = target_matrix.revision
    snapshot["roster_id"] = target_roster.id
    replay_run = SectionPlacementRun.objects.create(
        academic_year=academic_year,
        input_mode="annual_total",
        budget_approval=budget_approval,
        conflict_matrix=target_matrix,
        teacher_roster=target_roster,
        created_by=counselor_user,
        status="complete",
        input_snapshot=snapshot,
        result=asdict(rebound),
        solver_metadata={
            "engine": "frozen-research-replay",
            "placement_cp_sat_rerun": False,
            "source_attempt": "post_half_pair_placement_fix_20260912_r5_lossless_placement_checkpoint",
        },
    )
    approval = approve_section_placement_run(
        replay_run,
        approved_by=counselor_user,
        reason="Approve fresh ORM materialization of frozen placement.",
    )
    materialized = _materialized_result(
        data=target_data,
        frozen_result=rebound,
        academic_year=academic_year,
        approval=approval,
    )
    replay_match = (
        placement_result_semantic_fingerprint(target_data, materialized)
        == frozen_result["semantic_fingerprint"]
    )
    assert replay_match is True
    assert OnlineSupervisionSession.objects.filter(
        academic_year=academic_year,
        placement_approval=approval,
        timeslot__isnull=False,
    ).count() == 6
    assert SectionSchedule.objects.filter(
        section__academic_year=academic_year,
        placement_approval_assignment__approval=approval,
    ).count() == 320

    report = {
        "schema": "section_placement_fresh_orm_replay_report_v1",
        "source_attempt": "post_half_pair_placement_fix_20260912_r5_lossless_placement_checkpoint",
        "source_fingerprint": spec.fingerprint,
        "placement_cp_sat_rerun": False,
        "input_semantic_fingerprint": frozen_input["semantic_fingerprint"],
        "result_semantic_fingerprint": frozen_result["semantic_fingerprint"],
        "semantic_placement_replay_match": replay_match,
        "pair_summary": pair_summary,
        "anonymous_staffing_witness_proven": rebound.staffing_summary["witness_proven"],
        "materialized_section_count": 320,
        "materialized_online_session_count": 6,
    }
    (R5_ROOT / "fresh_orm_placement_replay_report.json").write_text(
        json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
