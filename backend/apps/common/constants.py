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

# A catalogue course may be offered in either term or restricted to one term.
# These are deliberately separate from the numeric semester values above: a
# course can be available in both terms without being two different courses.
COURSE_ALLOWED_SEMESTER_1_ONLY = "semester_1_only"
COURSE_ALLOWED_SEMESTER_2_ONLY = "semester_2_only"
COURSE_ALLOWED_SEMESTER_EITHER = "either_semester"
COURSE_ALLOWED_SEMESTER_CHOICES = (
    (COURSE_ALLOWED_SEMESTER_1_ONLY, "Semester 1 only"),
    (COURSE_ALLOWED_SEMESTER_2_ONLY, "Semester 2 only"),
    (COURSE_ALLOWED_SEMESTER_EITHER, "Either semester"),
)

# Section-planning capacity profiles and course-demand priorities.
CAPACITY_PROFILE_SCOPE_SHARED = "shared"
CAPACITY_PROFILE_SCOPE_COURSE_SPECIFIC = "course_specific"
CAPACITY_PROFILE_SCOPE_CHOICES = (
    (CAPACITY_PROFILE_SCOPE_SHARED, "Shared"),
    (CAPACITY_PROFILE_SCOPE_COURSE_SPECIFIC, "Course specific"),
)
COURSE_PRIORITY_TIER_CORE = 1
COURSE_PRIORITY_TIER_PATHWAY = 2
COURSE_PRIORITY_TIER_COUNSELOR = 3
COURSE_PRIORITY_TIER_STANDARD = 4
COURSE_PRIORITY_TIER_CHOICES = (
    (COURSE_PRIORITY_TIER_CORE, "Core graduation requirement"),
    (COURSE_PRIORITY_TIER_PATHWAY, "Pathway-critical"),
    (COURSE_PRIORITY_TIER_COUNSELOR, "Counselor-designated priority"),
    (COURSE_PRIORITY_TIER_STANDARD, "Standard elective"),
)
SECTION_PLANNING_RUN_STATUS_COMPLETE = "complete"
SECTION_PLANNING_RUN_STATUS_INFEASIBLE = "infeasible"
SECTION_PLANNING_RUN_STATUS_FAILED = "failed"
SECTION_PLANNING_RUN_STATUS_CHOICES = (
    (SECTION_PLANNING_RUN_STATUS_COMPLETE, "Complete"),
    (SECTION_PLANNING_RUN_STATUS_INFEASIBLE, "Infeasible"),
    (SECTION_PLANNING_RUN_STATUS_FAILED, "Failed"),
)

# A course request can be a student's main request or a backup choice.
COURSE_REQUEST_TYPE_PRIMARY = "primary"
COURSE_REQUEST_TYPE_ALTERNATE = "alternate"
COURSE_REQUEST_TYPE_CHOICES = (
    (COURSE_REQUEST_TYPE_PRIMARY, "Primary"),
    (COURSE_REQUEST_TYPE_ALTERNATE, "Alternate"),
)

# Official teaching-qualification catalog values. A qualification record is a
# normalized, reusable credential (for example, mathematics + senior), while a
# TeacherQualification records that a particular teacher holds it.
QUALIFICATION_KIND_TEACHABLE = "teachable"
QUALIFICATION_KIND_ADDITIONAL = "additional"
QUALIFICATION_KIND_CHOICES = (
    (QUALIFICATION_KIND_TEACHABLE, "Teachable qualification"),
    (QUALIFICATION_KIND_ADDITIONAL, "Additional qualification"),
)

# Canonical teachable subjects presently represented by the available school
# records. Add a new subject here before importing a new official teachable;
# callers must import these values rather than use their own strings.
QUALIFICATION_SUBJECT_NONE = ""
QUALIFICATION_SUBJECT_MATHEMATICS = "mathematics"
QUALIFICATION_SUBJECT_CHEMISTRY = "chemistry"
QUALIFICATION_SUBJECT_FRENCH = "french"
QUALIFICATION_SUBJECT_COMPUTER_STUDIES = "computer_studies"
QUALIFICATION_SUBJECT_BUSINESS_STUDIES_GENERAL = "business_studies_general"
QUALIFICATION_SUBJECT_CHOICES = (
    (QUALIFICATION_SUBJECT_NONE, "Not applicable"),
    (QUALIFICATION_SUBJECT_MATHEMATICS, "Mathematics"),
    (QUALIFICATION_SUBJECT_CHEMISTRY, "Chemistry"),
    (QUALIFICATION_SUBJECT_FRENCH, "French"),
    (QUALIFICATION_SUBJECT_COMPUTER_STUDIES, "Computer Studies"),
    (QUALIFICATION_SUBJECT_BUSINESS_STUDIES_GENERAL, "Business Studies - General"),
)

QUALIFICATION_DIVISION_NONE = "none"
QUALIFICATION_DIVISION_PRIMARY = "primary"
QUALIFICATION_DIVISION_JUNIOR = "junior"
QUALIFICATION_DIVISION_INTERMEDIATE = "intermediate"
QUALIFICATION_DIVISION_SENIOR = "senior"
QUALIFICATION_DIVISION_CHOICES = (
    (QUALIFICATION_DIVISION_NONE, "Not applicable"),
    (QUALIFICATION_DIVISION_PRIMARY, "Primary"),
    (QUALIFICATION_DIVISION_JUNIOR, "Junior"),
    (QUALIFICATION_DIVISION_INTERMEDIATE, "Intermediate"),
    (QUALIFICATION_DIVISION_SENIOR, "Senior"),
)

# Course-to-qualification rules are either legal requirements or assignment
# preferences. Grade 11 and 12 courses must use required senior teachables.
QUALIFICATION_ENFORCEMENT_REQUIRED = "required"
QUALIFICATION_ENFORCEMENT_PREFERRED = "preferred"
QUALIFICATION_ENFORCEMENT_CHOICES = (
    (QUALIFICATION_ENFORCEMENT_REQUIRED, "Required"),
    (QUALIFICATION_ENFORCEMENT_PREFERRED, "Preferred"),
)
STATUTORY_TEACHABLE_MIN_GRADE = GRADE_LEVEL_11

# Provenance values for a teacher's individual credential record.
QUALIFICATION_SOURCE_ASPEN = "aspen"
QUALIFICATION_SOURCE_MANUAL = "manual"
QUALIFICATION_SOURCE_IMPORT = "import"
QUALIFICATION_SOURCE_CHOICES = (
    (QUALIFICATION_SOURCE_ASPEN, "Aspen"),
    (QUALIFICATION_SOURCE_MANUAL, "Manual entry"),
    (QUALIFICATION_SOURCE_IMPORT, "Approved import"),
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
