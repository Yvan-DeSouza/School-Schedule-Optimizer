"""Small fresh-source replay through production placement approval."""

from dataclasses import asdict
from decimal import Decimal

import pytest

from backend.apps.common.constants import (
    GRADE_LEVEL_12,
    QUALIFICATION_DIVISION_SENIOR,
    QUALIFICATION_REVIEW_VERIFIED,
)
from backend.apps.common.models import AcademicYear
from backend.apps.constraints.models import (
    CourseConflictMatrix,
    CourseQualificationRequirement,
    Qualification,
    TeacherQualification,
)
from backend.apps.courses.constants import COURSE_DURATION_HALF_SEMESTER
from backend.apps.courses.models import (
    Course,
    HalfSemesterCoursePair,
    HalfSemesterSectionPair,
    Section,
)
from backend.apps.courses.services.offerings import ensure_academic_year_offerings
from backend.apps.scheduling.models import (
    CapacityProfile,
    CoursePriorityProfile,
    SectionBudgetApproval,
    SectionBudgetApprovalOffering,
    SectionBudgetRun,
    SectionPlacementApprovalAssignment,
    SectionPlacementRun,
    SectionSchedule,
    TeacherPlanningCapacity,
    TeacherPlanningRoster,
    TeacherPlanningRosterMember,
    TimeSlot,
)
from backend.apps.scheduling.services.engine_adapter import (
    load_section_placement_input,
    placement_input_fingerprint,
)
from backend.apps.scheduling.services.section_placement import (
    approve_section_placement_run,
    create_section_placement_run,
)
from scheduling_engine.dto import PlacementAssignmentDTO, PlacementResultDTO
from scheduling_engine.placement_research_artifacts import (
    placement_input_semantic_fingerprint,
    placement_result_semantic_fingerprint,
    rebind_placement_result,
    serialize_frozen_placement_input,
    serialize_frozen_placement_result,
)


def _build_year_source(*, suffix, counselor_user, teacher_user, qualification, catalog=None):
    year_name = "2040-2041" if suffix == "A" else "2042-2043"
    academic_year = AcademicYear.objects.create(name=year_name)
    if catalog is None:
        profile = CapacityProfile.objects.create(
            name="Replay canonical capacity",
            hard_min=1,
            soft_min=1,
            target=4,
            soft_max=5,
            hard_max=6,
        )
        priority = CoursePriorityProfile.objects.create(
            name="Replay canonical priority", tier=1,
        )
        normal = Course.objects.create(
            course_code="RPL4U",
            name="Replay normal course",
            grade_level=GRADE_LEVEL_12,
            category="math",
            capacity_profile=profile,
            priority_profile=priority,
            capacity_min=1,
            capacity_max=6,
        )
        first = Course.objects.create(
            course_code="RCH2O",
            name="Replay first half",
            grade_level=GRADE_LEVEL_12,
            category="social_sciences",
            duration=COURSE_DURATION_HALF_SEMESTER,
            credit_value=Decimal("0.5"),
            capacity_profile=profile,
            priority_profile=priority,
            capacity_min=1,
            capacity_max=6,
        )
        second = Course.objects.create(
            course_code="RGL2O",
            name="Replay second half",
            grade_level=GRADE_LEVEL_12,
            category="social_sciences",
            duration=COURSE_DURATION_HALF_SEMESTER,
            credit_value=Decimal("0.5"),
            capacity_profile=profile,
            priority_profile=priority,
            capacity_min=1,
            capacity_max=6,
        )
        pair = HalfSemesterCoursePair.objects.create(
            first_course=first,
            second_course=second,
        )
        for course in (normal, first, second):
            CourseQualificationRequirement.objects.create(
                course=course,
                qualification=qualification,
            )
        catalog = {
            "normal": normal,
            "first": first,
            "second": second,
            "pair": pair,
        }
    else:
        normal = catalog["normal"]
        first = catalog["first"]
        second = catalog["second"]
        pair = catalog["pair"]
    for semester in (1, 2):
        for block in ("A", "B", "C", "D"):
            TimeSlot.objects.create(
                academic_year=academic_year,
                semester=semester,
                block=block,
            )
        TeacherPlanningCapacity.objects.create(
            teacher=teacher_user.teacher_profile,
            academic_year=academic_year,
            semester=semester,
            maximum_sections=8,
        )
    roster = TeacherPlanningRoster.objects.create(
        academic_year=academic_year,
        status="ready",
    )
    TeacherPlanningRosterMember.objects.create(
        roster=roster,
        teacher=teacher_user.teacher_profile,
        added_by=counselor_user,
    )
    offerings = ensure_academic_year_offerings(academic_year, actor=counselor_user)
    groups = {
        offering.course_id: offering.delivery_group
        for offering in offerings
    }
    budget_run = SectionBudgetRun.objects.create(
        academic_year=academic_year,
        created_by=counselor_user,
        status="complete",
        budget_type="exact",
        section_budget=3,
    )
    budget = SectionBudgetApproval.objects.create(
        budget_run=budget_run,
        approved_by=counselor_user,
        reason="Approve bounded placement replay source.",
    )
    for course in (normal, first, second):
        SectionBudgetApprovalOffering.objects.create(
            approval=budget,
            delivery_group=groups[course.id],
            recommended_annual_count=1,
            recommended_semester_1_count=1,
            recommended_semester_2_count=0,
            approved_annual_count=1,
            approved_semester_1_count=1,
            approved_semester_2_count=0,
        )
    matrix = CourseConflictMatrix.objects.create(
        academic_year=academic_year,
        initialization_mode="fresh_current_demand",
        created_by=counselor_user,
    )
    return {
        "academic_year": academic_year,
        "budget": budget,
        "matrix": matrix,
        "pair": pair,
        "groups": groups,
        "catalog": catalog,
    }


