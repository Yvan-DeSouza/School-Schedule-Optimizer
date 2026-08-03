from backend.apps.core import models

class AcademicYear(models.Model):
    name = models.CharField(
        max_length=20,
        unique=True
    )