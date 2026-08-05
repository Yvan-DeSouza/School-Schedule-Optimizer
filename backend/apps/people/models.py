from django.db import models

from backend.apps.common.constants import GRADE_LEVEL


class Student(models.Model):
    student_number = models.CharField(max_length=30, unique=True)

    email = models.EmailField(unique=True)

    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)

    date_of_birth = models.DateField()
    grade_level = models.IntegerField(choices=GRADE_LEVEL)

    phone = models.CharField(max_length=30, null=True, blank=True)

    academic_year = models.ForeignKey("common.AcademicYear", on_delete=models.CASCADE)

    attendance_rate = models.FloatField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["last_name", "first_name", "student_number"]

    def __str__(self):
        return f"{self.student_number} - {self.first_name} {self.last_name}"


class Teacher(models.Model):
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)

    email = models.EmailField(unique=True)

    department = models.CharField(max_length=100)

    seniority = models.IntegerField(default=0)

    max_courses_per_semester = models.IntegerField(default=3)

    max_courses_total = models.IntegerField(default=6)

    reduced_load = models.BooleanField(default=False)

    class Meta:
        ordering = ["last_name", "first_name"]

    def __str__(self):
        return f"{self.last_name}, {self.first_name}"


class Counselor(models.Model):
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)

    email = models.EmailField(unique=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["last_name", "first_name"]

    def __str__(self):
        return f"{self.last_name}, {self.first_name}"
