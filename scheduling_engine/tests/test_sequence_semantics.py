"""Small, deterministic qualification tests for v2 soft-sequence semantics."""

from dataclasses import replace

import pytest

from scheduling_engine.dto import CoursePrerequisiteDTO, CourseSequencePreferenceDTO
from scheduling_engine.realistic_student_assignment_validation import (
    _request,
    _section,
    build_realistic_quality_tradeoff_fixture,
)
from scheduling_engine.student_assignment import solve_student_assignment
from scheduling_engine.student_assignment.quality import evaluate_student_assignment_quality


OBJECTIVE_KEYS = (
    "section_utilization_balance",
    "student_semester_balance",
    "course_sequence_preferences",
    "difficulty_balance",
    "course_category_diversity",
)


def _scores(**overrides):
    values = {key: 0 for key in OBJECTIVE_KEYS}
    values.update(overrides)
    return values


def _sequence_fixture(*, course_semesters, student_count=1, preferences=((1, 2),)):
    """Build a tiny complete input with explicitly controlled course terms."""

    requests = []
    request_id = 1
    course_ids = set(course_semesters)
    course_ids.update(course for pair in preferences for course in pair)
    for student_id in range(1, student_count + 1):
        for course_id in sorted(course_ids):
            # Unrelated students deliberately request only course 3.
            if student_id > 1 and course_id != 3:
                continue
            requests.append(_request(
                request_id, student_id, course_id, is_mandatory=True,
            ))
            request_id += 1
    sections = []
    section_id = 1
    for course_id, semesters in sorted(course_semesters.items()):
        for semester in semesters:
            sections.append(_section(
                section_id, course_id, semester, section_id, 50,
            ))
            section_id += 1
    return replace(
        build_realistic_quality_tradeoff_fixture(),
        requests=tuple(requests),
        sections=tuple(sections),
        fixed_enrollments=(),
        hard_prerequisites=(),
        soft_sequence_preferences=tuple(
            CourseSequencePreferenceDTO(earlier, later)
            for earlier, later in preferences
        ),
        course_difficulties=(),
        objective_semantics_version="v2",
        objective_importance_scores=_scores(course_sequence_preferences=10),
    )


def _sequence_facts(result):
    return result.objective_components["normalization"][
        "course_sequence_preferences_penalty"
    ], result.objective_components["normalized_components"][
        "course_sequence_preferences_penalty"
    ]


def test_sequence_case_1_satisfied_and_case_2_violated_have_exact_v2_penalties():
    satisfied = solve_student_assignment(_sequence_fixture(
        course_semesters={1: (1,), 2: (2,)},
    ))
    violated = solve_student_assignment(_sequence_fixture(
        course_semesters={1: (2,), 2: (1,)},
    ))

    satisfied_scale, satisfied_penalty = _sequence_facts(satisfied)
    violated_scale, violated_penalty = _sequence_facts(violated)
    assert satisfied.status == violated.status == "complete"
    assert satisfied.objective_components["soft_sequence_preferences_satisfied"] == 1
    assert satisfied_scale["denominator"] == violated_scale["denominator"] == 1
    assert satisfied_penalty == 0
    assert violated.objective_components["soft_sequence_preferences_satisfied"] == 0
    assert violated_penalty == 10_000
    report = evaluate_student_assignment_quality(
        _sequence_fixture(course_semesters={1: (2,), 2: (1,)}),
        assignments=violated.assignments,
        commitment_assignments=violated.commitment_assignments,
        solver_objective_components=violated.objective_components,
    )
    assert report["objective_semantics"]["components"][
        "course_sequence_preferences"
    ]["raw_penalty"] == 1


def test_sequence_case_3_not_applicable_does_not_enter_the_denominator():
    data = _sequence_fixture(
        course_semesters={1: (1,), 2: (2,)},
        preferences=((1, 2),),
    )
    data = replace(data, requests=(data.requests[0],))
    result = solve_student_assignment(data)
    scale, penalty = _sequence_facts(result)
    assert result.status == "complete"
    assert scale["denominator"] == 0
    assert penalty == 0


def test_sequence_case_4_is_soft_and_can_lose_to_a_larger_weighted_quality_gain():
    base = build_realistic_quality_tradeoff_fixture()
    data = replace(
        base,
        objective_semantics_version="v2",
        soft_sequence_preferences=(CourseSequencePreferenceDTO(1, 2),),
        objective_importance_scores=_scores(
            course_sequence_preferences=1,
            difficulty_balance=10,
        ),
    )
    result = solve_student_assignment(data)
    semesters = {item.course_id: item.semester for item in result.assignments}
    assert result.status == "complete"
    # The preferred 1 -> 2 order loses to the much larger difficulty gain.
    assert semesters[1] == 2
    assert result.objective_components["soft_sequence_preferences_satisfied"] == 0


def test_sequence_case_5_hard_prerequisite_cannot_be_traded_away():
    base = build_realistic_quality_tradeoff_fixture()
    data = replace(
        base,
        objective_semantics_version="v2",
        hard_prerequisites=(CoursePrerequisiteDTO(course_id=2, prerequisite_id=1),),
        soft_sequence_preferences=(CourseSequencePreferenceDTO(1, 2),),
        objective_importance_scores=_scores(
            course_sequence_preferences=1,
            difficulty_balance=10,
        ),
    )
    result = solve_student_assignment(data)
    semesters = {item.course_id: item.semester for item in result.assignments}
    assert result.status == "complete"
    assert semesters == {1: 1, 2: 2}
    assert result.objective_components["soft_sequence_preferences_satisfied"] == 1


def test_sequence_case_6_unrelated_students_do_not_dilute_a_violated_opportunity():
    baseline = solve_student_assignment(_sequence_fixture(
        course_semesters={1: (2,), 2: (1,), 3: (1,)},
    ))
    population = solve_student_assignment(_sequence_fixture(
        course_semesters={1: (2,), 2: (1,), 3: (1,)},
        student_count=25,
    ))
    baseline_scale, baseline_penalty = _sequence_facts(baseline)
    population_scale, population_penalty = _sequence_facts(population)
    assert baseline_scale["denominator"] == population_scale["denominator"] == 1
    assert baseline_penalty == population_penalty == 10_000


def test_sequence_case_7_multiple_opportunities_are_scored_per_opportunity():
    result = solve_student_assignment(_sequence_fixture(
        course_semesters={1: (1,), 2: (2,), 3: (1,)},
        preferences=((1, 2), (2, 3)),
    ))
    scale, penalty = _sequence_facts(result)
    assert result.status == "complete"
    assert scale["denominator"] == 2
    assert result.objective_components["soft_sequence_preferences_satisfied"] == 1
    assert penalty == 5_000


def test_provisional_v3_objective_semantics_are_not_a_supported_engine_version():
    data = replace(build_realistic_quality_tradeoff_fixture(), objective_semantics_version="v3")
    with pytest.raises(ValueError, match="Unsupported student-assignment objective semantics version"):
        solve_student_assignment(data)
