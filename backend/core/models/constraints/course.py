from backend.core import models
from backend.core.models.constraints.base import Qualification
from backend.core.models.rooms import ROOM_TYPES
from core.models.courses import Course


class CourseRoomRequirement(models.Model):
    course = models.ForeignKey(Course, on_delete=models.CASCADE)

    room_type = models.CharField(max_length=20, choices=ROOM_TYPES)
    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["course", "room_type"],
                name="unique_course_room_requirement"
            )
        ]

class CourseQualificationRequirement(models.Model):
    course = models.ForeignKey(Course, on_delete=models.CASCADE)

    qualification = models.ForeignKey(Qualification, on_delete=models.CASCADE)
    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["course", "qualification"],
                name="unique_course_qualification_requirement"
            )
        ]

class CourseConflict(models.Model):
    course_a = models.ForeignKey(Course, on_delete=models.CASCADE, related_name="conflicts_as_a")
    course_b = models.ForeignKey(Course, on_delete=models.CASCADE, related_name="conflicts_as_b")
    weight = models.FloatField()
    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["course_a", "course_b"],
                name="unique_course_conflict"
            ),
            models.CheckConstraint(
                check=~models.Q(course_a=models.F("course_b")),
                name="course_cannot_conflict_with_itself",
            )
        ]