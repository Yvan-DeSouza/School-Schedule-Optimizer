from django.core.validators import MinValueValidator
from django.db import models

from backend.apps.common.constants import GRADE_LEVEL


class Course(models.Model):
    name = models.CharField(max_length=200)

    grade_level = models.IntegerField(
        choices=GRADE_LEVEL
    )

    course_code = models.CharField(max_length=20, unique=True)
    category = models.CharField(
        max_length=50,
        choices=[
            ("math", "Mathematics"),
            ("science", "Science"),
            ("language", "Language"),
            ("technology", "Technology"),
            ("arts", "Arts"),
            ("business", "Business"),
            ("humanities", "Humanities"),
        ]
    )

    capacity_min = models.IntegerField(
        validators=[MinValueValidator(1)]
    )
    capacity_max = models.IntegerField(
        validators=[MinValueValidator(1)]
    )
    is_online = models.BooleanField(default=False)

    class Meta:
        ordering = ["course_code"]

    def __str__(self):
        return f"{self.course_code} - {self.name}"


class Section(models.Model):
    course = models.ForeignKey(Course, on_delete=models.CASCADE)
    section_number = models.CharField(max_length=10)
    academic_year = models.ForeignKey("common.AcademicYear", on_delete=models.CASCADE)
    semester = models.IntegerField(choices=[(1, "Fall"), (2, "Winter")])
    teacher = models.ForeignKey(
        "people.Teacher",
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )

    capacity_min = models.IntegerField(
        validators=[MinValueValidator(1)]
    )
    capacity_max = models.IntegerField(
        validators=[MinValueValidator(1)]
    )

    is_locked = models.BooleanField(default=False)

    class Meta:
        ordering = ["academic_year", "course", "section_number"]
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "course",
                    "section_number",
                    "academic_year"
                ],
                name="unique_course_section_year"
            )
        ]

    def __str__(self):
        return f"{self.course.course_code}-{self.section_number} ({self.academic_year})"


class Enrollment(models.Model):
    student = models.ForeignKey("people.Student", on_delete=models.CASCADE)
    section = models.ForeignKey(Section, on_delete=models.CASCADE)

    class Meta:
        ordering = ["student", "section"]
        constraints = [
            models.UniqueConstraint(
                fields=["student", "section"],
                name="unique_student_section_enrollment"
            )
        ]

    def __str__(self):
        return f"{self.student} -> {self.section}"


class CourseRequest(models.Model):
    student = models.ForeignKey("people.Student", on_delete=models.CASCADE)
    academic_year = models.ForeignKey("common.AcademicYear", on_delete=models.CASCADE)

    course = models.ForeignKey(Course, on_delete=models.CASCADE)

    is_mandatory = models.BooleanField(default=False)

    request_type = models.CharField(max_length=20, choices=[
        ("primary", "Primary"),
        ("alternate", "Alternate")
    ])

    class Meta:
        ordering = ["academic_year", "student", "request_type", "course"]
        constraints = [
            models.UniqueConstraint(
                fields=["student", "course", "academic_year"],
                name="unique_student_course_request"
            )
        ]

    def __str__(self):
        return f"{self.student} requests {self.course} ({self.request_type})"


class CoursePrerequisite(models.Model):
    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name="prerequisites")

    prerequisite = models.ForeignKey(Course, on_delete=models.CASCADE, related_name="required_for")

    class Meta:
        ordering = ["course", "prerequisite"]
        constraints = [
            models.UniqueConstraint(
                fields=["course", "prerequisite"],
                name="unique_course_prerequisite"
            )
        ]

    def __str__(self):
        return f"{self.course} requires {self.prerequisite}"
