"""Adapter and immutable-run contracts for new student-assignment preferences."""

import pytest

from backend.apps.common.constants import (
    COURSE_CATEGORY_SCIENCE,
    COURSE_REQUEST_TYPE_PRIMARY,
    GRADE_LEVEL_12,
    SCHEDULE_BLOCK_A,
)
from backend.apps.courses.models import Course, CourseCategoryRelationship, CourseRequest, Section
from backend.apps.courses.services.offerings import ensure_academic_year_offerings
from backend.apps.scheduling.models import SectionSchedule, TimeSlot
from backend.apps.scheduling.serializers import StudentAssignmentRunCreateSerializer
from backend.apps.scheduling.services.engine_adapter import load_student_assignment_input
from backend.apps.scheduling.services.student_assignment import (
    StudentAssignmentConflictError,
    create_student_assignment_run,
    preview_student_assignment_approval,
)


def _importance():
    return {
        "section_utilization_balance": "not_important",
        "student_semester_balance": "not_important",
        "course_sequence_preferences": "not_important",
        "difficulty_balance": "important",
        "course_category_diversity": "really_important",
    }


def _importance_scores():
    return {
        "section_utilization_balance": 1,
        "student_semester_balance": 3,
        "course_sequence_preferences": 5,
        "difficulty_balance": 7,
        "course_category_diversity": 9,
    }


def test_v2_serializer_accepts_canonical_scores_and_rejects_them_for_v1():
    base = {
        "academic_year": 1,
        "staffing_mode": "sections_only",
        "soft_constraint_importance": _importance(),
        "objective_importance_scores": _importance_scores(),
    }
    v2 = StudentAssignmentRunCreateSerializer(
        data={**base, "objective_semantics_version": "v2"}
    )
    assert v2.is_valid(), v2.errors
    assert v2.validated_data["objective_importance_scores"] == _importance_scores()

    v1 = StudentAssignmentRunCreateSerializer(data={**base, "objective_semantics_version": "v1"})
    assert not v1.is_valid()
    assert "objective_importance_scores" in v1.errors


@pytest.mark.django_db
def test_adapter_freezes_effective_difficulty_category_relationships_and_importance(
    academic_year, course, counselor_user, student_user,
):
    course.manual_difficulty_override = 87
    course.save(update_fields=["manual_difficulty_override"])
    other_course = Course.objects.create(
        name="Physics", grade_level=GRADE_LEVEL_12, course_code="SPH4U",
        category=COURSE_CATEGORY_SCIENCE, capacity_min=10, capacity_max=30,
    )
    CourseCategoryRelationship.objects.create(
        category_a="math", category_b="science", similarity_score=40,
    )
    offering = ensure_academic_year_offerings(academic_year, actor=counselor_user)[0]
    Section.objects.create(
        course=course, delivery_group=offering.delivery_group, section_number="SOFT-01",
        academic_year=academic_year, semester=1, capacity_min=10, capacity_max=30,
    )
    section = Section.objects.get(section_number="SOFT-01")
    slot = TimeSlot.objects.create(academic_year=academic_year, semester=1, block=SCHEDULE_BLOCK_A)
    SectionSchedule.objects.create(section=section, timeslot=slot)
    CourseRequest.objects.create(
        student=student_user.student_profile, academic_year=academic_year,
        course=course, request_type=COURSE_REQUEST_TYPE_PRIMARY,
    )

    data, _staffing = load_student_assignment_input(
        academic_year_id=academic_year.id,
        staffing_mode="sections_only",
        soft_constraint_importance=_importance(),
    )

    assert data.difficulty_balance_importance == "important"
    assert data.course_category_diversity_importance == "really_important"
    difficulty = data.course_difficulties[0]
    assert difficulty.course_id == course.id
    assert difficulty.calculated_difficulty == 93
    assert difficulty.manual_difficulty_override == 87
    assert difficulty.effective_difficulty == 87
    assert difficulty.source == "manual_override"
    assert data.course_category_relationships[0].similarity_score == 40
    assert other_course.id not in {item.course_id for item in data.course_difficulties}
    assert (student_user.student_profile.id, student_user.student_profile.grade_level) in data.student_grades


