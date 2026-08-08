"""Manual scheduling overrides and hard section locks."""

from django.db import models


class ManualOverride(models.Model):
    """Human-readable before/after audit event for a section change."""

    section = models.ForeignKey("courses.Section", on_delete=models.CASCADE)

    # Examples: lock_teacher, lock_timeslot, move_section. A future override
    # service owns the canonical action workflow.
    action = models.CharField(max_length=50)

    previous_value = models.TextField(null=True, blank=True)
    new_value = models.TextField(null=True, blank=True)

    reason = models.TextField(null=True, blank=True)

    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-timestamp"]

    def __str__(self):
        return f"{self.action} for {self.section}"


class SectionLock(models.Model):
    """At most one set of counselor-fixed teacher/slot/room values per section."""

    section = models.OneToOneField(
        "courses.Section",
        on_delete=models.CASCADE
    )

    # Nullable fields allow locking only the accepted dimensions. SET_NULL keeps
    # the section/lock record if referenced master data is removed.
    locked_teacher = models.ForeignKey(
        "people.Teacher",
        null=True,
        blank=True,
        on_delete=models.SET_NULL
    )

    locked_timeslot = models.ForeignKey(
        "scheduling.TimeSlot",
        null=True,
        blank=True,
        on_delete=models.SET_NULL
    )

    locked_room = models.ForeignKey(
        "common.Room",
        null=True,
        blank=True,
        on_delete=models.SET_NULL
    )

    class Meta:
        ordering = ["section"]

    def __str__(self):
        return f"Locks for {self.section}"
