from dataclasses import replace
from pathlib import Path

import pytest

import backend.tests.production_scale_special_scheduling_validation as scenario
from backend.apps.scheduling.services import section_placement as placement_service


RESULT_PATH = Path(r"C:\Users\desou\AppData\Local\Temp\placement-objective-probe.txt")


class _StopAfterPlacement(Exception):
    pass


@pytest.mark.django_db
def test_temporary_placement_objective_probe(counselor_user, monkeypatch):
    RESULT_PATH.write_text("START\n", encoding="utf-8")
    original_solve = placement_service.solve_section_placement

    def longer_objective_solve(data):
        return original_solve(replace(data, time_limit_seconds=120))

    monkeypatch.setattr(placement_service, "solve_section_placement", longer_objective_solve)

    original_approval = scenario.approve_section_placement_run

    def capture_and_stop(run, **kwargs):
        RESULT_PATH.write_text(
            repr({
                "status": run.status,
                "result": run.result,
                "assignment_count": len(run.result.get("assignments", ())),
            }),
            encoding="utf-8",
        )
        raise _StopAfterPlacement

    monkeypatch.setattr(scenario, "approve_section_placement_run", capture_and_stop)
    with pytest.raises(_StopAfterPlacement):
        scenario.run_production_scale_special_scheduling_validation(
            counselor_user=counselor_user,
            prefix="scale-a",
            include_reruns=False,
        )
