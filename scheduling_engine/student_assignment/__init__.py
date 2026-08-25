"""Public entrypoint for the pure student-assignment solver.

Callers continue to import ``solve_student_assignment`` from this package;
the internal modules are deliberately not part of the solver's public API.
"""

from .core import (
    run_student_assignment_targeted_repair_diagnostic,
    run_student_assignment_targeted_s1_diagnostic,
    run_student_assignment_targeted_s2_diagnostic,
    run_student_assignment_ordinary_repair_diagnostic,
    run_substantive_soft_tier_probe,
    solve_student_assignment,
)
from .search_guidance import (
    StudentTargetPressure,
    rank_students_by_quality_pressure,
    reconcile_student_quality_pressure,
)
from .search_experiments import (
    RANKING_POLICY_DETERMINISTIC,
    RANKING_POLICY_RAW,
    RANKING_POLICY_WEIGHTED,
    StudentSearchExperimentRecord,
    build_search_experiment_record,
    rank_students_by_raw_local_penalty,
    select_deterministic_control_students,
    select_interacting_second_student,
    source_decision_fingerprint,
)
__all__ = [
    "run_substantive_soft_tier_probe",
    "solve_student_assignment",
    "run_student_assignment_targeted_repair_diagnostic",
    "run_student_assignment_targeted_s1_diagnostic",
    "run_student_assignment_targeted_s2_diagnostic",
    "run_student_assignment_ordinary_repair_diagnostic",
    "StudentTargetPressure",
    "rank_students_by_quality_pressure",
    "reconcile_student_quality_pressure",
]
