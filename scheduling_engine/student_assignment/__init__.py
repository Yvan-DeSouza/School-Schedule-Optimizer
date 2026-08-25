"""Public entrypoint for the pure student-assignment solver.

Callers continue to import ``solve_student_assignment`` from this package;
the internal modules are deliberately not part of the solver's public API.
"""

from .core import (
    run_student_assignment_targeted_repair_diagnostic,
    run_student_assignment_targeted_s1_diagnostic,
    run_student_assignment_targeted_s2_diagnostic,
    run_student_assignment_ordinary_repair_diagnostic,
    run_student_assignment_operator_session_diagnostic,
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
from .adaptive_search import (
    ADAPTIVE_POLICY_VERSION,
    AdaptiveOperatorAttempt,
    AdaptiveOperatorSpec,
    AdaptivePolicyDecision,
    AdaptiveSearchState,
    AdaptiveSessionRecord,
    DEFAULT_ADAPTIVE_OPERATOR_PORTFOLIO,
    build_adaptive_search_state,
    build_operator_session_request,
    choose_adaptive_operator,
    replay_adaptive_policy,
    simulate_adaptive_policy,
)
from .adaptive_runtime import (
    AdaptiveSessionResult,
    run_adaptive_local_search_diagnostic,
)
from .operator_session import (
    ContinuousOperatorSessionConfig,
    ContinuousOperatorSessionRecord,
    OPERATOR_FAMILIES,
    TARGET_POLICIES,
    build_continuous_operator_session_record,
    operator_session_target_count,
    select_operator_session_targets,
)
__all__ = [
    "run_substantive_soft_tier_probe",
    "solve_student_assignment",
    "run_student_assignment_targeted_repair_diagnostic",
    "run_student_assignment_targeted_s1_diagnostic",
    "run_student_assignment_targeted_s2_diagnostic",
    "run_student_assignment_ordinary_repair_diagnostic",
    "run_student_assignment_operator_session_diagnostic",
    "StudentTargetPressure",
    "rank_students_by_quality_pressure",
    "reconcile_student_quality_pressure",
    "ADAPTIVE_POLICY_VERSION",
    "AdaptiveOperatorAttempt",
    "AdaptiveOperatorSpec",
    "AdaptivePolicyDecision",
    "AdaptiveSearchState",
    "AdaptiveSessionRecord",
    "AdaptiveSessionResult",
    "DEFAULT_ADAPTIVE_OPERATOR_PORTFOLIO",
    "build_adaptive_search_state",
    "build_operator_session_request",
    "choose_adaptive_operator",
    "replay_adaptive_policy",
    "simulate_adaptive_policy",
    "run_adaptive_local_search_diagnostic",
    "ContinuousOperatorSessionConfig",
    "ContinuousOperatorSessionRecord",
    "OPERATOR_FAMILIES",
    "TARGET_POLICIES",
    "build_continuous_operator_session_record",
    "operator_session_target_count",
    "select_operator_session_targets",
]
