"""Shared academic-year, room, and historical-demand reference models."""

from django.core.validators import MinValueValidator
from django.db import models

from backend.apps.common.constants import ROOM_TYPE_CHOICES


class AcademicYear(models.Model):
    """School planning year identified by the canonical YYYY-YYYY label."""

    name = models.CharField(
        max_length=20,
        unique=True
    )

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Room(models.Model):
    """Physical room and capabilities used by future section placement."""

    name = models.CharField(
        max_length=50,
        unique=True
    )

    room_type = models.CharField(
        max_length=20,
        choices=ROOM_TYPE_CHOICES
    )

    capacity = models.IntegerField(
        validators=[MinValueValidator(1)]
    )
    # Specialized distinguishes purpose-built spaces even when room_type is also
    # used as the hard matching category.
    is_specialized = models.BooleanField(default=False)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class HistoricalCourseDemand(models.Model):
    """Observed request-to-final-enrollment outcome for one course/year."""

    course = models.ForeignKey("courses.Course", on_delete=models.CASCADE)
    academic_year = models.ForeignKey("common.AcademicYear", on_delete=models.CASCADE)

    # The pure demand analyzer applies recency weighting and computes the
    # conversion ratio; raw evidence remains lossless here.
    requests = models.IntegerField()

    final_enrollment = models.IntegerField()

    class Meta:
        ordering = ["academic_year", "course"]
        constraints = [
            models.UniqueConstraint(
                fields=["course", "academic_year"],
                name="unique_historical_course_year"
            )
        ]

    def __str__(self):
        return f"{self.course} demand for {self.academic_year}"
