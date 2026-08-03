from backend.apps.core import models
from backend.apps.core.models.rooms import Room
from backend.apps.core.models.time import AcademicYear
from backend.apps.core.models.courses import Section
class TimeSlot(models.Model):
    day = models.CharField(max_length=20)

    period = models.IntegerField()
    academic_year = models.ForeignKey(AcademicYear, on_delete=models.CASCADE)


    semester = models.IntegerField(choices=[(1, "Fall"), (2, "Winter")])

    is_available = models.BooleanField(default=True)

class SectionSchedule(models.Model):
    section = models.OneToOneField(Section, on_delete=models.CASCADE)
    timeslot = models.ForeignKey(TimeSlot, on_delete=models.SET_NULL, null=True)

    room = models.ForeignKey(Room, on_delete=models.SET_NULL, null=True)
    is_manual = models.BooleanField(default=False)