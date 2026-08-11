"""Catalog difficulty and category-diversity configuration contracts."""

import pytest

from backend.apps.common.constants import COURSE_CATEGORY_MATH, COURSE_CATEGORY_SCIENCE, GRADE_LEVEL_7, GRADE_LEVEL_12
from backend.apps.courses.models import Course, CourseCategoryRelationship
from backend.apps.courses.services.difficulty import course_difficulty_facts


@pytest.mark.django_db
def test_grade_level_difficulty_is_bounded_deterministic_and_overrideable(course):
    early_course = Course.objects.create(
        name="Foundations", grade_level=GRADE_LEVEL_7, course_code="MTH7",
        category=COURSE_CATEGORY_MATH, capacity_min=10, capacity_max=30,
    )

    early = course_difficulty_facts(early_course)
    late = course_difficulty_facts(course)
    assert early["calculated_difficulty"] == 23
    assert late["calculated_difficulty"] == 93
    assert late["effective_difficulty"] == 93
    assert late == course_difficulty_facts(course)

    course.manual_difficulty_override = 93
    course.save(update_fields=["manual_difficulty_override"])
    overridden = course_difficulty_facts(course)
    assert overridden["calculated_difficulty"] == 93
    assert overridden["manual_difficulty_override"] == 93
    assert overridden["effective_difficulty"] == 93
    assert overridden["source"] == "manual_override"


@pytest.mark.django_db
def test_course_api_exposes_explainable_effective_difficulty(
    authenticated_client, counselor_user, course,
):
    client = authenticated_client(counselor_user)
    updated = client.patch(
        f"/api/courses/{course.id}/",
        {"manual_difficulty_override": 85},
        format="json",
    )

    assert updated.status_code == 200
    assert updated.data["calculated_difficulty"] == 93
    assert updated.data["manual_difficulty_override"] == 85
    assert updated.data["effective_difficulty"] == 85


@pytest.mark.django_db
def test_category_relationship_api_uses_one_canonical_unordered_pair(
    authenticated_client, counselor_user,
):
    client = authenticated_client(counselor_user)
    created = client.post("/api/planning/course-category-relationships/", {
        "category_a": COURSE_CATEGORY_MATH,
        "category_b": COURSE_CATEGORY_SCIENCE,
        "similarity_score": 35,
    }, format="json")

    assert created.status_code == 201
    assert created.data["similarity_score"] == 35
    reversed_pair = client.post("/api/planning/course-category-relationships/", {
        "category_a": COURSE_CATEGORY_SCIENCE,
        "category_b": COURSE_CATEGORY_MATH,
        "similarity_score": 35,
    }, format="json")
    assert reversed_pair.status_code == 400
    assert CourseCategoryRelationship.objects.count() == 1
