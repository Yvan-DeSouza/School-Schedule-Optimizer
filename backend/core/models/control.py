from backend.core import models

from backend.core.models.people import Teacher
from backend.core.models.rooms import Room
from backend.core.models.scheduling import TimeSlot
from core.models.courses import Section

class ManualOverride(models.Model):
    section = models.ForeignKey(Section, on_delete=models.CASCADE)

    action = models.CharField(max_length=50)
    # lock_teacher, lock_timeslot, move_section

    previous_value = models.TextField(null=True, blank=True)
    new_value = models.TextField(null=True, blank=True)

    reason = models.TextField(null=True, blank=True)

    timestamp = models.DateTimeField(auto_now_add=True)

class SectionLock(models.Model):
    section = models.OneToOneField(
        Section,
        on_delete=models.CASCADE
    )

    locked_teacher = models.ForeignKey(
        Teacher,
        null=True,
        blank=True,
        on_delete=models.SET_NULL
    )

    locked_timeslot = models.ForeignKey(
        TimeSlot,
        null=True,
        blank=True,
        on_delete=models.SET_NULL
    )

    locked_room = models.ForeignKey(
        Room,
        null=True,
        blank=True,
        on_delete=models.SET_NULL
    )