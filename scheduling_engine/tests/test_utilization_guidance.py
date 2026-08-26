from scheduling_engine.dto import (
    StudentAssignmentInputDTO,
    StudentAssignmentRequestDTO,
    StudentAssignmentSectionDTO,
)
from scheduling_engine.student_assignment.utilization_guidance import (
    select_utilization_cluster_targets,
)


def _guidance_fixture():
    requests = tuple(
        StudentAssignmentRequestDTO(
            request_id=request_id,
            student_id=request_id,
            course_id=1,
            course_offering_id=1,
            is_primary=True,
            is_mandatory=True,
            priority_tier=0,
        )
        for request_id in (1, 2, 3)
    )
    sections = tuple(
        StudentAssignmentSectionDTO(
            section_id=section_id,
            delivery_group_id=1,
            member_course_offering_ids=(1,),
            member_course_ids=(1,),
            semester=1,
            timeslot_id=section_id,
            capacity_max=10,
            target_capacity=10,
        )
        for section_id in (1, 2, 3)
    )
    data = StudentAssignmentInputDTO(
        academic_year_id=1,
        requests=requests,
        sections=sections,
        fixed_enrollments=(),
        hard_prerequisites=(),
        soft_sequence_preferences=(),
        section_utilization_balance_importance="important",
        student_semester_balance_importance="not_important",
        course_sequence_preferences_importance="not_important",
    )
    quality = {
        "section_utilization_balance": {
            "entities": {
                "1": {
                    "section_enrollment_counts": {"1": 6, "2": 2, "3": 0},
                    "pairwise_absolute_difference": 14,
                }
            }
        }
    }
    source_decisions = (
        (("course", 1), (1, 1, None, 1, 1, None)),
        (("course", 2), (2, 1, None, 1, 1, None)),
        (("course", 3), (3, 2, None, 1, 2, None)),
    )
    return data, quality, source_decisions


def test_utilization_leverage_uses_exact_pairwise_penalty_and_is_guidance_only():
    data, quality, source = _guidance_fixture()
    selection = select_utilization_cluster_targets(
        data,
        quality,
        source,
        target_scope_size=2,
        policy="top_individual",
    )

    assert selection.selected_student_ids == (1, 2)
    assert selection.pressure_facts[0].pairwise_penalty == 14
    assert selection.pressure_facts[0].section_capacities == ((1, 10), (2, 10), (3, 10))
    assert selection.pressure_facts[0].total_penalty_share == 1.0
    student_one = next(
        item for item in selection.leverage_facts if item.student_id == 1
    )
    assert student_one.total_positive_leverage == 10
    assert student_one.strongest_single_move == 6
    assert student_one.move_facts[0]["hypothetical_penalty"] == 8
    assert selection.guidance_facts["guidance_only"] is True
    assert selection.guidance_facts["objective_attribution"] is False
    assert all(
        len(item["relevant_student_ids"]) <= 20
        for item in selection.guidance_facts["top_delivery_groups"]
    )


def test_fixed_utilization_cluster_scope_is_sorted_and_not_reselected():
    data, quality, source = _guidance_fixture()
    selection = select_utilization_cluster_targets(
        data,
        quality,
        source,
        target_scope_size=2,
        policy="interaction_aware",
        fixed_student_ids=(3, 1),
    )

    assert selection.selected_student_ids == (1, 3)
    assert selection.guidance_facts["selection_reason"] == "fixed_scope"


def test_utilization_guidance_deduplicates_semantic_move_opportunities():
    data, quality, source = _guidance_fixture()
    duplicate_request = StudentAssignmentRequestDTO(
        request_id=4,
        student_id=1,
        course_id=1,
        course_offering_id=1,
        is_primary=True,
        is_mandatory=True,
        priority_tier=0,
    )
    data = StudentAssignmentInputDTO(
        **{**data.__dict__, "requests": data.requests + (duplicate_request,)}
    )
    source = source + ((("course", 4), (1, 1, None, 1, 1, None)),)
    selection = select_utilization_cluster_targets(
        data,
        quality,
        source,
        target_scope_size=1,
        policy="top_individual",
    )
    student_one = next(
        item for item in selection.leverage_facts if item.student_id == 1
    )
    assert student_one.alternate_section_opportunity_count == 2


def test_utilization_selection_policies_are_explicit_and_deterministic():
    data, quality, source = _guidance_fixture()
    for policy in (
        "top_individual",
        "delivery_group_focused",
        "interaction_aware",
        "mixed",
    ):
        first = select_utilization_cluster_targets(
            data,
            quality,
            source,
            target_scope_size=2,
            policy=policy,
        )
        second = select_utilization_cluster_targets(
            data,
            quality,
            source,
            target_scope_size=2,
            policy=policy,
        )
        assert first.selected_student_ids == second.selected_student_ids
        assert first.guidance_facts["policy"] == policy
        assert first.guidance_facts["guidance_only"] is True
