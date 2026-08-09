"""Immutable data-transfer objects forming the engine's public boundary.

These structures intentionally contain primitive values and other DTOs only.
They are not ORM mirrors: fields are selected for scheduling needs so the pure
engine can run in tests or another process without Django settings or a database.

IDs are opaque identifiers supplied by the adapter.  The engine uses them for
joins and returns them in results, but never assumes database behavior.
"""

from dataclasses import dataclass, field
from typing import Mapping, Optional, Tuple


@dataclass(frozen=True)
class CourseDTO:
    """Scheduling-relevant catalog and planning policy for one course."""

    id: int
    course_code: str
    name: str
    # Legacy bounds remain for the older estimator/API.  New section planning
    # uses the five-value capacity profile below.
    capacity_min: int
    capacity_max: int
    grade_level: int = 0
    category: str = ""
    is_online: bool = False
    # The adapter derives this legal switch from the canonical grade threshold;
    # it is not inferred again inside individual solvers.
    requires_statutory_qualification: bool = False
    capacity_profile_id: int = 0
    hard_min: int = 1
    soft_min: int = 1
    target_capacity: int = 1
    soft_max: int = 1
    hard_max: int = 1
    allowed_semester: str = "either_semester"
    priority_tier: int = 4
    priority_profile_id: int = 0


@dataclass(frozen=True)
class CourseRequestDTO:
    """One student's primary or alternate request for a course."""

    student_id: int
    course_id: int
    # The Django adapter translates the canonical request-type values into this
    # engine-neutral flag. This keeps the engine independent of Django enums.
    is_primary: bool
    is_mandatory: bool = False


@dataclass(frozen=True)
class HistoricalDemandDTO:
    """Prior-year requests and realized enrollment used for conversion ratios."""

    course_id: int
    requests: int
    final_enrollment: int
    academic_year_id: int = 0


@dataclass(frozen=True)
class AcademicYearDTO:
    """Minimal year identity used to order historical data safely."""

    id: int
    name: str


@dataclass(frozen=True)
class SectionDTO:
    """An already-existing operational section loaded as planning context."""

    id: int
    course_id: int
    academic_year_id: int
    semester: int
    capacity_min: int
    capacity_max: int
    teacher_id: Optional[int] = None
    is_locked: bool = False
    delivery_group_id: int = 0
    member_course_ids: Tuple[int, ...] = ()


@dataclass(frozen=True)
class PlanningOfferingDTO:
    """One physical planning unit backed by one or more catalog courses."""

    id: int
    member_course_ids: Tuple[int, ...]
    member_course_codes: Tuple[str, ...]
    capacity_profile_id: int
    hard_min: int
    soft_min: int
    target_capacity: int
    soft_max: int
    hard_max: int
    allowed_semester: str = "either_semester"
    priority_tier: int = 4
    is_combined: bool = False


@dataclass(frozen=True)
class StudentDTO:
    """Minimal student facts required by demand and future assignment stages."""

    id: int
    grade_level: int


@dataclass(frozen=True)
class TeacherDTO:
    """Teacher identity and default load limits, never a proposed assignment."""

    id: int
    max_courses_per_semester: int
    max_courses_total: int
    seniority: int = 0
    is_reduced_load: bool = False


@dataclass(frozen=True)
class TeacherPlanningCapacityDTO:
    """Effective per-semester planning limit before scenario reductions."""

    teacher_id: int
    semester: int
    maximum_sections: int
    # Reserved includes administrator reservations and committed/locked sections
    # assembled by the adapter.
    reserved_sections: int = 0


@dataclass(frozen=True)
class RoomDTO:
    """Room attributes reserved for later placement solving."""

    id: int
    room_type: str
    capacity: int
    is_specialized: bool = False


@dataclass(frozen=True)
class TimeSlotDTO:
    """One recurring A-D block within a semester, not a calendar occurrence."""

    id: int
    academic_year_id: int
    semester: int
    block: str
    is_available: bool = True