@pytest.mark.django_db
def test_adapter_resolves_v2_importance_scores_into_immutable_engine_input(
    academic_year, course, counselor_user,
):
    offering = ensure_academic_year_offerings(academic_year, actor=counselor_user)[0]
    section = Section.objects.create(
        course=course, delivery_group=offering.delivery_group, section_number="V2-01",
        academic_year=academic_year, semester=1, capacity_min=10, capacity_max=30,
    )
    slot = TimeSlot.objects.create(
        academic_year=academic_year, semester=1, block=SCHEDULE_BLOCK_A,
    )
    SectionSchedule.objects.create(section=section, timeslot=slot)
    data, _staffing = load_student_assignment_input(
        academic_year_id=academic_year.id,
        staffing_mode="sections_only",
        soft_constraint_importance=_importance(),
        objective_semantics_version="v2",
        objective_importance_scores=_importance_scores(),
    )

    assert data.objective_semantics_version == "v2"
    assert dict(data.objective_importance_scores) == _importance_scores()


@pytest.mark.django_db
def test_v2_run_snapshot_and_result_preserve_objective_semantics_metadata(
    academic_year, course, counselor_user, student_user,
):
    offering = ensure_academic_year_offerings(academic_year, actor=counselor_user)[0]
    section = Section.objects.create(
        course=course, delivery_group=offering.delivery_group, section_number="V2-02",
        academic_year=academic_year, semester=1, capacity_min=10, capacity_max=30,
    )
    slot = TimeSlot.objects.create(
        academic_year=academic_year, semester=1, block=SCHEDULE_BLOCK_A,
    )
    SectionSchedule.objects.create(section=section, timeslot=slot)
    CourseRequest.objects.create(
        student=student_user.student_profile, academic_year=academic_year,
        course=course, request_type=COURSE_REQUEST_TYPE_PRIMARY,
    )

    run = create_student_assignment_run(
        academic_year=academic_year.id,
        staffing_mode="sections_only",
        soft_constraint_importance=_importance(),
        objective_semantics_version="v2",
        objective_importance_scores=_importance_scores(),
        created_by=counselor_user,
    )

    assert run.status == "complete"
    assert run.input_snapshot["objective_semantics_version"] == "v2"
    assert run.input_snapshot["objective_importance_scores"] == _importance_scores()
    assert run.solver_metadata["objective_semantics_version"] == "v2"
    assert run.result["optimization_facts"]["objective_semantics"]["version"] == "v2"


@pytest.mark.django_db
def test_difficulty_change_invalidates_an_unapproved_run_snapshot(
    academic_year, course, counselor_user, student_user,
):
    offering = ensure_academic_year_offerings(academic_year, actor=counselor_user)[0]
    section = Section.objects.create(
        course=course, delivery_group=offering.delivery_group, section_number="SOFT-02",
        academic_year=academic_year, semester=1, capacity_min=10, capacity_max=30,
    )
    slot = TimeSlot.objects.create(academic_year=academic_year, semester=1, block=SCHEDULE_BLOCK_A)
    SectionSchedule.objects.create(section=section, timeslot=slot)
    CourseRequest.objects.create(
        student=student_user.student_profile, academic_year=academic_year,
        course=course, request_type=COURSE_REQUEST_TYPE_PRIMARY,
    )
    run = create_student_assignment_run(
        academic_year=academic_year.id, staffing_mode="sections_only",
        soft_constraint_importance=_importance(), created_by=counselor_user,
    )
    review = preview_student_assignment_approval(run)
    assert review["course_difficulty_facts"][0]["effective_difficulty"] == 93
    assert review["student_difficulty_balance"][0]["difficulty_imbalance"] == 93

    course.manual_difficulty_override = 90
    course.save(update_fields=["manual_difficulty_override"])
    with pytest.raises(StudentAssignmentConflictError) as error:
        preview_student_assignment_approval(run)

    assert error.value.detail["code"] == "student_assignment_input_changed_since_run"
