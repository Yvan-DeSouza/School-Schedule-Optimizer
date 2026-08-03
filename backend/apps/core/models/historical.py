from backend.apps.core import models
from backend.apps.core.models.people import Teacher
from backend.apps.core.models.time import AcademicYear
from backend.apps.core.models.courses import Course
class HistoricalCourseDemand(models.Model):
    course = models.ForeignKey(Course, on_delete=models.CASCADE)
    academic_year = models.ForeignKey(AcademicYear, on_delete=models.CASCADE)

    requests = models.IntegerField()

    final_enrollment = models.IntegerField()
    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["course", "academic_year"],
                name="unique_historical_course_year"
            )
        ]

