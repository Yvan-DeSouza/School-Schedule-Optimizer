from scheduling_engine.r16_scope_structure_forensics import (
    derive_interaction_scope,
    scope_structure,
)


def test_scope_structure_labels_guidance_cycles_without_claiming_feasibility():
    snapshot = {
        "utilization_candidates": [
            {
                "student_id": 1,
                "rank": 1,
                "total_positive_leverage": 10,
                "strongest_single_move": 10,
                "relevant_group_count": 1,
                "alternate_section_opportunity_count": 1,
                "delivery_group_ids": [7],
                "move_facts": [{
                    "delivery_group_id": 7,
                    "request_id": 101,
                    "from_section_id": 11,
                    "to_section_id": 12,
                    "positive_leverage": 10,
                }],
            },
            {
                "student_id": 2,
                "rank": 2,
                "total_positive_leverage": 8,
                "strongest_single_move": 8,
                "relevant_group_count": 1,
                "alternate_section_opportunity_count": 1,
                "delivery_group_ids": [7],
                "move_facts": [{
                    "delivery_group_id": 7,
                    "request_id": 102,
                    "from_section_id": 12,
                    "to_section_id": 11,
                    "positive_leverage": 8,
                }],
            },
        ],
        "pressure_candidates": [],
        "utilization_groups": [{
            "delivery_group_id": 7,
            "pairwise_penalty": 10,
            "total_penalty_share": 1.0,
        }],
    }
    source = {
        ("course", 101): (1, 11, None, 1, 1, None),
        ("course", 102): (2, 12, None, 1, 2, None),
    }
    requests = {
        101: {"student_id": 1, "course_id": 1},
        102: {"student_id": 2, "course_id": 1},
    }

    result = scope_structure(snapshot, source, (1, 2), requests)

    assert result["reciprocal_move_pair_count"] == 1
    assert result["potential_chain_count"] == 2
    assert result["structural_cycle_candidate_count"] == 1
    assert result["destinations_potentially_freed_by_selected_student"] == 2
    assert result["guidance_only"] is True
    assert result["full_feasibility_not_inferred"] is True


def test_interaction_scope_prefers_top_group_then_overlap_deterministically():
    snapshot = {
        "utilization_groups": [{"delivery_group_id": 9}],
        "utilization_candidates": [
            {"student_id": 1, "total_positive_leverage": 20, "strongest_single_move": 20, "relevant_group_count": 1, "alternate_section_opportunity_count": 1, "delivery_group_ids": [1]},
            {"student_id": 2, "total_positive_leverage": 19, "strongest_single_move": 19, "relevant_group_count": 1, "alternate_section_opportunity_count": 1, "delivery_group_ids": [9]},
            {"student_id": 3, "total_positive_leverage": 18, "strongest_single_move": 18, "relevant_group_count": 2, "alternate_section_opportunity_count": 2, "delivery_group_ids": [9, 10]},
            {"student_id": 4, "total_positive_leverage": 17, "strongest_single_move": 17, "relevant_group_count": 1, "alternate_section_opportunity_count": 1, "delivery_group_ids": [10]},
        ],
    }
    assert derive_interaction_scope(snapshot, count=3) == (2, 3, 4)