@dataclass(frozen=True)
class QualificationDTO:
    """Normalized qualification catalog entry; raw Aspen text is excluded."""

    id: int
    name: str
    code: str = ""
    kind: str = ""
    subject_code: str = ""
    division: str = ""


@dataclass(frozen=True)
class TeacherQualificationDTO:
    """Normalized many-to-many link between a teacher and credential."""

    teacher_id: int
    qualification_id: int


@dataclass(frozen=True)
class TeacherCoursePreferenceDTO:
    """Structured teacher preference for a specific catalog course."""

    teacher_id: int
    course_id: int


@dataclass(frozen=True)
class TeacherCurrentCourseDTO:
    """Course recently/currently taught, available to preference objectives."""

    teacher_id: int
    course_id: int
    academic_year_id: int


@dataclass(frozen=True)
class TeacherAvailabilityDTO:
    """Teacher availability for a recurring semester timeslot."""

    teacher_id: int
    timeslot_id: int
    is_available: bool = True


@dataclass(frozen=True)
class CourseRoomRequirementDTO:
    """Required room type for a course's future placement candidates."""

    course_id: int
    room_type: str


@dataclass(frozen=True)
class CourseQualificationRequirementDTO:
    """Normalized required/preferred credential rule for one course."""

    course_id: int
    qualification_id: int
    is_required: bool = True


@dataclass(frozen=True)
class CoursePrerequisiteDTO:
    """Directed course prerequisite edge for future student assignment."""

    course_id: int
    prerequisite_id: int


@dataclass(frozen=True)
class StudentAssignmentRequestDTO:
    """One effective request that may receive one new enrollment."""

    request_id: int
    student_id: int
    course_id: int
    course_offering_id: int
    is_primary: bool
    is_mandatory: bool
    priority_tier: int
    assignment_basis: str = "primary_request"
    backup_resolution_snapshot: Mapping[str, object] | None = None
    # A scoped rerun may represent a course that already has an active
    # enrollment. The adapter supplies this opaque identifier so review and a
    # later approval service can preserve the replacement audit trail.
    current_enrollment_id: Optional[int] = None
    # Scoped reruns retain out-of-scope request facts in the snapshot for
    # drift detection, while the pure solver ignores requests marked false.
    is_in_scope: bool = True


@dataclass(frozen=True)
class StudentAssignmentSectionDTO:
    """Fixed active physical section eligible for new enrollment."""

    section_id: int
    delivery_group_id: int
    member_course_offering_ids: Tuple[int, ...]
    member_course_ids: Tuple[int, ...]
    semester: int
    timeslot_id: int
    capacity_max: int
    target_capacity: int
    # Teacher identity remains fixed context. It is only needed by the
    # next-release student-to-teacher lock; the engine never assigns teachers.
    teacher_id: Optional[int] = None


@dataclass(frozen=True)
class FixedEnrollmentDTO:
    """Existing enrollment and its operational status for a rerun.

    Historical rows are audit evidence only and must not consume capacity or a
    timeslot. An active row becomes a decision variable only when it is both
    in scope and unlocked; all other active rows remain fixed context.
    """

    student_id: int
    section_id: int
    course_offering_id: int
    course_id: int
    semester: int
    timeslot_id: int
    enrollment_id: Optional[int] = None
    is_active: bool = True
    is_locked: bool = False
    is_historical: bool = False
    is_in_scope: bool = False
    lock_ids: Tuple[int, ...] = ()


@dataclass(frozen=True)
class StudentAssignmentLockDTO:
    """One active counselor lock expressed without Django model references."""

    lock_id: int
    lock_type: str
    student_id: Optional[int] = None
    section_id: Optional[int] = None
    course_id: Optional[int] = None
    teacher_id: Optional[int] = None
    member_student_ids: Tuple[int, ...] = ()
    is_active: bool = True


