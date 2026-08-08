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
    reduced_load: bool = False


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
