"""School-wide selectable values owned by the common app.

These are stable concepts shared across several domains. Values that describe a
specific workflow or bounded domain live beside that domain instead and are
re-exported by ``common.constants`` only for compatibility with older imports.
"""

# Student and course grade levels.
GRADE_LEVEL_7 = 7
GRADE_LEVEL_8 = 8
GRADE_LEVEL_9 = 9
GRADE_LEVEL_10 = 10
GRADE_LEVEL_11 = 11
GRADE_LEVEL_12 = 12
GRADE_LEVEL_CHOICES = (
    (GRADE_LEVEL_7, "Grade 7"),
    (GRADE_LEVEL_8, "Grade 8"),
    (GRADE_LEVEL_9, "Grade 9"),
    (GRADE_LEVEL_10, "Grade 10"),
    (GRADE_LEVEL_11, "Grade 11"),
    (GRADE_LEVEL_12, "Grade 12"),
)

# Physical room classifications.
ROOM_TYPE_CLASSROOM = "classroom"
ROOM_TYPE_SCIENCE_LAB = "science_lab"
ROOM_TYPE_COMPUTER_LAB = "computer_lab"
ROOM_TYPE_GYM = "gym"
ROOM_TYPE_DOME = "dome"
ROOM_TYPE_ART_ROOM = "art_room"
ROOM_TYPE_MUSIC_ROOM = "music_room"
ROOM_TYPE_CHOICES = (
    (ROOM_TYPE_CLASSROOM, "Classroom"),
    (ROOM_TYPE_SCIENCE_LAB, "Science Lab"),
    (ROOM_TYPE_COMPUTER_LAB, "Computer Lab"),
    (ROOM_TYPE_GYM, "Gym"),
    (ROOM_TYPE_DOME, "Dome"),
    (ROOM_TYPE_ART_ROOM, "Art Room"),
    (ROOM_TYPE_MUSIC_ROOM, "Music Room"),
)

# The school uses two semesters in each academic year.
SEMESTER_FALL = 1
SEMESTER_WINTER = 2
SEMESTER_CHOICES = (
    (SEMESTER_FALL, "Fall"),
    (SEMESTER_WINTER, "Winter"),
)

# Fixed A-D timetable blocks and their permanent four-day rotation. Each pair is
# (rotation_day, period), not a concrete calendar date.
SCHEDULE_BLOCK_A = "A"
SCHEDULE_BLOCK_B = "B"
SCHEDULE_BLOCK_C = "C"
SCHEDULE_BLOCK_D = "D"
SCHEDULE_BLOCK_CHOICES = (
    (SCHEDULE_BLOCK_A, "Block A"),
    (SCHEDULE_BLOCK_B, "Block B"),
    (SCHEDULE_BLOCK_C, "Block C"),
    (SCHEDULE_BLOCK_D, "Block D"),
)
BLOCK_ROTATION = {
    SCHEDULE_BLOCK_A: ((1, 1), (2, 3), (3, 2), (4, 4)),
    SCHEDULE_BLOCK_B: ((1, 2), (2, 4), (3, 1), (4, 3)),
    SCHEDULE_BLOCK_C: ((1, 3), (2, 1), (3, 4), (4, 2)),
    SCHEDULE_BLOCK_D: ((1, 4), (2, 2), (3, 3), (4, 1)),
}

