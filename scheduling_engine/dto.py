from dataclasses import dataclass, field
from typing import Mapping, Optional, Tuple


@dataclass(frozen=True)
class CourseDTO:
    id: int
    course_code: str
    name: str
    capacity_min: int
    capacity_max: int
    grade_level: int = 0
    category: str = ""
    is_online: bool = False


@dataclass(frozen=True)
class CourseRequestDTO:
    student_id: int
    course_id: int
    # The Django adapter translates the canonical request-type values into this
    # engine-neutral flag. This keeps the engine independent of Django enums.
    is_primary: bool
    is_mandatory: bool = False


@dataclass(frozen=True)
class HistoricalDemandDTO:
    course_id: int
    requests: int
    final_enrollment: int
    academic_year_id: int = 0


@dataclass(frozen=True)
class AcademicYearDTO:
    id: int
    name: str


@dataclass(frozen=True)
class SectionDTO:
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
    id: int
    grade_level: int


@dataclass(frozen=True)
class TeacherDTO:
    id: int
    max_courses_per_semester: int
    max_courses_total: int
    seniority: int = 0
    reduced_load: bool = False


@dataclass(frozen=True)
class RoomDTO:
    id: int
    room_type: str
    capacity: int
    is_specialized: bool = False


@dataclass(frozen=True)
class TimeSlotDTO:
    id: int
    academic_year_id: int
    semester: int
    block: str
    is_available: bool = True


@dataclass(frozen=True)
class QualificationDTO:
    id: int
    name: str


@dataclass(frozen=True)
class TeacherQualificationDTO:
    teacher_id: int
    qualification_id: int


@dataclass(frozen=True)
class TeacherCoursePreferenceDTO:
    teacher_id: int
    course_id: int


@dataclass(frozen=True)
class TeacherCurrentCourseDTO:
    teacher_id: int
    course_id: int
    academic_year_id: int


@dataclass(frozen=True)
class TeacherAvailabilityDTO:
    teacher_id: int
    timeslot_id: int
    is_available: bool = True


@dataclass(frozen=True)
class CourseRoomRequirementDTO:
    course_id: int
    room_type: str


@dataclass(frozen=True)
class CourseQualificationRequirementDTO:
    course_id: int
    qualification_id: int


@dataclass(frozen=True)
class CoursePrerequisiteDTO:
    course_id: int
    prerequisite_id: int


@dataclass(frozen=True)
class CourseConflictDTO:
    course_a_id: int
    course_b_id: int
    weight: float


@dataclass(frozen=True)
class SectionLockDTO:
    section_id: int
    locked_teacher_id: Optional[int] = None
    locked_timeslot_id: Optional[int] = None
    locked_room_id: Optional[int] = None


@dataclass(frozen=True)
class HardConstraintDTO:
    id: int
    name: str
    type: str
    priority: int = 100


@dataclass(frozen=True)
class SoftConstraintDTO:
    id: int
    name: str
    category: str
    default_weight: int = 1


@dataclass(frozen=True)
class CounselorConstraintPreferenceDTO:
    counselor_id: int
    soft_constraint_id: int
    weight: int


@dataclass(frozen=True)
class SchedulingInputDTO:
    academic_year_id: int
    academic_years: Tuple[AcademicYearDTO, ...] = ()
    courses: Tuple[CourseDTO, ...] = ()
    course_requests: Tuple[CourseRequestDTO, ...] = ()
    historical_demand: Tuple[HistoricalDemandDTO, ...] = ()
    sections: Tuple[SectionDTO, ...] = ()
    students: Tuple[StudentDTO, ...] = ()
    teachers: Tuple[TeacherDTO, ...] = ()
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
    course_id: int
    course_code: str
    primary_requests: int
    alternate_requests: int
    total_requests: int
    historical_conversion_ratio: Optional[float]
    lacks_historical_data: bool


@dataclass(frozen=True)
class CourseConflictRecommendationDTO:
    course_a_id: int
    course_b_id: int
    weight: float
    co_request_count: int


@dataclass(frozen=True)
class DemandAnalysisResultDTO:
    summaries: Tuple[DemandSummaryDTO, ...]
    conflict_recommendations: Tuple[CourseConflictRecommendationDTO, ...]


@dataclass(frozen=True)
class SectionCountRecommendationDTO:
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
    academic_year_id: int
    course_by_id: Mapping[int, CourseDTO]
    section_by_id: Mapping[int, SectionDTO]
    qualified_teacher_ids_by_course: Mapping[int, frozenset[int]]
    available_timeslot_ids_by_teacher: Mapping[int, frozenset[int]]
    preferred_course_ids_by_teacher: Mapping[int, frozenset[int]]
    current_course_ids_by_teacher: Mapping[int, frozenset[int]]
    required_room_types_by_course: Mapping[int, frozenset[str]]
    required_qualification_ids_by_course: Mapping[int, frozenset[int]]
    prerequisite_ids_by_course: Mapping[int, frozenset[int]]
    conflict_weights_by_course_pair: Mapping[tuple[int, int], float]
    locked_sections_by_id: Mapping[int, SectionLockDTO]
    available_room_ids: frozenset[int]
    available_timeslot_ids: frozenset[int]
    hard_constraint_priorities: Mapping[str, int]
    soft_constraint_weights: Mapping[int, int]
    counselor_constraint_weights: Mapping[tuple[int, int], int]