@dataclass(frozen=True)
class StudentAssignmentScopeDTO:
    """Resolved full-year or scoped rerun boundary frozen in engine input."""

    scope_type: str = "full"
    student_ids: Tuple[int, ...] = ()
    course_ids: Tuple[int, ...] = ()
    section_ids: Tuple[int, ...] = ()


@dataclass(frozen=True)
class CourseSequencePreferenceDTO:
    """Non-binding earlier-course to later-course sequencing relation."""

    earlier_course_id: int
    later_course_id: int


@dataclass(frozen=True)
class StudentAssignmentInputDTO:
    """Detached facts consumed by the pure student-to-section engine."""

    academic_year_id: int
    requests: Tuple[StudentAssignmentRequestDTO, ...]
    sections: Tuple[StudentAssignmentSectionDTO, ...]
    fixed_enrollments: Tuple[FixedEnrollmentDTO, ...]
    hard_prerequisites: Tuple[CoursePrerequisiteDTO, ...]
    soft_sequence_preferences: Tuple[CourseSequencePreferenceDTO, ...]
    section_utilization_balance_importance: str
    student_semester_balance_importance: str
    course_sequence_preferences_importance: str
    time_limit_seconds: float = 20.0
    student_assignment_locks: Tuple[StudentAssignmentLockDTO, ...] = ()
    schedule_preservation_level: str = "none"
    priority_request_ids: Tuple[int, ...] = ()
    # This is a resolved, run-snapshot value. The future school-wide
    # configuration model owns the default; the engine only enforces the
    # supplied bound and never reads Django configuration.
    priority_request_limit: Optional[int] = None
    scope: StudentAssignmentScopeDTO = field(default_factory=StudentAssignmentScopeDTO)


@dataclass(frozen=True)
class StudentAssignmentDTO:
    """One recommendation to create an enrollment after counselor approval."""

    request_id: int
    student_id: int
    section_id: int
    course_offering_id: int
    course_id: int
    semester: int
    timeslot_id: int
    assignment_basis: str
    backup_resolution_snapshot: Mapping[str, object] | None = None
    previous_enrollment_id: Optional[int] = None
    previous_section_id: Optional[int] = None


@dataclass(frozen=True)
class StudentAssignmentUnmetRequestDTO:
    """A request left unassigned in the best diagnostic recommendation."""

    request_id: int
    student_id: int
    course_id: int
    is_primary: bool
    is_mandatory: bool
    assignment_basis: str
    diagnostic_code: str
    blocking_lock_id: Optional[int] = None
    blocking_section_id: Optional[int] = None
    blocking_student_id: Optional[int] = None
    remediation_codes: Tuple[str, ...] = ()


@dataclass(frozen=True)
class StudentAssignmentLockCostDTO:
    """Counterfactual unresolved-request cost of one active lock."""

    lock_id: int
    attributable_request_count: int
    unresolved_request_ids: Tuple[int, ...]


@dataclass(frozen=True)
class StudentAssignmentSeatContentionDTO:
    """Demand evidence for a physical section whose seats were awarded."""

    section_id: int
    available_seat_count: int
    awarded_request_ids: Tuple[int, ...]
    competing_request_ids: Tuple[int, ...]


@dataclass(frozen=True)
class StudentAssignmentSectionBalanceDTO:
    """Observed final enrollment count against the section's target capacity."""

    section_id: int
    enrollment_count: int
    target_capacity: int
    diagnostic_code: Optional[str] = None


@dataclass(frozen=True)
class StudentAssignmentResultDTO:
    """Complete or partial student-assignment recommendation and diagnostics."""

    status: str
    solver_outcome: str
    assignments: Tuple[StudentAssignmentDTO, ...]
    unmet_requests: Tuple[StudentAssignmentUnmetRequestDTO, ...]
    diagnostics: Tuple[Mapping[str, object], ...]
    objective_components: Mapping[str, float]
    sequence_outcomes: Tuple[Mapping[str, object], ...]
    lock_costs: Tuple[StudentAssignmentLockCostDTO, ...] = ()
    seat_contention: Tuple[StudentAssignmentSeatContentionDTO, ...] = ()
    section_balance_facts: Tuple[StudentAssignmentSectionBalanceDTO, ...] = ()


