"""Django-free constants shared by pure scheduling-engine modules.

These values intentionally stay with the engine rather than a Django app.
They describe detached solver input and result vocabulary, so importing a
backend constants module here would violate the scheduling-engine boundary.
"""

IMPORTANCE_LEVELS = {
    "not_important": 0,
    "a_little_bit_important": 1,
    "important": 2,
    "really_important": 3,
    "extremely_important": 4,
}

# These labels deliberately mirror the scheduling-domain constants without
# importing backend code. The engine owns no Django vocabulary dependency.
SCHEDULE_PRESERVATION_LEVELS = {
    "none": 0,
    "slight": 1,
    "moderate": 2,
    "strong": 4,
}

LOCK_TYPE_EXACT_SECTION = "exact_student_section"
LOCK_TYPE_WHOLE_SCHEDULE = "whole_student_schedule"
LOCK_TYPE_SECTION_ROSTER = "section_roster"
LOCK_TYPE_COURSE_ROSTER = "course_roster"
LOCK_TYPE_STUDENT_GROUP = "student_group_same_section"
LOCK_TYPE_STUDENT_TEACHER = "student_teacher_course"
LOCK_TYPES = {
    LOCK_TYPE_EXACT_SECTION,
    LOCK_TYPE_WHOLE_SCHEDULE,
    LOCK_TYPE_SECTION_ROSTER,
    LOCK_TYPE_COURSE_ROSTER,
    LOCK_TYPE_STUDENT_GROUP,
    LOCK_TYPE_STUDENT_TEACHER,
}

HALF_SEMESTER_SEGMENTS = ("first_half", "second_half")
