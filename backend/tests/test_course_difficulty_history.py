"""Historical-result evidence for explainable course difficulty."""

import pytest

from backend.apps.common.constants import COURSE_CATEGORY_MATH, COURSE_CATEGORY_SCIENCE, GRADE_LEVEL_12
from backend.apps.common.models import AcademicYear
from backend.apps.courses.models import Course, StudentCourseHistoricalResult
from backend.apps.courses.services.difficulty import course_difficulty_facts
from backend.apps.people.models import Student


def _student(index, year):
    return Student.objects.create(
        student_number=f"H{index}", email=f"history{index}@example.com",
        first_name="History", last_name=str(index), date_of_birth="2008-01-01",
        grade_level=GRADE_LEVEL_12, academic_year=year,
    )


@pytest.mark.django_db
def test_metadata_uses_grade_category_and_ontario_designation_signals():
    year = AcademicYear.objects.create(name="2021-2022")
    university_math = Course.objects.create(name="Advanced Math", grade_level=12, course_code="MHF4U", category=COURSE_CATEGORY_MATH, capacity_min=10, capacity_max=30)
    college_science = Course.objects.create(name="College Science", grade_level=12, course_code="SNC4C", category=COURSE_CATEGORY_SCIENCE, capacity_min=10, capacity_max=30)

    assert course_difficulty_facts(university_math)["metadata_difficulty"] > course_difficulty_facts(college_science)["metadata_difficulty"]
    assert course_difficulty_facts(university_math)["source"] == "metadata"


@pytest.mark.django_db
def test_historical_relative_performance_is_recency_weighted_and_blended():
    old_year = AcademicYear.objects.create(name="2020-2021")
    recent_year = AcademicYear.objects.create(name="2024-2025")
    target = Course.objects.create(name="Physics", grade_level=12, course_code="SPH4U", category=COURSE_CATEGORY_SCIENCE, capacity_min=10, capacity_max=30)
    comparison = Course.objects.create(name="English", grade_level=12, course_code="ENG4U", category="language", capacity_min=10, capacity_max=30)
    old_student, recent_student = _student(1, old_year), _student(2, recent_year)
    old_target = StudentCourseHistoricalResult.objects.create(student=old_student, course=target, academic_year=old_year, final_mark=90)
    StudentCourseHistoricalResult.objects.create(student=old_student, course=comparison, academic_year=old_year, final_mark=80)
    recent_target = StudentCourseHistoricalResult.objects.create(student=recent_student, course=target, academic_year=recent_year, final_mark=70)
    StudentCourseHistoricalResult.objects.create(student=recent_student, course=comparison, academic_year=recent_year, final_mark=90)
    results = [old_target, recent_target]
    student_year = {(old_student.id, old_year.id): list(StudentCourseHistoricalResult.objects.filter(student=old_student, academic_year=old_year)), (recent_student.id, recent_year.id): list(StudentCourseHistoricalResult.objects.filter(student=recent_student, academic_year=recent_year))}

    facts = course_difficulty_facts(target, historical_results=results, student_year_results=student_year)
    assert facts["source"] == "historical_and_metadata"
    assert facts["relative_performance_signal"] < 0
    assert facts["weighted_course_average"] < 82
    assert facts["historical_observation_count"] == 2
    assert facts["historical_confidence"] > 0


@pytest.mark.django_db
def test_missing_comparison_marks_fall_back_and_override_remains_authoritative(course):
    year = AcademicYear.objects.create(name="2023-2024")
    student = _student(3, year)
    row = StudentCourseHistoricalResult.objects.create(student=student, course=course, academic_year=year, final_mark=55)
    facts = course_difficulty_facts(course, historical_results=[row], student_year_results={})
    assert facts["source"] == "metadata"
    course.manual_difficulty_override = 40
    assert course_difficulty_facts(course)["effective_difficulty"] == 40
