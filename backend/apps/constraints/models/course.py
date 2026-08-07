from django.db import models

from backend.apps.common.constants import ROOM_TYPE_CHOICES
from backend.apps.constraints.models.base import Qualification


class CourseRoomRequirement(models.Model):
    course = models.ForeignKey("courses.Course", on_delete=models.CASCADE)

    room_type = models.CharField(max_length=20, choices=ROOM_TYPE_CHOICES)

    class Meta:
        ordering = ["course", "room_type"]
        constraints = [
            models.UniqueConstraint(
                fields=["course", "room_type"],
                name="unique_course_room_requirement"
            )
        ]

    def __str__(self):
        return f"{self.course} requires {self.room_type}"


class CourseQualificationRequirement(models.Model):
    course = models.ForeignKey("courses.Course", on_delete=models.CASCADE)

    qualification = models.ForeignKey(Qualification, on_delete=models.CASCADE)

    class Meta:
        ordering = ["course", "qualification"]
        constraints = [
            models.UniqueConstraint(
                fields=["course", "qualification"],
                name="unique_course_qualification_requirement"
            )
        ]

    def __str__(self):
        return f"{self.course} requires {self.qualification}"


class CourseConflict(models.Model):
    course_a = models.ForeignKey("courses.Course", on_delete=models.CASCADE, related_name="conflicts_as_a")
    course_b = models.ForeignKey("courses.Course", on_delete=models.CASCADE, related_name="conflicts_as_b")
    weight = models.FloatField()

    class Meta:
        ordering = ["course_a", "course_b"]
        constraints = [
            models.UniqueConstraint(
                fields=["course_a", "course_b"],
                name="unique_course_conflict"
            ),
            models.CheckConstraint(
                condition=~models.Q(course_a=models.F("course_b")),
                name="course_cannot_conflict_with_itself",
            )
        ]

    def __str__(self):
        return f"{self.course_a} conflicts with {self.course_b}"