@dataclass(frozen=True)
class CourseConflictDTO:
    """Weighted co-request conflict used by future section placement."""

    course_a_id: int
    course_b_id: int
    weight: float


@dataclass(frozen=True)
class SectionLockDTO:
    """Counselor-fixed section decisions that downstream solvers must respect."""

    section_id: int
    locked_teacher_id: Optional[int] = None
    locked_timeslot_id: Optional[int] = None
    locked_room_id: Optional[int] = None


@dataclass(frozen=True)
class HardConstraintDTO:
    """Configured hard-constraint metadata exposed to the compiler."""

    id: int
    name: str
    type: str
    priority: int = 100


@dataclass(frozen=True)
class SoftConstraintDTO:
    """Configured soft objective and its school-wide default weight."""

    id: int
    name: str
    category: str
    default_weight: int = 1


@dataclass(frozen=True)
class CounselorConstraintPreferenceDTO:
    """Counselor-specific override of a soft-constraint weight."""

    counselor_id: int
    soft_constraint_id: int
    weight: int


@dataclass(frozen=True)
class SchedulingInputDTO:
    """Complete immutable input for one academic-year engine invocation.

    Tuple defaults keep fixtures concise while preserving immutability.  The
    constraint compiler validates referential integrity before solvers consume
    these collections.
    """

    academic_year_id: int
    academic_years: Tuple[AcademicYearDTO, ...] = ()
    courses: Tuple[CourseDTO, ...] = ()
    planning_offerings: Tuple[PlanningOfferingDTO, ...] = ()
    course_requests: Tuple[CourseRequestDTO, ...] = ()
    historical_demand: Tuple[HistoricalDemandDTO, ...] = ()
    sections: Tuple[SectionDTO, ...] = ()
    students: Tuple[StudentDTO, ...] = ()
    teachers: Tuple[TeacherDTO, ...] = ()
    teacher_planning_capacities: Tuple[TeacherPlanningCapacityDTO, ...] = ()
    rooms: Tuple[RoomDTO, ...] = ()
    timeslots: Tuple[TimeSlotDTO, ...] = ()
    qualifications: Tuple[QualificationDTO, ...] = ()
    teacher_qualifications: Tuple[TeacherQualificationDTO, ...] = ()
    teacher_preferences: Tuple[TeacherCoursePreferenceDTO, ...] = ()
    teacher_current_courses: Tuple[TeacherCurrentCourseDTO, ...] = ()
    teacher_availability: Tuple[TeacherAvailabilityDTO, ...] = ()
    course_room_requirements: Tuple[CourseRoomRequirementDTO, ...] = ()
    course_qualification_requirements: Tuple[CourseQualificationRequirementDTO, ...] = ()
    course_prerequisites: Tuple[CoursePrerequisiteDTO, ...] = ()
    course_conflicts: Tuple[CourseConflictDTO, ...] = ()
    section_locks: Tuple[SectionLockDTO, ...] = ()
    hard_constraints: Tuple[HardConstraintDTO, ...] = ()
    soft_constraints: Tuple[SoftConstraintDTO, ...] = ()
    counselor_constraint_preferences: Tuple[CounselorConstraintPreferenceDTO, ...] = ()


@dataclass(frozen=True)
class PlacementUnitDTO:
    """One real section or stable pre-materialization annual delivery slot.

    Annual keys exist so a counselor can lock "Physics slot 3" before a
    semester-specific Section row exists.  They are deliberately not fake
    SectionDTOs with a nullable semester.
    """

    key: str
    delivery_group_id: int
    member_course_ids: Tuple[int, ...]
    allowed_semesters: Tuple[int, ...]
    section_id: Optional[int] = None
    fixed_semester: Optional[int] = None
    locked_timeslot_id: Optional[int] = None
    locked_teacher_id: Optional[int] = None
    annual_index: Optional[int] = None
    source_mode: str = "fixed_semester"


