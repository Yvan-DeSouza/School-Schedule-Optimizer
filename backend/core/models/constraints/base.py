from backend.core import models
from backend.core.models.rooms import ROOM_TYPES
from backend.core.models.scheduling import TimeSlot
from backend.core.models.time import AcademicYear
from core.models.courses import Course
from core.models.people import Counselor, Teacher

class HardConstraint(models.Model):
    name = models.CharField(max_length=200)

    type = models.CharField(max_length=100)
    # conflict, prerequisite, qualification, capacity

    priority = models.IntegerField(default=100)


class SoftConstraint(models.Model):
    name = models.CharField(max_length=200)

    category = models.CharField(max_length=100)
    # balance_semesters, teacher_preferences, group_size_balance

    default_weight = models.IntegerField(default=1)

class CounselorConstraintPreference(models.Model):
    counselor = models.ForeignKey(Counselor, on_delete=models.CASCADE)

    constraint = models.ForeignKey(SoftConstraint, on_delete=models.CASCADE)

    weight = models.IntegerField(default=1)

class Qualification(models.Model):
    name = models.CharField(
        max_length=100,
        unique=True
    )
