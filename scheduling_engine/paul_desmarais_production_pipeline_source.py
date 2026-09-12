"""Deterministic upstream source specification for the Paul-Desmarais pipeline.

This module deliberately stops at school/SIS source facts.  It creates no
Sections, SectionSchedules, online-supervision sessions, teacher assignments,
enrollments, or solver output.  Those are materialized by the production
workflow qualification harness.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json

from .paul_desmarais_stress_benchmark import (
    COURSE_BY_ID,
    COURSES,
    _base_courses,
)


LINEAGE_ID = "paul_desmarais_shaped_g9_12_production_pipeline_v1"
SOURCE_VERSION = "v1"
SOURCE_SEED = 20260912
STUDENT_COUNT = 1400
STUDENTS_PER_GRADE = 350
GRADES = (9, 10, 11, 12)
ONLINE_SOURCE_COURSE_IDS = (1, 9, 19, 28)


@dataclass(frozen=True)
class SourceCourse:
    key: str
    code: str
    name_fr: str
    name_en: str
    grade_level: int
    pathway: str
    category: str
    delivery_kind: str = "normal_instruction"
    duration: str = "full_semester"
    credit_value: float = 1.0
    allowed_semester: str = "either_semester"
    qualification_key: str | None = None
    source_course_id: int | None = None


@dataclass(frozen=True)
class SourceRequest:
    student_number: int
    course_key: str
    is_mandatory: bool = True


@dataclass(frozen=True)
class SourceCommitmentRequest:
    student_number: int
    commitment_type: str
    request_index: int = 1


@dataclass(frozen=True)
class SourceSpec:
    lineage_id: str
    version: str
    seed: int
    academic_year_name: str
    students: tuple[tuple[int, int], ...]
    courses: tuple[SourceCourse, ...]
    requests: tuple[SourceRequest, ...]
    commitment_requests: tuple[SourceCommitmentRequest, ...]
    assumptions: tuple[tuple[str, object, str], ...]
    fingerprint: str


def _special_sets():
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
    assert len(study_once) == 42
    assert len(study_twice) == 14
    assert len(focus_students) == 28
    assert len(co_op_students) == 42
    assert len(online_students) == 84
    assert len(half_pair_students) == 350
    assert len(sequence_students) == 56
    return {
        "study_once": study_once,
        "study_twice": study_twice,
        "focus": focus_students,
        "co_op": co_op_students,
        "online": online_students,
        "half_pair": half_pair_students,
        "sequence": sequence_students,
    }


def _course_specs() -> tuple[SourceCourse, ...]:
    specs = []
    for course in COURSES:
        delivery_kind = "co_op" if course.course_id == 41 else "normal_instruction"
        duration = "half_semester" if course.course_id in {15, 16} else "full_semester"
        specs.append(SourceCourse(
            key=f"course:{course.course_id}",
            code=course.code,
            name_fr=course.name_fr,
            name_en=course.name_en,
            grade_level=course.grade_level,
            pathway=course.pathway,
            category=course.category,
            delivery_kind=delivery_kind,
            duration=duration,
            credit_value=course.credit_value,
            qualification_key=(None if delivery_kind == "co_op" else f"qual:{(course.course_id - 1) % 11}"),
            source_course_id=course.course_id,
        ))
    for source_course_id in ONLINE_SOURCE_COURSE_IDS:
        course = COURSE_BY_ID[source_course_id]
        specs.append(SourceCourse(
            key=f"online:{source_course_id}",
            code=f"{course.code}-ONL",
            name_fr=f"{course.name_fr} en ligne",
            name_en=f"{course.name_en} online",
            grade_level=course.grade_level,
            pathway=course.pathway,
            category=course.category,
            delivery_kind="online",
            duration="full_semester",
            credit_value=1.0,
            qualification_key=None,
            source_course_id=source_course_id,
        ))
    return tuple(specs)


def _build_requests(course_by_key, special):
    requests = []
    commitments = []
    for student_number in range(1, STUDENT_COUNT + 1):
        grade = 9 + ((student_number - 1) // STUDENTS_PER_GRADE)
        course_ids = list(_base_courses(grade))
        if student_number in special["focus"]:
            course_ids = course_ids[:4]
        if student_number in special["co_op"]:
            course_ids = course_ids[:6]
        if student_number in special["study_once"]:
            course_ids = course_ids[:-1]
        if student_number in special["study_twice"]:
            course_ids = course_ids[:-2]
        if student_number in special["sequence"]:
            course_ids = course_ids[:-2] + [17, 18]
        if student_number in special["half_pair"]:
            course_ids = course_ids[:-1] + [15, 16]

        online_source_id = course_ids[0] if student_number in special["online"] else None
        for course_id in course_ids:
            key = f"online:{course_id}" if course_id == online_source_id else f"course:{course_id}"
            if key not in course_by_key:
                raise AssertionError(f"Missing source course for {key}")
            requests.append(SourceRequest(student_number=student_number, course_key=key))
        if student_number in special["co_op"]:
            requests.append(SourceRequest(student_number=student_number, course_key="course:41"))
        for index in range(
            1 if student_number in special["study_once"]
            else 2 if student_number in special["study_twice"]
            else 0
        ):
            commitments.append(SourceCommitmentRequest(
                student_number=student_number,
                commitment_type="study",
                request_index=index + 1,
            ))
        if student_number in special["focus"]:
            commitments.append(SourceCommitmentRequest(
                student_number=student_number,
                commitment_type="focus",
                request_index=1,
            ))
    return tuple(requests), tuple(commitments)


def build_source_spec() -> SourceSpec:
    special = _special_sets()
    courses = _course_specs()
    course_by_key = {course.key: course for course in courses}
    requests, commitments = _build_requests(course_by_key, special)
    students = tuple(
        (student_number, 9 + ((student_number - 1) // STUDENTS_PER_GRADE))
        for student_number in range(1, STUDENT_COUNT + 1)
    )
    assumptions = (
        ("student_count", STUDENT_COUNT, "synthetic_stress_assumption"),
        ("students_per_grade", STUDENTS_PER_GRADE, "synthetic_stress_assumption"),
        ("active_grades", GRADES, "verified_school_rule"),
        ("term_count", 2, "verified_school_rule"),
        ("instructional_blocks_per_term", 4, "verified_school_rule"),
        ("study_request_count", len(special["study_once"]) + 2 * len(special["study_twice"]), "synthetic_stress_assumption"),
        ("focus_request_count", len(special["focus"]), "synthetic_stress_assumption"),
        ("connected_two_credit_co_op_count", len(special["co_op"]), "synthetic_stress_assumption"),
        ("online_course_choice_count", len(special["online"]), "synthetic_stress_assumption"),
        ("paired_half_course_count", len(special["half_pair"]), "synthetic_stress_assumption"),
        ("upper_grade_half_course_coverage_count", 5, "synthetic_coverage_case"),
        ("sequence_eligible_opportunity_count", len(special["sequence"]), "synthetic_coverage_case"),
        ("focus_internal_credit_accounting", None, "unknown_deferred_domain_fact"),
        ("prerequisite_waiver_representation", None, "unknown_deferred_domain_fact"),
    )
    payload = {
        "lineage_id": LINEAGE_ID,
        "version": SOURCE_VERSION,
        "seed": SOURCE_SEED,
        "academic_year_name": "2037-2038",
        "students": students,
        "courses": [asdict(item) for item in courses],
        "requests": [asdict(item) for item in requests],
        "commitment_requests": [asdict(item) for item in commitments],
        "assumptions": assumptions,
    }
    fingerprint = sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()
    return SourceSpec(
        lineage_id=LINEAGE_ID,
        version=SOURCE_VERSION,
        seed=SOURCE_SEED,
        academic_year_name="2037-2038",
        students=students,
        courses=courses,
        requests=requests,
        commitment_requests=commitments,
        assumptions=assumptions,
        fingerprint=fingerprint,
    )


def source_summary(spec: SourceSpec) -> dict:
    grade_counts = {}
    for _student_number, grade in spec.students:
        grade_counts[grade] = grade_counts.get(grade, 0) + 1
    course_counts = {}
    for request in spec.requests:
        course_counts[request.course_key] = course_counts.get(request.course_key, 0) + 1
    commitment_counts = {}
    for request in spec.commitment_requests:
        commitment_counts[request.commitment_type] = commitment_counts.get(request.commitment_type, 0) + 1
    return {
        "lineage_id": spec.lineage_id,
        "version": spec.version,
        "seed": spec.seed,
        "fingerprint": spec.fingerprint,
        "student_count": len(spec.students),
        "grade_counts": dict(sorted(grade_counts.items())),
        "request_count": len(spec.requests),
        "commitment_request_count": len(spec.commitment_requests),
        "commitment_counts": dict(sorted(commitment_counts.items())),
        "online_course_choice_count": sum(
            request.course_key.startswith("online:") for request in spec.requests
        ),
        "paired_half_course_student_count": sum(
            1 for student_number, _grade in spec.students
            if sum(
                request.student_number == student_number
                and request.course_key in {"course:15", "course:16"}
                for request in spec.requests
            ) == 2
        ),
        "course_request_counts": dict(sorted(course_counts.items())),
    }

