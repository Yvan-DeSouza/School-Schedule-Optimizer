"""Paul-Desmarais-shaped Grades 9--12 synthetic stress benchmark.

This module is intentionally fixture-only. It does not invoke CP-SAT, alter
the legacy research fixtures, or claim measured school enrollment/prevalence.
See ``docs/TARGET_SCALE_PRODUCTION_STRESS_BENCHMARK.md`` for the canonical
human-readable assumptions and provenance.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from hashlib import sha256
import json

from .dto import (
    CourseCategoryRelationshipDTO,
    CourseDifficultyDTO,
    CourseSequencePreferenceDTO,
    FixedStudentScheduleCommitmentDTO,
    OnlineSupervisionSessionDTO,
    StudentAssignmentInputDTO,
    StudentAssignmentRequestDTO,
    StudentAssignmentSectionDTO,
    StudentScheduleCommitmentRequestDTO,
    TimeSlotDTO,
)
from .student_assignment.runtime import semantic_student_assignment_input_fingerprint


BENCHMARK_ID = "paul_desmarais_shaped_g9_12_stress_v2"
BENCHMARK_VERSION = "v2"
BENCHMARK_STUDENT_COUNT = 1400
BENCHMARK_STUDENTS_PER_GRADE = 350
BENCHMARK_DEFAULT_LOCALE = "fr-CA"
BENCHMARK_ROTATION = (
    ("day_1", ("A", "B", "C", "D")),
    ("day_2", ("C", "D", "A", "B")),
    ("day_3", ("B", "A", "D", "C")),
    ("day_4", ("D", "C", "B", "A")),
)
PROVENANCE_CLASSES = frozenset({
    "verified_school_rule",
    "verified_ontario_rule",
    "verified_code_behavior",
    "synthetic_stress_assumption",
    "synthetic_coverage_case",
    "unknown_deferred_domain_fact",
})


@dataclass(frozen=True)
class BenchmarkAssumption:
    key: str
    value: object
    provenance: str
    rationale: str


@dataclass(frozen=True)
class BenchmarkCourse:
    course_id: int
    code: str
    name_fr: str
    name_en: str
    grade_level: int
    pathway: str
    category: str
    credit_value: float = 1.0


@dataclass(frozen=True)
class CoOpShapeCoverage:
    shape_id: str
    credit_value: float
    occupancy_description: str
    materialization_status: str
    provenance: str


@dataclass(frozen=True)
class PolicyExceptionCoverage:
    case_id: str
    grade_level: int
    commitment_type: str
    expected_review_code: str
    provenance: str


@dataclass(frozen=True)
class PaulDesmaraisStressFixture:
    benchmark_id: str
    benchmark_version: str
    input_data: StudentAssignmentInputDTO
    courses: tuple[BenchmarkCourse, ...]
    assumptions: tuple[BenchmarkAssumption, ...]
    co_op_shape_coverage: tuple[CoOpShapeCoverage, ...]
    policy_exception_coverage: tuple[PolicyExceptionCoverage, ...]
    fixture_fingerprint: str


def _course(course_id, code, name_fr, name_en, grade_level, pathway, category, credit_value=1.0):
    return BenchmarkCourse(
        course_id, code, name_fr, name_en, grade_level, pathway, category, credit_value,
    )


COURSES = (
    _course(1, "MTH1W", "Mathématiques", "Mathematics", 9, "W", "math"),
    _course(2, "SNC1W", "Sciences", "Science", 9, "W", "science"),
    _course(3, "FRA1W", "Français", "French", 9, "W", "language"),
    _course(4, "EAE1W", "Anglais", "English", 9, "W", "language"),
    _course(5, "CGC1W", "Enjeux géographiques du Canada", "Issues in Canadian Geography", 9, "W", "humanities"),
    _course(6, "PPL1O", "Éducation physique et santé", "Healthy Active Living Education", 9, "O", "humanities"),
    _course(7, "TIJ1O", "Exploration des technologies", "Exploring Technologies", 9, "O", "technology"),
    _course(8, "AVI1O", "Arts visuels", "Visual Arts", 9, "O", "arts"),
    _course(9, "MPM2D", "Principes de mathématiques", "Principles of Mathematics", 10, "D", "math"),
    _course(10, "MFM2P", "Méthodes de mathématiques", "Foundations of Mathematics", 10, "P", "math"),
    _course(11, "FRA2D", "Français", "French", 10, "D", "language"),
    _course(12, "EAE2D", "Anglais", "English", 10, "D", "language"),
    _course(13, "SNC2D", "Sciences", "Science", 10, "D", "science"),
    _course(14, "CHC2D", "Histoire du Canada depuis la Première Guerre mondiale", "Canadian History since World War I", 10, "D", "humanities"),
    _course(15, "CHV2O", "Civisme", "Civics and Citizenship", 10, "O", "humanities", 0.5),
    _course(16, "GLC2O", "Exploration de carrière", "Career Studies", 10, "O", "humanities", 0.5),
    _course(17, "MCF3M", "Modèles de fonctions", "Functions and Applications", 11, "M", "math"),
    _course(18, "MCR3U", "Fonctions", "Functions", 11, "U", "math"),
    _course(19, "SBI3U", "Biologie", "Biology", 11, "U", "science"),
    _course(20, "SCH3U", "Chimie", "Chemistry", 11, "U", "science"),
    _course(21, "SPH3U", "Physique", "Physics", 11, "U", "science"),
    _course(22, "BAF3M", "Introduction à la comptabilité financière", "Introduction to Financial Accounting", 11, "M", "business"),
    _course(23, "ICS3U", "Introduction à l'informatique", "Introduction to Computer Science", 11, "U", "technology"),
    _course(24, "HSP3U", "Introduction à la psychologie, à la sociologie et à l'anthropologie", "Introduction to Anthropology, Psychology, and Sociology", 11, "U", "humanities"),
    _course(25, "PPL3O", "Éducation physique et santé", "Healthy Active Living Education", 11, "O", "humanities"),
    _course(26, "AMU3M", "Musique", "Music", 11, "M", "arts"),
    _course(27, "TCJ3C", "Technologie des communications", "Communications Technology", 11, "C", "technology"),
    _course(28, "MHF4U", "Fonctions avancées", "Advanced Functions", 12, "U", "math"),
    _course(29, "MCV4U", "Calcul différentiel et vecteurs", "Calculus and Vectors", 12, "U", "math"),
    _course(30, "MDM4U", "Mathématiques de la gestion des données", "Mathematics of Data Management", 12, "U", "math"),
    _course(31, "SBI4U", "Biologie", "Biology", 12, "U", "science"),
    _course(32, "SCH4U", "Chimie", "Chemistry", 12, "U", "science"),
    _course(33, "SPH4U", "Physique", "Physics", 12, "U", "science"),
    _course(34, "BOH4M", "Principes de gestion", "Business Leadership", 12, "M", "business"),
    _course(35, "ICS4U", "Informatique", "Computer Science", 12, "U", "technology"),
    _course(36, "HHS4U", "Individus et familles au Canada", "Individuals and Families in a Diverse Society", 12, "U", "humanities"),
    _course(37, "HFA4U", "Nutrition et santé", "Nutrition and Health", 12, "U", "humanities"),
    _course(38, "PPL4O", "Éducation physique et santé", "Healthy Active Living Education", 12, "O", "humanities"),
    _course(39, "AMU4M", "Musique", "Music", 12, "M", "arts"),
    _course(40, "TCJ4C", "Technologie des communications", "Communications Technology", 12, "C", "technology"),
    _course(41, "COOP2", "Programme coopératif de deux crédits", "Two-credit Co-op Program", 11, "", "", 2.0),
)
COURSE_BY_ID = {course.course_id: course for course in COURSES}


def _assumptions():
    return (
        BenchmarkAssumption("benchmark_student_count", 1400, "synthetic_stress_assumption", "Upper-bound Grades 9--12 stress scale."),
        BenchmarkAssumption("students_per_grade", 350, "synthetic_stress_assumption", "Deterministic even grade coverage."),
        BenchmarkAssumption("active_grades", (9, 10, 11, 12), "verified_school_rule", "V1 scheduling scope excludes Grades 7--8."),
        BenchmarkAssumption("term_count", 2, "verified_school_rule", "Supported v1 semester model."),
        BenchmarkAssumption("instructional_blocks_per_term", 4, "verified_school_rule", "Current Paul-Desmarais profile."),
        BenchmarkAssumption("cycle_rotation", BENCHMARK_ROTATION, "verified_code_behavior", "Repository-verified A--D realization."),
        BenchmarkAssumption("chv2o_glc2o_credit_value", 0.5, "verified_ontario_rule", "Each member of the supported Ontario half-course pair is 0.5 credit."),
        BenchmarkAssumption("study_normal_grades", (12,), "verified_school_rule", "Normal fixture policy, not a hard solver law."),
        BenchmarkAssumption("study_request_count", 70, "synthetic_stress_assumption", "Stress-density coverage, not measured prevalence."),
        BenchmarkAssumption("focus_normal_grades", (11, 12), "verified_school_rule", "Normal fixture policy."),
        BenchmarkAssumption("focus_request_count", 28, "synthetic_stress_assumption", "Stress-density coverage, not measured prevalence."),
        BenchmarkAssumption("connected_two_credit_co_op_request_count", 42, "synthetic_stress_assumption", "Current movable Co-op shape coverage."),
        BenchmarkAssumption("online_request_count", 84, "synthetic_stress_assumption", "Stress-density coverage, not measured prevalence."),
        BenchmarkAssumption("half_pair_upper_grade_count", 5, "synthetic_coverage_case", "Transfer/recovery-shaped exception coverage."),
        BenchmarkAssumption("sequence_eligible_opportunities", 56, "synthetic_coverage_case", "Stable non-vacuous MCF3M/MCR3U coverage."),
        BenchmarkAssumption("focus_internal_credit_accounting", None, "unknown_deferred_domain_fact", "Excluded from ordinary objectives until authoritative accounting exists."),
        BenchmarkAssumption("study_maximum", None, "unknown_deferred_domain_fact", "No verified hard maximum or review threshold."),
        BenchmarkAssumption("category_relationships", "synthetic_default_relationships", "synthetic_stress_assumption", "Not counselor-reviewed values."),
    )


def _metadata_difficulty(course: BenchmarkCourse) -> int:
    designation_adjustments = {"U": 10, "M": 6, "C": 0, "E": -8, "D": 4, "P": -3, "W": 0, "O": -6, "L": -10}
    category_adjustments = {"math": 3, "science": 3, "language": 1, "humanities": 1, "technology": 0, "business": 0, "arts": -1}
    grade_score = 20 + round((course.grade_level - 7) * 60 / 5)
    designation = course.code[-1] if course.code and course.code[-1].isalpha() else ""
    return max(0, min(100, int(round(
        grade_score + designation_adjustments.get(designation, 0) + category_adjustments.get(course.category, 0)
    ))))


def _grade_for_student(student_id: int) -> int:
    return 9 + ((student_id - 1) // BENCHMARK_STUDENTS_PER_GRADE)


def _request(request_id, student_id, course_id, *, delivery_kind="normal_instruction", duration="full_semester", credit_value=1.0, half_semester_segment=None, paired_half_course_id=None):
    return StudentAssignmentRequestDTO(
        request_id=request_id,
        student_id=student_id,
        course_id=course_id,
        course_offering_id=1000 + course_id,
        is_primary=True,
        is_mandatory=True,
        priority_tier=1,
        delivery_kind=delivery_kind,
        duration=duration,
        credit_value=credit_value,
        half_semester_segment=half_semester_segment,
        paired_half_course_id=paired_half_course_id,
    )


def _base_courses(grade_level: int) -> list[int]:
    if grade_level == 9:
        return list(range(1, 9))
    if grade_level == 10:
        return [9, 10, 11, 12, 13, 14, 7, 8]
    if grade_level == 11:
        return [19, 20, 21, 22, 23, 24, 25, 26]
    return [28, 29, 30, 31, 32, 33, 34, 35]


def _section(section_id, course_id, semester, timeslot_id, *, half_semester_segment=None, half_semester_pair_key=None):
    return StudentAssignmentSectionDTO(
        section_id=section_id,
        delivery_group_id=course_id,
        member_course_offering_ids=(1000 + course_id,),
        member_course_ids=(course_id,),
        semester=semester,
        timeslot_id=timeslot_id,
        capacity_max=900,
        target_capacity=800,
        half_semester_segment=half_semester_segment,
        half_semester_pair_key=half_semester_pair_key,
    )


def _fixture_fingerprint(input_data, assumptions, co_op_shape_coverage, policy_exception_coverage):
    payload = {
        "benchmark_id": BENCHMARK_ID,
        "benchmark_version": BENCHMARK_VERSION,
        "input_fingerprint": semantic_student_assignment_input_fingerprint(input_data),
        "courses": [asdict(course) for course in COURSES],
        "assumptions": [asdict(item) for item in assumptions],
        "co_op_shape_coverage": [asdict(item) for item in co_op_shape_coverage],
        "policy_exception_coverage": [asdict(item) for item in policy_exception_coverage],
    }
    return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def build_paul_desmarais_shaped_g9_12_stress_fixture() -> PaulDesmaraisStressFixture:
    """Build the deterministic fixture without constructing or solving CP-SAT."""

    grade_students = {grade: list(range(1 + (grade - 9) * 350, 1 + (grade - 8) * 350)) for grade in range(9, 13)}
    study_once = set(range(1051, 1091)) | {1122, 1123}
    study_twice = set(range(1091, 1105))
    focus_students = set(range(701, 715)) | set(range(1105, 1119))
    co_op_students = {1, 351} | set(range(715, 735)) | set(range(1121, 1141))
    online_students = {
        *range(1, 22),
        *range(352, 373),
        701, 715,
        *range(735, 754),
        1051, 1121, 1122,
        *range(1141, 1159),
    }
    half_pair_students = set(range(352, 697)) | {735, 736, 1141, 1142, 1143}
    sequence_students = set(range(755, 811))

    if len(study_once) != 42 or len(study_twice) != 14 or len(focus_students) != 28:
        raise AssertionError("Stress fixture special-program constants are inconsistent.")
    if len(co_op_students) != 42 or len(online_students) != 84 or len(half_pair_students) != 350:
        raise AssertionError("Stress fixture coverage constants are inconsistent.")
    if len(sequence_students) != 56:
        raise AssertionError("Stress fixture sequence coverage is inconsistent.")

    requests = []
    commitments = []
    next_request_id = 1
    next_commitment_id = 100000

    for student_id in range(1, BENCHMARK_STUDENT_COUNT + 1):
        grade_level = _grade_for_student(student_id)
        course_ids = _base_courses(grade_level)
        if student_id in focus_students:
            course_ids = course_ids[:4]
        if student_id in co_op_students:
            course_ids = course_ids[:6]
        if student_id in study_once:
            course_ids = course_ids[:-1]
        if student_id in study_twice:
            course_ids = course_ids[:-2]
        if student_id in sequence_students:
            course_ids = course_ids[:-2] + [17, 18]
        if student_id in half_pair_students:
            course_ids = course_ids[:-1] + [15, 16]

        online_course_id = course_ids[0] if student_id in online_students else None
        for course_id in course_ids:
            course = COURSE_BY_ID[course_id]
            requests.append(_request(
                next_request_id,
                student_id,
                course_id,
                delivery_kind="online" if course_id == online_course_id else "normal_instruction",
                duration="half_semester" if course_id in {15, 16} else "full_semester",
                credit_value=course.credit_value,
                half_semester_segment=("first_half" if course_id == 15 else "second_half" if course_id == 16 else None),
                paired_half_course_id=(16 if course_id == 15 else 15 if course_id == 16 else None),
            ))
            next_request_id += 1

        if student_id in co_op_students:
            requests.append(_request(
                next_request_id, student_id, 41, delivery_kind="co_op", credit_value=2.0,
            ))
            next_request_id += 1
        for _index in range(1 if student_id in study_once else 2 if student_id in study_twice else 0):
            commitments.append(StudentScheduleCommitmentRequestDTO(
                request_id=next_commitment_id, student_id=student_id, commitment_type="study",
            ))
            next_commitment_id += 1
        if student_id in focus_students:
            commitments.append(StudentScheduleCommitmentRequestDTO(
                request_id=next_commitment_id, student_id=student_id, commitment_type="focus",
            ))
            next_commitment_id += 1

    timeslots = tuple(
        TimeSlotDTO(
            id=slot_id,
            academic_year_id=1,
            semester=1 if slot_id <= 4 else 2,
            block=("A", "B", "C", "D")[(slot_id - 1) % 4],
        )
        for slot_id in range(1, 9)
    )
    sections = []
    section_id = 1
    for course in COURSES:
        if course.course_id in {15, 16, 41}:
            continue
        for semester in (1, 2):
            sections.append(_section(section_id, course.course_id, semester, 1 + ((course.course_id + semester) % 4) + (semester - 1) * 4))
            section_id += 1
    for semester, timeslot_id in ((1, 1), (2, 5)):
        pair_key = f"chv_glc_s{semester}"
        sections.append(_section(section_id, 15, semester, timeslot_id, half_semester_segment="first_half", half_semester_pair_key=pair_key))
        section_id += 1
        sections.append(_section(section_id, 16, semester, timeslot_id, half_semester_segment="second_half", half_semester_pair_key=pair_key))
        section_id += 1

    online_sessions = tuple(
        OnlineSupervisionSessionDTO(
            session_id=slot.id,
            semester=slot.semester,
            timeslot_id=slot.id,
            capacity_max=900,
            target_capacity=800,
        )
        for slot in timeslots
    )
    online_course_ids = tuple(course.course_id for course in COURSES if course.course_id not in {15, 16, 41})
    for slot in timeslots:
        sections.append(StudentAssignmentSectionDTO(
            section_id=-slot.id,
            delivery_group_id=-slot.id,
            member_course_offering_ids=tuple(1000 + course_id for course_id in online_course_ids),
            member_course_ids=online_course_ids,
            semester=slot.semester,
            timeslot_id=slot.id,
            capacity_max=900,
            target_capacity=800,
        ))

    difficulties = tuple(
        CourseDifficultyDTO(
            course_id=course.course_id,
            category=course.category,
            calculated_difficulty=_metadata_difficulty(course),
            manual_difficulty_override=None,
            effective_difficulty=_metadata_difficulty(course),
            calculation_version="metadata_and_relative_history_v2",
            source="metadata",
            metadata_difficulty=_metadata_difficulty(course),
            designation=course.code[-1] if course.code[-1].isalpha() else "",
            historical_observation_count=0,
            historical_year_count=0,
            historical_confidence=0.0,
        )
        for course in COURSES
    )
    input_data = StudentAssignmentInputDTO(
        academic_year_id=1,
        requests=tuple(requests),
        sections=tuple(sections),
        fixed_enrollments=(),
        hard_prerequisites=(),
        soft_sequence_preferences=(CourseSequencePreferenceDTO(17, 18),),
        section_utilization_balance_importance="important",
        student_semester_balance_importance="important",
        course_sequence_preferences_importance="important",
        difficulty_balance_importance="important",
        course_category_diversity_importance="important",
        objective_semantics_version="v2",
        objective_importance_scores={
            "section_utilization_balance": 6,
            "student_semester_balance": 6,
            "course_sequence_preferences": 6,
            "difficulty_balance": 6,
            "course_category_diversity": 6,
        },
        course_difficulties=difficulties,
        course_category_relationships=(
            CourseCategoryRelationshipDTO("math", "science", 45),
            CourseCategoryRelationshipDTO("language", "arts", 30),
        ),
        online_supervision_sessions=online_sessions,
        schedule_commitment_requests=tuple(commitments),
        special_commitment_locks=(),
        timeslots=timeslots,
        student_grades=tuple((student_id, _grade_for_student(student_id)) for student_id in range(1, 1401)),
    )
    co_op_shape_coverage = (
        CoOpShapeCoverage("one_credit_fixed_context", 1.0, "one occupied instructional position", "fixed_context_supported", "synthetic_coverage_case"),
        CoOpShapeCoverage("connected_two_credit", 2.0, "A+B or C+D in one term", "movable_supported_current_v1", "verified_school_rule"),
        CoOpShapeCoverage("two_independent_one_credit", 2.0, "two independent one-position requests", "movable_unsupported_current_v1", "synthetic_coverage_case"),
        CoOpShapeCoverage("flexible_two_credit", 2.0, "two same-term positions without connected-pair rule", "movable_unsupported_current_v1", "synthetic_coverage_case"),
        CoOpShapeCoverage("four_credit_fixed_context", 4.0, "all four instructional blocks in one term", "fixed_context_supported", "synthetic_coverage_case"),
    )
    policy_exception_coverage = (
        PolicyExceptionCoverage("grade_11_study", 11, "study", "STUDY_OUTSIDE_NORMAL_GRADE_POLICY", "synthetic_coverage_case"),
        PolicyExceptionCoverage("grade_10_focus", 10, "focus", "FOCUS_OUTSIDE_NORMAL_GRADE_POLICY", "synthetic_coverage_case"),
    )
    assumptions = _assumptions()
    return PaulDesmaraisStressFixture(
        BENCHMARK_ID,
        BENCHMARK_VERSION,
        input_data,
        COURSES,
        assumptions,
        co_op_shape_coverage,
        policy_exception_coverage,
        _fixture_fingerprint(input_data, assumptions, co_op_shape_coverage, policy_exception_coverage),
    )


def summarize_paul_desmarais_shaped_g9_12_stress_fixture(fixture: PaulDesmaraisStressFixture):
    """Return a compact deterministic audit without invoking the solver."""

    data = fixture.input_data
    grades = dict(sorted(Counter(dict(data.student_grades).values()).items()))
    requests_by_student = defaultdict(list)
    for request in data.requests:
        requests_by_student[request.student_id].append(request)
    commitments_by_student = defaultdict(list)
    for commitment in data.schedule_commitment_requests:
        commitments_by_student[commitment.student_id].append(commitment.commitment_type)

    positions = {}
    credits = {}
    for student_id in range(1, BENCHMARK_STUDENT_COUNT + 1):
        rows = requests_by_student[student_id]
        paired = set()
        occupied = 0
        credit_total = 0.0
        for request in rows:
            credit_total += request.credit_value
            if request.delivery_kind == "co_op":
                occupied += 2
            elif request.duration == "half_semester" and request.paired_half_course_id:
                pair = tuple(sorted((request.course_id, request.paired_half_course_id)))
                if pair not in paired:
                    paired.add(pair)
                    occupied += 1
            else:
                occupied += 1
        for commitment_type in commitments_by_student[student_id]:
            occupied += 4 if commitment_type == "focus" else 1
        positions[student_id] = occupied
        credits[student_id] = round(credit_total, 1)

    course_by_id = {course.course_id: course for course in fixture.courses}
    co_op_by_grade = Counter(
        dict(data.student_grades)[request.student_id]
        for request in data.requests if request.delivery_kind == "co_op"
    )
    online_by_grade = Counter(
        dict(data.student_grades)[request.student_id]
        for request in data.requests if request.delivery_kind == "online"
    )
    focus_by_grade = Counter(
        dict(data.student_grades)[commitment.student_id]
        for commitment in data.schedule_commitment_requests if commitment.commitment_type == "focus"
    )
    study_by_grade = Counter(
        dict(data.student_grades)[commitment.student_id]
        for commitment in data.schedule_commitment_requests if commitment.commitment_type == "study"
    )
    half_pair_students = {
        request.student_id for request in data.requests
        if request.course_id == 15 and request.paired_half_course_id == 16
    }
    sequence_eligible = sum(
        {17, 18}.issubset({request.course_id for request in rows})
        for rows in requests_by_student.values()
    )
    combinations = Counter()
    for student_id, rows in requests_by_student.items():
        kinds = set(commitments_by_student[student_id])
        if any(row.delivery_kind == "co_op" for row in rows):
            kinds.add("co_op")
        if any(row.delivery_kind == "online" for row in rows):
            kinds.add("online")
        if len(kinds) > 1:
            combinations["+".join(sorted(kinds))] += 1
    category_counts = Counter(
        course_by_id[request.course_id].category
        for request in data.requests
        if course_by_id[request.course_id].category
    )
    pathway_counts = Counter(
        course_by_id[request.course_id].pathway
        for request in data.requests
        if course_by_id[request.course_id].pathway
    )
    course_code_counts = Counter(course_by_id[request.course_id].code for request in data.requests)
    provenance_counts = Counter(item.provenance for item in fixture.assumptions)
    return {
        "benchmark_id": fixture.benchmark_id,
        "benchmark_version": fixture.benchmark_version,
        "statement": "Synthetic 1,400-student Grades 9-12 production STRESS benchmark; not measured Paul-Desmarais enrollment or prevalence data.",
        "fixture_fingerprint": fixture.fixture_fingerprint,
        "input_fingerprint": semantic_student_assignment_input_fingerprint(data),
        "student_count": len(data.student_grades),
        "grade_distribution": grades,
        "term_count": 2,
        "instructional_blocks_per_term": 4,
        "rotation": {day: list(blocks) for day, blocks in BENCHMARK_ROTATION},
        "occupied_position_distribution": dict(sorted(Counter(positions.values()).items())),
        "credit_distribution_excluding_focus_internal_credit": dict(sorted(Counter(credits.values()).items())),
        "study_request_count": sum(study_by_grade.values()),
        "study_by_grade": dict(sorted(study_by_grade.items())),
        "focus_request_count": sum(focus_by_grade.values()),
        "focus_by_grade": dict(sorted(focus_by_grade.items())),
        "co_op_request_count": sum(co_op_by_grade.values()),
        "co_op_by_grade": dict(sorted(co_op_by_grade.items())),
        "online_request_count": sum(online_by_grade.values()),
        "online_by_grade": dict(sorted(online_by_grade.items())),
        "half_pair_student_count": len(half_pair_students),
        "half_pair_upper_grade_count": sum(dict(data.student_grades)[student_id] in {11, 12} for student_id in half_pair_students),
        "half_course_credit_values": dict(sorted(Counter(request.credit_value for request in data.requests if request.duration == "half_semester").items())),
        "special_program_combinations": dict(sorted(combinations.items())),
        "sequence_preference": {"earlier": "MCF3M", "later": "MCR3U", "eligible_opportunities": sequence_eligible, "provenance": "synthetic_coverage_case"},
        "category_request_counts": dict(sorted(category_counts.items())),
        "pathway_request_counts": dict(sorted(pathway_counts.items())),
        "course_code_request_counts": dict(sorted(course_code_counts.items())),
        "difficulty_provenance": {"calculation_version": "metadata_and_relative_history_v2", "source": "metadata", "historical_observation_count": 0, "historical_confidence": 0.0, "study_intended_future_domain_difficulty": 0},
        "category_relationship_provenance": "synthetic_default_relationships",
        "provenance_class_counts": dict(sorted(provenance_counts.items())),
        "co_op_shape_coverage": [asdict(item) for item in fixture.co_op_shape_coverage],
        "policy_exception_coverage": [asdict(item) for item in fixture.policy_exception_coverage],
    }


def fixed_context_co_op_coverage():
    """Return safe fixed-context representations; they are not movable requests."""

    return (
        FixedStudentScheduleCommitmentDTO(
            commitment_id=1, student_id=1, commitment_kind="co_op", credit_value=1.0,
            occupancy=((1, "first_half"), (1, "second_half")),
        ),
        FixedStudentScheduleCommitmentDTO(
            commitment_id=2, student_id=2, commitment_kind="co_op", credit_value=4.0,
            occupancy=tuple((slot_id, segment) for slot_id in range(1, 5) for segment in ("first_half", "second_half")),
        ),
    )


if __name__ == "__main__":
    print(json.dumps(
        summarize_paul_desmarais_shaped_g9_12_stress_fixture(
            build_paul_desmarais_shaped_g9_12_stress_fixture()
        ),
        sort_keys=True,
        separators=(",", ":"),
    ))
