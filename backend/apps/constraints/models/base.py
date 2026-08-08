"""School-wide constraint metadata and normalized qualification catalog."""

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
    """Named hard-rule metadata available to compiler/administration."""

    name = models.CharField(max_length=200)

    # Examples: conflict, prerequisite, qualification, and capacity. Values are
    # administrator data rather than a duplicated Python choice list for now.
    type = models.CharField(max_length=100)

    priority = models.IntegerField(default=100)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class SoftConstraint(models.Model):
    """Named objective category and its school-wide default weight."""

    name = models.CharField(max_length=200)

    # Examples: balance_semesters, teacher_preferences, group_size_balance.
    category = models.CharField(max_length=100)

    default_weight = models.IntegerField(default=1)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class CounselorConstraintPreference(models.Model):
    """Per-counselor override weight for one soft objective."""

    counselor = models.ForeignKey("people.Counselor", on_delete=models.CASCADE)

    constraint = models.ForeignKey(SoftConstraint, on_delete=models.CASCADE)

    weight = models.IntegerField(default=1)

    class Meta:
        ordering = ["counselor", "constraint"]

    def __str__(self):
        return f"{self.counselor} prefers {self.constraint} at {self.weight}"


class Qualification(models.Model):
    """A normalized credential that can be matched to courses and teachers."""

    # Code is stable for imports/integration; name is the human-facing label.
    code = models.CharField(max_length=100, unique=True)

    name = models.CharField(
        max_length=100,
        unique=True
    )

    # Teachable vs additional keeps legal matching distinct from other training.
    kind = models.CharField(
        max_length=20,
        choices=QUALIFICATION_KIND_CHOICES,
        default=QUALIFICATION_KIND_TEACHABLE,
    )

    # Subject/division are canonical matching dimensions. Raw Aspen strings live
    # on TeacherQualification provenance fields instead.
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
