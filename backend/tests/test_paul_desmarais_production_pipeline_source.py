"""Fast deterministic checks for the upstream pipeline source contract."""

from scheduling_engine.paul_desmarais_production_pipeline_source import (
    LINEAGE_ID,
    STUDENT_COUNT,
    STUDENTS_PER_GRADE,
    build_source_spec,
    source_summary,
)


def test_pipeline_source_is_deterministic_and_target_scale():
    first = build_source_spec()
    second = build_source_spec()
    assert first.fingerprint == second.fingerprint
    summary = source_summary(first)
    assert first.lineage_id == LINEAGE_ID
    assert summary["student_count"] == STUDENT_COUNT == 1400
    assert summary["grade_counts"] == {9: 350, 10: 350, 11: 350, 12: 350}
    assert summary["commitment_counts"] == {"focus": 28, "study": 70}
    assert summary["online_course_choice_count"] == 84
    assert summary["paired_half_course_student_count"] == 350
    assert all(grade in {9, 10, 11, 12} for _student, grade in first.students)
    assert STUDENTS_PER_GRADE == 350


def test_pipeline_source_has_no_downstream_operational_facts():
    spec = build_source_spec()
    assert not hasattr(spec, "sections")
    assert not hasattr(spec, "section_schedules")
    assert not hasattr(spec, "online_supervision_sessions")
    assert not hasattr(spec, "teacher_assignments")
    assert not hasattr(spec, "enrollments")

