"""Course catalog, request, offering, and delivery-group domain values."""

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

# A catalogue course may be offered in either term or restricted to one term.
# These are deliberately separate from numeric semester values: a course can be
# available in both terms without becoming two separate catalog courses.
COURSE_ALLOWED_SEMESTER_1_ONLY = "semester_1_only"
COURSE_ALLOWED_SEMESTER_2_ONLY = "semester_2_only"
COURSE_ALLOWED_SEMESTER_EITHER = "either_semester"
COURSE_ALLOWED_SEMESTER_CHOICES = (
    (COURSE_ALLOWED_SEMESTER_1_ONLY, "Semester 1 only"),
    (COURSE_ALLOWED_SEMESTER_2_ONLY, "Semester 2 only"),
    (COURSE_ALLOWED_SEMESTER_EITHER, "Either semester"),
)

# A course request can be a student's main request or a backup choice.
COURSE_REQUEST_TYPE_PRIMARY = "primary"
COURSE_REQUEST_TYPE_ALTERNATE = "alternate"
COURSE_REQUEST_TYPE_CHOICES = (
    (COURSE_REQUEST_TYPE_PRIMARY, "Primary"),
    (COURSE_REQUEST_TYPE_ALTERNATE, "Alternate"),
)

# Course-offering decisions are year-specific. A catalog course continues to
# exist when its offering is cancelled, so requests and history remain intact.
COURSE_OFFERING_STATUS_OFFERED = "offered"
COURSE_OFFERING_STATUS_CANCELLED = "cancelled"
COURSE_OFFERING_STATUS_CHOICES = (
    (COURSE_OFFERING_STATUS_OFFERED, "Offered"),
    (COURSE_OFFERING_STATUS_CANCELLED, "Cancelled"),
)

DELIVERY_GROUP_STATUS_ACTIVE = "active"
DELIVERY_GROUP_STATUS_RETIRED = "retired"
DELIVERY_GROUP_STATUS_CHOICES = (
    (DELIVERY_GROUP_STATUS_ACTIVE, "Active"),
    (DELIVERY_GROUP_STATUS_RETIRED, "Retired"),
)

COURSE_OFFERING_ACTION_CANCELLED = "cancelled"
COURSE_OFFERING_ACTION_RESTORED = "restored"
COURSE_OFFERING_ACTION_COMBINED = "combined"
COURSE_OFFERING_ACTION_SEPARATED = "separated"
COURSE_OFFERING_ACTION_CHOICES = (
    (COURSE_OFFERING_ACTION_CANCELLED, "Cancelled"),
    (COURSE_OFFERING_ACTION_RESTORED, "Restored"),
    (COURSE_OFFERING_ACTION_COMBINED, "Combined"),
    (COURSE_OFFERING_ACTION_SEPARATED, "Separated"),
)

