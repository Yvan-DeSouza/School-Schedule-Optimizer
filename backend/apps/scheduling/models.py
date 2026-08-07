from django.db import models

from backend.apps.scheduling.constants import SCHEDULE_BLOCKS


class TimeSlot(models.Model):
    block = models.CharField(max_length=1, choices=SCHEDULE_BLOCKS)
    academic_year = models.ForeignKey("common.AcademicYear", on_delete=models.CASCADE)

    semester = models.IntegerField(choices=[(1, "Fall"), (2, "Winter")])

    is_available = models.BooleanField(default=True)

    class Meta:
        ordering = ["academic_year", "semester", "block"]
        constraints = [
            models.UniqueConstraint(
                fields=["academic_year", "semester", "block"],
                name="unique_academic_year_semester_block",
            )
        ]

    def __str__(self):
        return f"{self.academic_year} S{self.semester} Block {self.block}"


class SectionSchedule(models.Model):
    section = models.OneToOneField("courses.Section", on_delete=models.CASCADE)
    timeslot = models.ForeignKey(TimeSlot, on_delete=models.SET_NULL, null=True)

    room = models.ForeignKey("common.Room", on_delete=models.SET_NULL, null=True)
    is_manual = models.BooleanField(default=False)

    class Meta:
        ordering = ["section"]

    def __str__(self):
        return f"Schedule for {self.section}"
