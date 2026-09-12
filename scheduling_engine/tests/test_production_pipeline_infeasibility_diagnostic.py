"""Regression coverage for the preserved production-pipeline negative run."""

from scheduling_engine.diagnose_production_pipeline_stage1_infeasibility import (
    ARTIFACT,
    _load_groups,
    _source_request_courses,
    solve_layer,
)


def test_frozen_production_half_pair_failure_remains_a_distinct_layer():
    """The old frozen input must not be rewritten to appear feasible."""

    payload, results = _load_groups(ARTIFACT)
    request_courses = _source_request_courses()
    ordinary = solve_layer(
        results,
        included_kinds={"ordinary"},
        request_courses=request_courses,
        grades={10},
        time_limit_seconds=15.0,
    )
    ordinary_and_half = solve_layer(
        results,
        included_kinds={"ordinary", "half_pair"},
        request_courses=request_courses,
        grades={10},
        time_limit_seconds=15.0,
    )

    assert payload["final_staffing_input"]["fingerprint"] == (
        "4ca85caf11673a604bb373beb9d443b6cee8bb295698933c345699bc9020a7fa"
    )
    assert ordinary["status"] in {"optimal", "feasible"}
    assert ordinary_and_half["status"] == "infeasible"
