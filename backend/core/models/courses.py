from backend.core import models

from backend.core.models.people import Student, Teacher
from backend.core.models.time import AcademicYear
from django.core.validators import MinValueValidator

GRADE_LEVEL = [    
    (7, "Grade 7"),
    (8, "Grade 8"),
    (9, "Grade 9"),
    (10, "Grade 10"),
    (11, "Grade 11"),
    (12, "Grade 12"),
]
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

class Section(models.Model):
    course = models.ForeignKey(Course, on_delete=models.CASCADE)
    section_number = models.CharField(max_length=10)
    academic_year = models.ForeignKey(AcademicYear, on_delete=models.CASCADE)
    semester = models.IntegerField(choices=[(1, "Fall"), (2, "Winter")])
    teacher = models.ForeignKey(
        Teacher,
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

class Enrollment(models.Model):
    student = models.ForeignKey(Student, on_delete=models.CASCADE)
    section = models.ForeignKey(Section, on_delete=models.CASCADE)
    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["student", "section"],
                name="unique_student_section_enrollment"
            )
        ]

class CourseRequest(models.Model):
    student = models.ForeignKey(Student, on_delete=models.CASCADE)
    academic_year = models.ForeignKey(AcademicYear, on_delete=models.CASCADE)

    course = models.ForeignKey(Course, on_delete=models.CASCADE)

    is_mandatory = models.BooleanField(default=False)

    request_type = models.CharField(max_length=20, choices=[
        ("primary", "Primary"),
        ("alternate", "Alternate")
    ])

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["student", "course", "academic_year"],
                name="unique_student_course_request"
            )
        ]


class CoursePrerequisite(models.Model):
    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name="prerequisites")

    prerequisite = models.ForeignKey(Course, on_delete=models.CASCADE, related_name="required_for")
    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["course", "prerequisite"],
                name="unique_course_prerequisite"
            )
        ]