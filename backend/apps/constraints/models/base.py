from django.db import models

from backend.apps.common.constants import (
    QUALIFICATION_DIVISION_CHOICES,
    QUALIFICATION_DIVISION_NONE,
    QUALIFICATION_KIND_CHOICES,
    QUALIFICATION_KIND_TEACHABLE,
    QUALIFICATION_SUBJECT_CHOICES,
    QUALIFICATION_SUBJECT_NONE,
)

class HardConstraint(models.Model):
    name = models.CharField(max_length=200)

    type = models.CharField(max_length=100)
    # conflict, prerequisite, qualification, capacity

    priority = models.IntegerField(default=100)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class SoftConstraint(models.Model):
    name = models.CharField(max_length=200)

    category = models.CharField(max_length=100)
    # balance_semesters, teacher_preferences, group_size_balance

    default_weight = models.IntegerField(default=1)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class CounselorConstraintPreference(models.Model):
    counselor = models.ForeignKey("people.Counselor", on_delete=models.CASCADE)

    constraint = models.ForeignKey(SoftConstraint, on_delete=models.CASCADE)

    weight = models.IntegerField(default=1)

    class Meta:
        ordering = ["counselor", "constraint"]

    def __str__(self):
        return f"{self.counselor} prefers {self.constraint} at {self.weight}"


class Qualification(models.Model):
    """A normalized credential that can be matched to courses and teachers."""

    code = models.CharField(max_length=100, unique=True)

    name = models.CharField(
        max_length=100,
        unique=True
    )

    kind = models.CharField(
        max_length=20,
        choices=QUALIFICATION_KIND_CHOICES,
        default=QUALIFICATION_KIND_TEACHABLE,
    )

    subject_code = models.CharField(
        max_length=100,
        choices=QUALIFICATION_SUBJECT_CHOICES,
        blank=True,
        default=QUALIFICATION_SUBJECT_NONE,
    )
    division = models.CharField(
        max_length=20,
        choices=QUALIFICATION_DIVISION_CHOICES,
        default=QUALIFICATION_DIVISION_NONE,
    )

    class Meta:
        ordering = ["kind", "subject_code", "division", "name"]

    def __str__(self):
        return self.name
