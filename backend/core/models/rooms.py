from backend.core import models
from django.core.validators import MinValueValidator

ROOM_TYPES = [
    ("classroom", "Classroom"),
    ("science_lab", "Science Lab"),
    ("computer_lab", "Computer Lab"),
    ("gym", "Gym"),
    ("dome", "Dome"),
    ("art_room", "Art Room"),
    ("music_room", "Music Room"),
]

class Room(models.Model):
    name = models.CharField(
        max_length=50,
        unique=True
    )

    room_type = models.CharField(
        max_length=20,
        choices=ROOM_TYPES
    )

    capacity = models.IntegerField(
        validators=[MinValueValidator(1)]
    )
    is_specialized = models.BooleanField(default=False)