def _materialized_result(*, data, source_result, academic_year, approval):
    sections = {
        (section.delivery_group_id, assignment.annual_index): (
            section,
            assignment.timeslot,
        )
        for assignment in SectionPlacementApprovalAssignment.objects.filter(
            approval=approval,
        ).select_related("section", "timeslot")
        for section in (assignment.section,)
    }
    online = {
        session.id: session.timeslot
        for session in academic_year.onlinesupervisionsession_set.all()
        if session.placement_approval_id == approval.id
    }
    assignments = []
    for item in source_result.assignments:
        if item.online_supervision_session_id is not None:
            slot = online[item.online_supervision_session_id]
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
        section, slot = sections[item.delivery_group_id, item.annual_index]
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
        status=source_result.status,
        solver_outcome=source_result.solver_outcome,
        assignments=tuple(assignments),
        unplaced_unit_keys=(),
        diagnostics=source_result.diagnostics,
        objective_components=dict(source_result.objective_components),
        staffing_summary=dict(source_result.staffing_summary),
    )


@pytest.mark.django_db(transaction=True)
def test_frozen_placement_replays_through_fresh_year_and_approval(
    counselor_user, teacher_user, tmp_path,
):
    qualification = Qualification.objects.create(
        code="replay-senior",
        name="Replay senior qualification",
        kind="teachable",
        subject_code="mathematics",
        division=QUALIFICATION_DIVISION_SENIOR,
    )
    TeacherQualification.objects.create(
        teacher=teacher_user.teacher_profile,
        qualification=qualification,
        review_status=QUALIFICATION_REVIEW_VERIFIED,
    )

    first = _build_year_source(
        suffix="A", counselor_user=counselor_user,
        teacher_user=teacher_user, qualification=qualification,
    )
    first_data, _first_matrix, _first_roster = load_section_placement_input(
        academic_year_id=first["academic_year"].id,
        input_mode="annual_total",
        budget_approval=first["budget"],
        conflict_matrix=first["matrix"],
    )
    first_run = create_section_placement_run(
        academic_year_id=first["academic_year"].id,
        input_mode="annual_total",
        budget_approval=first["budget"],
        created_by=counselor_user,
    )
    assert first_run.status == "complete"
    source_result = PlacementResultDTO(
        status=first_run.result["status"],
        solver_outcome=first_run.result["solver_outcome"],
        assignments=tuple(
            PlacementAssignmentDTO(**item)
            for item in first_run.result["assignments"]
        ),
        unplaced_unit_keys=tuple(first_run.result.get("unplaced_unit_keys", ())),
        diagnostics=tuple(first_run.result.get("diagnostics", ())),
        objective_components=dict(first_run.result.get("objective_components", {})),
        staffing_summary=dict(first_run.result.get("staffing_summary", {})),
    )
    input_path = tmp_path / "fresh-replay-input.json"
    result_path = tmp_path / "fresh-replay-result.json"
    serialize_frozen_placement_input(input_path, data=first_data, provenance={"test": True})
    serialize_frozen_placement_result(
        result_path, data=first_data, result=source_result,
        provenance={"test": True},
    )

    second = _build_year_source(
        suffix="B", counselor_user=counselor_user,
        teacher_user=teacher_user, qualification=qualification,
        catalog=first["catalog"],
    )
    second_data, second_matrix, second_roster = load_section_placement_input(
        academic_year_id=second["academic_year"].id,
        input_mode="annual_total",
        budget_approval=second["budget"],
        conflict_matrix=second["matrix"],
    )
    assert placement_input_semantic_fingerprint(first_data) == placement_input_semantic_fingerprint(second_data)
    rebound = rebind_placement_result(first_data, source_result, second_data)
    assert placement_result_semantic_fingerprint(first_data, source_result) == placement_result_semantic_fingerprint(second_data, rebound)

    snapshot = asdict(second_data)
    snapshot["fingerprint"] = placement_input_fingerprint(snapshot)
    snapshot["matrix_revision"] = second_matrix.revision
    snapshot["roster_id"] = second_roster.id
    replay_run = SectionPlacementRun.objects.create(
        academic_year=second["academic_year"],
        input_mode="annual_total",
        budget_approval=second["budget"],
        conflict_matrix=second_matrix,
        teacher_roster=second_roster,
        created_by=counselor_user,
        status="complete",
        input_snapshot=snapshot,
        result=asdict(rebound),
        solver_metadata={
            "engine": "frozen-research-replay",
            "source_result_fingerprint": placement_result_semantic_fingerprint(
                first_data, source_result,
            ),
        },
    )
    approval = approve_section_placement_run(
        replay_run,
        approved_by=counselor_user,
        reason="Approve lossless frozen placement replay.",
    )
    materialized = _materialized_result(
        data=second_data,
        source_result=rebound,
        academic_year=second["academic_year"],
        approval=approval,
    )
    assert placement_result_semantic_fingerprint(second_data, materialized) == placement_result_semantic_fingerprint(first_data, source_result)
    pairs = list(HalfSemesterSectionPair.objects.filter(
        course_pair=second["pair"],
    ).select_related("first_section", "second_section"))
    assert len(pairs) == 1
    pair = pairs[0]
    first_slot = SectionSchedule.objects.get(section=pair.first_section).timeslot_id
    second_slot = SectionSchedule.objects.get(section=pair.second_section).timeslot_id
    assert pair.first_section.semester == pair.second_section.semester
    assert first_slot == second_slot
    assert Section.objects.filter(academic_year=second["academic_year"]).count() == 3
