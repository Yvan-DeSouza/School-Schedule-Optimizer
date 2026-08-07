"""Canonical domain values shared by Django apps.

Keep selectable domain values here. Models, serializers, services, and API
tests should import these constants instead of defining their own copies.
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

# Course catalogue classifications.
COURSE_CATEGORY_MATH = "math"
COURSE_CATEGORY_SCIENCE = "science"
COURSE_CATEGORY_LANGUAGE = "language"
COURSE_CATEGORY_TECHNOLOGY = "technology"
COURSE_CATEGORY_ARTS = "arts"
COURSE_CATEGORY_BUSINESS = "business"
COURSE_CATEGORY_HUMANITIES = "humanities"
COURSE_CATEGORY_CHOICES = (
    (COURSE_CATEGORY_MATH, "Mathematics"),
    (COURSE_CATEGORY_SCIENCE, "Science"),
    (COURSE_CATEGORY_LANGUAGE, "Language"),
    (COURSE_CATEGORY_TECHNOLOGY, "Technology"),
    (COURSE_CATEGORY_ARTS, "Arts"),
    (COURSE_CATEGORY_BUSINESS, "Business"),
    (COURSE_CATEGORY_HUMANITIES, "Humanities"),
)

# The school uses two semesters in each academic year.
SEMESTER_FALL = 1
SEMESTER_WINTER = 2
SEMESTER_CHOICES = (
    (SEMESTER_FALL, "Fall"),
    (SEMESTER_WINTER, "Winter"),
)

# A course request can be a student's main request or a backup choice.
COURSE_REQUEST_TYPE_PRIMARY = "primary"
COURSE_REQUEST_TYPE_ALTERNATE = "alternate"
COURSE_REQUEST_TYPE_CHOICES = (
    (COURSE_REQUEST_TYPE_PRIMARY, "Primary"),
    (COURSE_REQUEST_TYPE_ALTERNATE, "Alternate"),
)

# Fixed A-D timetable blocks and their permanent four-day rotation. Each pair
# is (rotation_day, period).
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

# Application roles. RoleChoices in people.models exposes these through
# Django's TextChoices API for permission policies and model validation.
USER_ROLE_STUDENT = "student"
USER_ROLE_STUDENT_LABEL = "Student"
USER_ROLE_TEACHER = "teacher"
USER_ROLE_TEACHER_LABEL = "Teacher"
USER_ROLE_COUNSELOR = "counselor"
USER_ROLE_COUNSELOR_LABEL = "Counselor"
USER_ROLE_STAFF = "staff"
USER_ROLE_STAFF_LABEL = "Staff"
USER_ROLE_DIRECTOR = "director"
USER_ROLE_DIRECTOR_LABEL = "Director"
USER_ROLE_UNKNOWN = "unknown"
USER_ROLE_UNKNOWN_LABEL = "Unknown"
USER_ROLE_CHOICES = (
    (USER_ROLE_STUDENT, USER_ROLE_STUDENT_LABEL),
    (USER_ROLE_TEACHER, USER_ROLE_TEACHER_LABEL),
    (USER_ROLE_COUNSELOR, USER_ROLE_COUNSELOR_LABEL),
    (USER_ROLE_STAFF, USER_ROLE_STAFF_LABEL),
    (USER_ROLE_DIRECTOR, USER_ROLE_DIRECTOR_LABEL),
    (USER_ROLE_UNKNOWN, USER_ROLE_UNKNOWN_LABEL),
)
