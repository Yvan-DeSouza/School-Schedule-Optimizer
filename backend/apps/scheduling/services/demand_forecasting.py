"""Scheduling-owned demand forecasts for backend application services.

The pure scheduling engine knows how to analyze demand, but non-scheduling
apps should not import engine modules directly.  This small facade lets course
offering workflows reuse the exact planner forecast without crossing the
Django-to-engine boundary themselves.
"""

from scheduling_engine.demand_analyzer import analyze_demand

from backend.apps.scheduling.services.engine_adapter import load_scheduling_input


def predicted_primary_demand_by_course(academic_year):
    """Return forecasted primary demand keyed by course id for one year."""

    summaries = analyze_demand(load_scheduling_input(academic_year.id)).summaries
    return {
        item.course_id: item.primary_requests
        * (
            1.0
            if item.historical_conversion_ratio is None
            else item.historical_conversion_ratio
        )
        for item in summaries
    }


def historical_conversion_evidence_by_course(academic_year):
    """Return planner-consistent ratios without leaking engine imports upstream.

    Counselor-owned conflict matrices need the exact same historical conversion
    rule as section planning.  This scheduling facade keeps the pure-engine
    dependency out of the constraints app.
    """

    return {
        item.course_id: {
            "ratio": 1.0 if item.historical_conversion_ratio is None else item.historical_conversion_ratio,
            "uses_current_demand_fallback": item.lacks_historical_data,
        }
        for item in analyze_demand(load_scheduling_input(academic_year.id)).summaries
    }