@dataclass(frozen=True)
class FixedPlacementDTO:
    """Accepted timing context outside this placement run's decision scope."""

    section_id: int
    timeslot_id: int
    teacher_id: Optional[int] = None


@dataclass(frozen=True)
class PlacementTeacherDTO:
    """Anonymous-to-output staffing facts used only to prove feasibility."""

    id: int
    eligible_course_ids: Tuple[int, ...]
    remaining_semester_1: int
    remaining_semester_2: int
    remaining_annual: int
    unavailable_timeslot_ids: Tuple[int, ...] = ()


@dataclass(frozen=True)
class PlacementConflictDTO:
    """Effective course-pair score plus estimated affected-student volume."""

    course_a_id: int
    course_b_id: int
    weight: float
    estimated_retained_co_request_count: float


@dataclass(frozen=True)
class PlacementInputDTO:
    """Detached input for semester/A-D placement with staffing feasibility."""

    academic_year_id: int
    input_mode: str
    units: Tuple[PlacementUnitDTO, ...]
    fixed_placements: Tuple[FixedPlacementDTO, ...]
    timeslots: Tuple[TimeSlotDTO, ...]
    teachers: Tuple[PlacementTeacherDTO, ...]
    conflicts: Tuple[PlacementConflictDTO, ...]
    time_limit_seconds: int = 30


@dataclass(frozen=True)
class PlacementAssignmentDTO:
    """A reviewable timing decision; it intentionally excludes a teacher name."""

    unit_key: str
    section_id: Optional[int]
    delivery_group_id: int
    semester: int
    timeslot_id: int
    block: str
    annual_index: Optional[int] = None


@dataclass(frozen=True)
class PlacementResultDTO:
    """Pure placement result with diagnostics and non-sensitive evidence only."""

    status: str
    solver_outcome: str
    assignments: Tuple[PlacementAssignmentDTO, ...]
    unplaced_unit_keys: Tuple[str, ...]
    diagnostics: Tuple[dict, ...]
    objective_components: Mapping[str, float]
    staffing_summary: Mapping[str, object]


@dataclass(frozen=True)
class TeacherAssignmentSectionDTO:
    """One accepted-timing section considered by named teacher assignment."""

    section_id: int
    delivery_group_id: int
    member_course_ids: Tuple[int, ...]
    semester: int
    timeslot_id: int
    locked_teacher_id: Optional[int] = None
    is_fixed: bool = False
    assigned_teacher_id: Optional[int] = None


@dataclass(frozen=True)
class TeacherAssignmentTeacherDTO:
    """Detached teacher facts used by the pure named-assignment solver."""

    id: int
    eligible_course_ids: Tuple[int, ...]
    remaining_semester_1: int
    remaining_semester_2: int
    remaining_annual: int
    unavailable_timeslot_ids: Tuple[int, ...] = ()
    preferred_course_ids: Tuple[int, ...] = ()
    prior_year_course_ids: Tuple[int, ...] = ()
    preferred_timeslot_ids: Tuple[int, ...] = ()
    avoided_timeslot_ids: Tuple[int, ...] = ()
    seniority: int = 0


@dataclass(frozen=True)
class TeacherCourseAssignmentRuleDTO:
    """Annual teacher/course hard bounds; a physical combined section counts per member course."""

    teacher_id: int
    course_id: int
    minimum_sections: int = 0
    maximum_sections: Optional[int] = None


@dataclass(frozen=True)
class FixedTeacherAssignmentDTO:
    """Named assignment outside the current write set that occupies teacher capacity."""

    section_id: int
    teacher_id: int
    semester: int
    timeslot_id: int
    member_course_ids: Tuple[int, ...] = ()


