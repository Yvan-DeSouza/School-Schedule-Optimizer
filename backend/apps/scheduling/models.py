from django.db import models


class TimeSlot(models.Model):
    day = models.CharField(max_length=20)

    period = models.IntegerField()
    academic_year = models.ForeignKey("common.AcademicYear", on_delete=models.CASCADE)

    semester = models.IntegerField(choices=[(1, "Fall"), (2, "Winter")])

    is_available = models.BooleanField(default=True)

    class Meta:
        ordering = ["academic_year", "semester", "day", "period"]

    def __str__(self):
        return f"{self.academic_year} S{self.semester} {self.day} P{self.period}"


class SectionSchedule(models.Model):
    section = models.OneToOneField("courses.Section", on_delete=models.CASCADE)
    timeslot = models.ForeignKey(TimeSlot, on_delete=models.SET_NULL, null=True)

    room = models.ForeignKey("common.Room", on_delete=models.SET_NULL, null=True)
    is_manual = models.BooleanField(default=False)

    class Meta:
        ordering = ["section"]

    def __str__(self):
        return f"Schedule for {self.section}"