@dataclass(frozen=True)
class TeacherAssignmentInputDTO:
    """Pure input for assigning teachers after semester/block placement."""

    academic_year_id: int
    sections: Tuple[TeacherAssignmentSectionDTO, ...]
    teachers: Tuple[TeacherAssignmentTeacherDTO, ...]
    rules: Tuple[TeacherCourseAssignmentRuleDTO, ...] = ()
    fixed_assignments: Tuple[FixedTeacherAssignmentDTO, ...] = ()
    time_limit_seconds: int = 30


@dataclass(frozen=True)
class TeacherAssignmentDTO:
    """Reviewable named result for one section; timing is input, never a decision."""

    section_id: int
    teacher_id: int
    semester: int
    timeslot_id: int
    explanation: Mapping[str, object]


@dataclass(frozen=True)
class TeacherAssignmentResultDTO:
    """Pure named-teacher recommendation and stable diagnostic evidence."""

    status: str
    solver_outcome: str
    assignments: Tuple[TeacherAssignmentDTO, ...]
    unassigned_section_ids: Tuple[int, ...]
    diagnostics: Tuple[dict, ...]
    objective_components: Mapping[str, float]


@dataclass(frozen=True)
class DemandSummaryDTO:
    """Per-course request totals and historical-ratio provenance."""

    course_id: int
    course_code: str
    primary_requests: int
    alternate_requests: int
    total_requests: int
    historical_conversion_ratio: Optional[float]
    lacks_historical_data: bool


@dataclass(frozen=True)
class CourseConflictRecommendationDTO:
    """Derived course-pair overlap suitable for counselor review."""

    course_a_id: int
    course_b_id: int
    weight: float
    co_request_count: int


@dataclass(frozen=True)
class DemandAnalysisResultDTO:
    """Combined demand summaries and conflict recommendations."""

    summaries: Tuple[DemandSummaryDTO, ...]
    conflict_recommendations: Tuple[CourseConflictRecommendationDTO, ...]


@dataclass(frozen=True)
class SectionCountRecommendationDTO:
    """Legacy heuristic section-count recommendation response."""

    course_id: int
    course_code: str
    current_requests: int
    conversion_ratio: float
    predicted_enrollment: float
    capacity_min: int
    capacity_max: int
    recommended_section_count: int
    used_fallback_ratio: bool
    warnings: Tuple[str, ...] = ()


@dataclass(frozen=True)
class CompiledConstraintSetDTO:
    """Validated, indexed, read-only representation consumed by solvers.

    Maps replace repeated scans of raw DTO tuples.  Set-valued maps are frozen
    so one solver stage cannot accidentally change another stage's constraints.
    """

    academic_year_id: int
    course_by_id: Mapping[int, CourseDTO]
    section_by_id: Mapping[int, SectionDTO]
    qualified_teacher_ids_by_course: Mapping[int, frozenset[int]]
    available_timeslot_ids_by_teacher: Mapping[int, frozenset[int]]
    preferred_course_ids_by_teacher: Mapping[int, frozenset[int]]
    current_course_ids_by_teacher: Mapping[int, frozenset[int]]
    required_room_types_by_course: Mapping[int, frozenset[str]]
    required_qualification_ids_by_course: Mapping[int, frozenset[int]]
    preferred_qualification_ids_by_course: Mapping[int, frozenset[int]]
    prerequisite_ids_by_course: Mapping[int, frozenset[int]]
    conflict_weights_by_course_pair: Mapping[tuple[int, int], float]
    locked_sections_by_id: Mapping[int, SectionLockDTO]
    available_room_ids: frozenset[int]
    available_timeslot_ids: frozenset[int]
    hard_constraint_priorities: Mapping[str, int]
    soft_constraint_weights: Mapping[int, int]
    counselor_constraint_weights: Mapping[tuple[int, int], int]
