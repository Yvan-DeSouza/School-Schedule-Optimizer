"""Course catalog, operational sections, demand requests, and prerequisites."""

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from backend.apps.common.constants import (
    COURSE_ALLOWED_SEMESTER_CHOICES,
    COURSE_ALLOWED_SEMESTER_EITHER,
    COURSE_CATEGORY_CHOICES,
    COURSE_REQUEST_TYPE_CHOICES,
    COURSE_REQUEST_TYPE_ALTERNATE,
    GRADE_LEVEL_CHOICES,
    COURSE_OFFERING_ACTION_CHOICES,
    COURSE_OFFERING_STATUS_CHOICES,
    COURSE_OFFERING_STATUS_OFFERED,
    DELIVERY_GROUP_STATUS_ACTIVE,
    DELIVERY_GROUP_STATUS_CHOICES,
    ENROLLMENT_LIFECYCLE_ACTIVE,
    ENROLLMENT_LIFECYCLE_CHOICES,
    SECTION_LIFECYCLE_ACTIVE,
    SECTION_LIFECYCLE_CHOICES,
    SEMESTER_CHOICES,
    COURSE_DELIVERY_KIND_CHOICES,
    COURSE_DELIVERY_KIND_CO_OP,
    COURSE_DELIVERY_KIND_NORMAL_INSTRUCTION,
    COURSE_DELIVERY_KIND_ONLINE,
    COURSE_DURATION_CHOICES,
    COURSE_DURATION_FULL_SEMESTER,
    COURSE_DURATION_HALF_SEMESTER,
    HALF_SEMESTER_SEGMENT_CHOICES,
    STUDENT_SCHEDULE_COMMITMENT_REQUEST_TYPE_CHOICES,
    STUDENT_SCHEDULE_COMMITMENT_REQUEST_TYPE_FOCUS,
    STUDENT_SCHEDULE_COMMITMENT_REQUEST_TYPE_STUDY,
)


class Course(models.Model):
    """Catalog course plus policy references used by section planning."""

    name = models.CharField(max_length=200)

    grade_level = models.IntegerField(
        choices=GRADE_LEVEL_CHOICES
    )

    course_code = models.CharField(max_length=20, unique=True)
    category = models.CharField(
        max_length=50,
        choices=COURSE_CATEGORY_CHOICES,
        blank=True,
        default="",
    )

    # Academic facts and delivery facts deliberately remain independent.  A
    # course's delivery may be online without changing its credit, category, or
    # difficulty; Co-op is the single category-neutral exception.
    delivery_kind = models.CharField(
        max_length=30,
        choices=COURSE_DELIVERY_KIND_CHOICES,
        default=COURSE_DELIVERY_KIND_NORMAL_INSTRUCTION,
    )
    duration = models.CharField(
        max_length=20,
        choices=COURSE_DURATION_CHOICES,
        default=COURSE_DURATION_FULL_SEMESTER,
    )
    credit_value = models.DecimalField(
        max_digits=3,
        decimal_places=1,
        default=Decimal("1.0"),
        validators=[MinValueValidator(Decimal("0.5"))],
    )

    # Deprecated compatibility fields remain for existing section CRUD, imports,
    # and the legacy estimator. The CP-SAT planner uses capacity_profile.
    capacity_min = models.IntegerField(
        validators=[MinValueValidator(1)],
        default=10,
    )
    capacity_max = models.IntegerField(
        validators=[MinValueValidator(1)],
        default=35,
    )
    # PROTECT prevents an active policy from disappearing underneath courses.
    capacity_profile = models.ForeignKey("scheduling.CapacityProfile", on_delete=models.PROTECT, related_name="courses")
    # Priority is explicit administrator policy and is never inferred from a
    # request's mandatory flag, course code, grade, or category.
    priority_profile = models.ForeignKey("scheduling.CoursePriorityProfile", on_delete=models.PROTECT, related_name="courses")
    # Semester availability is a hard catalog rule for planning and placement.
    allowed_semester = models.CharField(
        max_length=20,
        choices=COURSE_ALLOWED_SEMESTER_CHOICES,
        default=COURSE_ALLOWED_SEMESTER_EITHER,
    )
    is_online = models.BooleanField(default=False)
    # The automatic score is derived from the catalog grade level because the
    # application has no historical student-result data yet. A counselor can
    # replace that estimate when local academic knowledge is more reliable.
    manual_difficulty_override = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )

    class Meta:
        ordering = ["course_code"]

    def __str__(self):
        return f"{self.course_code} - {self.name}"

    def clean(self):
        """Keep the narrow special-course model explicit at its catalog source."""

        super().clean()
        errors = {}
        if self.delivery_kind == COURSE_DELIVERY_KIND_CO_OP:
            if self.duration != COURSE_DURATION_FULL_SEMESTER:
                errors["duration"] = "Co-op is a full-semester program with a special two-block commitment."
            if self.credit_value != Decimal("2.0"):
                errors["credit_value"] = "Co-op must carry exactly 2.0 credits."
            if self.category:
                errors["category"] = "Co-op is category-neutral and must not use a subject category."
        else:
            if not self.category:
                errors["category"] = "Normal and online academic courses require a category."
            expected_credit = Decimal("0.5") if self.duration == COURSE_DURATION_HALF_SEMESTER else Decimal("1.0")
            if self.credit_value != expected_credit:
                errors["credit_value"] = (
                    "Half-semester courses carry 0.5 credits and full-semester academic courses carry 1.0 credit."
                )
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        # Direct ORM creation is common in imports and tests, so guarantee
        # profiles even when callers did not pass the new foreign keys. The local
        # import avoids a models/services cycle during Django app initialization.
        if not self.capacity_profile_id or not self.priority_profile_id:
            from backend.apps.scheduling.services.planning_configuration import ensure_default_planning_profiles

            capacity_profile, priority_profile = ensure_default_planning_profiles()
            if not self.capacity_profile_id:
                self.capacity_profile = capacity_profile
            if not self.priority_profile_id:
                self.priority_profile = priority_profile
        # ``is_online`` is an older public field.  Keep it coherent for
        # existing callers while delivery_kind becomes the unambiguous source
        # of truth for all new scheduling behavior.
        if (
            self._state.adding
            and self.is_online
            and self.delivery_kind == COURSE_DELIVERY_KIND_NORMAL_INSTRUCTION
        ):
            # Preserve the old create-only convenience while allowing an
            # existing course to deliberately change delivery kind through the
            # explicit modern field. Reapplying the legacy flag on every save
            # would silently undo a counselor's reviewed change from online to
            # normal instruction.
            self.delivery_kind = COURSE_DELIVERY_KIND_ONLINE
        self.is_online = self.delivery_kind == COURSE_DELIVERY_KIND_ONLINE
        return super().save(*args, **kwargs)


class HalfSemesterCoursePair(models.Model):
    """Catalog default ordering for the school's intentionally small trimestre pattern."""

    first_course = models.ForeignKey(
        Course,
        on_delete=models.PROTECT,
        related_name="first_half_semester_pairs",
    )
    second_course = models.ForeignKey(
        Course,
        on_delete=models.PROTECT,
        related_name="second_half_semester_pairs",
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["first_course__course_code", "second_course__course_code"]
        constraints = [
            models.UniqueConstraint(
                fields=["first_course", "second_course"],
                name="unique_half_semester_course_pair",
            ),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if self.first_course_id == self.second_course_id:
            errors["second_course"] = "A half-semester course cannot be paired with itself."
        if self.first_course_id and self.first_course.duration != COURSE_DURATION_HALF_SEMESTER:
            errors["first_course"] = "The first paired course must be half-semester."
        if self.second_course_id and self.second_course.duration != COURSE_DURATION_HALF_SEMESTER:
            errors["second_course"] = "The second paired course must be half-semester."
        if self.first_course_id and self.second_course_id:
            if self.first_course.delivery_kind != self.second_course.delivery_kind:
                errors["second_course"] = "Paired half-semester courses must use the same delivery kind."
            existing = HalfSemesterCoursePair.objects.exclude(pk=self.pk).filter(
                models.Q(first_course_id__in=[self.first_course_id, self.second_course_id])
                | models.Q(second_course_id__in=[self.first_course_id, self.second_course_id])
            )
            if existing.exists():
                errors["first_course"] = "A course may belong to only one half-semester pair."
        if errors:
            raise ValidationError(errors)


class CourseCombinationRule(models.Model):
    """Administrator-approved catalog courses that may share one physical class."""

    name = models.CharField(max_length=200, unique=True)
    capacity_profile = models.ForeignKey(
        "scheduling.CapacityProfile",
        on_delete=models.PROTECT,
        related_name="combination_rules",
    )
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class CourseCombinationRuleMember(models.Model):
    """One member of a compatibility rule; rules require at least two members."""

    rule = models.ForeignKey(
        CourseCombinationRule,
        on_delete=models.CASCADE,
        related_name="members",
    )
    course = models.ForeignKey(Course, on_delete=models.PROTECT)

    class Meta:
        ordering = ["rule", "course__course_code"]
        constraints = [
            models.UniqueConstraint(
                fields=["rule", "course"],
                name="unique_course_per_combination_rule",
            )
        ]


class DeliveryGroup(models.Model):
    """One physical delivery identity shared by one or more course offerings."""

    academic_year = models.ForeignKey("common.AcademicYear", on_delete=models.CASCADE)
    name = models.CharField(max_length=200)
    capacity_profile = models.ForeignKey(
        "scheduling.CapacityProfile",
        on_delete=models.PROTECT,
        related_name="delivery_groups",
    )
    combination_rule = models.ForeignKey(
        CourseCombinationRule,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="delivery_groups",
    )
    status = models.CharField(
        max_length=20,
        choices=DELIVERY_GROUP_STATUS_CHOICES,
        default=DELIVERY_GROUP_STATUS_ACTIVE,
    )
    created_by = models.ForeignKey("auth.User", null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    reason = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["academic_year", "name", "id"]

    @property
    def is_combined(self):
        return self.offerings.count() > 1

    def __str__(self):
        return f"{self.name} ({self.academic_year})"


class CourseOffering(models.Model):
    """Current course/year offering state and its physical delivery group."""

    course = models.ForeignKey(Course, on_delete=models.PROTECT, related_name="offerings")
    academic_year = models.ForeignKey("common.AcademicYear", on_delete=models.CASCADE)
    delivery_group = models.ForeignKey(
        DeliveryGroup,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="offerings",
    )
    status = models.CharField(
        max_length=20,
        choices=COURSE_OFFERING_STATUS_CHOICES,
        default=COURSE_OFFERING_STATUS_OFFERED,
    )
    decision_reason = models.TextField(blank=True, default="")
    decided_by = models.ForeignKey("auth.User", null=True, on_delete=models.SET_NULL)
    decided_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["academic_year", "course__course_code"]
        constraints = [
            models.UniqueConstraint(
                fields=["course", "academic_year"],
                name="unique_course_offering_per_year",
            )
        ]

    def __str__(self):
        return f"{self.course.course_code} in {self.academic_year}"


class CourseOfferingDecision(models.Model):
    """Append-only audit event for cancellation and delivery-group changes."""

    offering = models.ForeignKey(
        CourseOffering,
        on_delete=models.PROTECT,
        related_name="decisions",
    )
    action = models.CharField(max_length=20, choices=COURSE_OFFERING_ACTION_CHOICES)
    previous_status = models.CharField(max_length=20, choices=COURSE_OFFERING_STATUS_CHOICES)
    new_status = models.CharField(max_length=20, choices=COURSE_OFFERING_STATUS_CHOICES)
    previous_delivery_group = models.ForeignKey(
        DeliveryGroup,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="previous_decisions",
    )
    new_delivery_group = models.ForeignKey(
        DeliveryGroup,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="new_decisions",
    )
    decided_by = models.ForeignKey("auth.User", null=True, on_delete=models.SET_NULL)
    decided_at = models.DateTimeField(auto_now_add=True)
    reason = models.TextField()

    class Meta:
        ordering = ["decided_at", "id"]

    def save(self, *args, **kwargs):
        if self.pk:
            from django.core.exceptions import ValidationError

            raise ValidationError("Course offering decisions are immutable.")
        return super().save(*args, **kwargs)


class Section(models.Model):
    """One offered instance of a course in a particular semester."""

    # ``delivery_group`` is the canonical physical identity. ``course`` remains
    # nullable as a short transition/compatibility field for legacy standalone
    # callers while new planning writes always set the group.
    course = models.ForeignKey(Course, null=True, blank=True, on_delete=models.PROTECT)
    delivery_group = models.ForeignKey(
        DeliveryGroup,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="sections",
    )
    section_number = models.CharField(max_length=10)
    academic_year = models.ForeignKey("common.AcademicYear", on_delete=models.CASCADE)
    semester = models.IntegerField(choices=SEMESTER_CHOICES)
    # A normal half-semester section is still a real instructional section. The
    # segment tells student assignment which half of its recurring A-D block it
    # occupies; full-semester sections deliberately leave this null.
    half_semester_segment = models.CharField(
        max_length=20,
        choices=HALF_SEMESTER_SEGMENT_CHOICES,
        null=True,
        blank=True,
    )
    # Teacher is optional because plan approval creates unstaffed drafts; named
    # assignment belongs to a later scheduling stage.
    teacher = models.ForeignKey(
        "people.Teacher",
        on_delete=models.SET_NULL,
        null=True,
        blank=True
    )

    capacity_min = models.IntegerField(
        validators=[MinValueValidator(1)]
    )
    capacity_max = models.IntegerField(
        validators=[MinValueValidator(1)]
    )

    # Locked sections are accepted decisions that downstream automation must not
    # silently move or reassign.
    is_locked = models.BooleanField(default=False)
    # Reconciliation retires obsolete drafts without deleting their identity,
    # provenance, or audit trail. Only active sections enter later solver stages.
    lifecycle_status = models.CharField(
        max_length=20,
        choices=SECTION_LIFECYCLE_CHOICES,
        default=SECTION_LIFECYCLE_ACTIVE,
    )
    # Manual/legacy sections legitimately have no approval source. Generated
    # drafts link to the exact per-course decision, then to approval/run/user.
    planning_approval_course = models.ForeignKey(
        "scheduling.SectionPlanningApprovalCourse",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="generated_sections",
    )
    # New physical-delivery approvals use an offering-level audit line.  The
    # older per-course relationship remains for historical standalone plans.
    staffing_approval_offering = models.ForeignKey(
        "scheduling.StaffingPlanApprovalOffering",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="generated_sections",
    )
    # Annual placement materializes virtual annual slots only after counselors
    # approve a semester/block candidate. This provenance prevents those rows
    # from being mistaken for manually-created sections by lifecycle helpers.
    annual_placement_approval = models.ForeignKey(
        "scheduling.SectionPlacementApproval",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="materialized_sections",
    )

    class Meta:
        ordering = ["academic_year", "course", "section_number"]
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "course",
                    "section_number",
                    "academic_year"
                ],
                name="unique_course_section_year"
            ),
            models.UniqueConstraint(
                fields=["delivery_group", "section_number", "academic_year"],
                name="unique_delivery_group_section_year",
            ),
        ]

    def __str__(self):
        # Combined physical sections intentionally have no single canonical
        # Course FK, so use the delivery-group label when needed.
        label = self.course.course_code if self.course_id else (
            self.delivery_group.name if self.delivery_group_id else "Unlinked"
        )
        return f"{label}-{self.section_number} ({self.academic_year})"


class HalfSemesterSectionPair(models.Model):
    """One shared teaching block formed by sequential normal instructional sections."""

    course_pair = models.ForeignKey(
        HalfSemesterCoursePair,
        on_delete=models.PROTECT,
        related_name="section_pairs",
    )
    first_section = models.OneToOneField(
        Section,
        on_delete=models.PROTECT,
        related_name="first_half_pair",
    )
    second_section = models.OneToOneField(
        Section,
        on_delete=models.PROTECT,
        related_name="second_half_pair",
    )

    class Meta:
        ordering = ["first_section__academic_year", "first_section__section_number"]

    def clean(self):
        super().clean()
        errors = {}
        if self.first_section_id == self.second_section_id:
            errors["second_section"] = "A half-semester section pair requires two distinct sections."
        if self.first_section_id and self.second_section_id:
            first = self.first_section
            second = self.second_section
            if first.academic_year_id != second.academic_year_id:
                errors["second_section"] = "Paired sections must belong to the same academic year."
            if first.semester != second.semester:
                errors["second_section"] = "Paired sections must be in the same semester."
            if first.half_semester_segment != "first_half":
                errors["first_section"] = "The first pair section must occupy the first half."
            if second.half_semester_segment != "second_half":
                errors["second_section"] = "The second pair section must occupy the second half."
            if first.course_id != self.course_pair.first_course_id:
                errors["first_section"] = "The first section must deliver the pair's first course."
            if second.course_id != self.course_pair.second_course_id:
                errors["second_section"] = "The second section must deliver the pair's second course."
            # SectionSchedule and teacher equality are enforced by the workflow
            # once those later-stage facts exist; direct model validation must
            # still allow the legitimate pre-placement/pre-staffing draft state.
        if errors:
            raise ValidationError(errors)


class Enrollment(models.Model):
    """Assignment of one student to one offered section."""

    student = models.ForeignKey("people.Student", on_delete=models.CASCADE)
    section = models.ForeignKey(Section, on_delete=models.CASCADE)
    # Required for new combined deliveries; nullable only for legacy standalone
    # rows created before the unified offering workflow is used.
    course_offering = models.ForeignKey(
        CourseOffering,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="enrollments",
    )
    # Retiring an enrollment preserves the original placement for audit while
    # removing it from operational capacity and student-conflict calculations.
    lifecycle_status = models.CharField(
        max_length=20,
        choices=ENROLLMENT_LIFECYCLE_CHOICES,
        default=ENROLLMENT_LIFECYCLE_ACTIVE,
    )

    class Meta:
        ordering = ["student", "section"]
        constraints = [
            models.UniqueConstraint(
                fields=["student", "section"],
                condition=models.Q(lifecycle_status=ENROLLMENT_LIFECYCLE_ACTIVE),
                name="unique_active_student_section_enrollment"
            ),
            # Legacy rows may not have an offering, but every new solver write
            # identifies the exact offering a student is taking.
            models.UniqueConstraint(
                fields=["student", "course_offering"],
                condition=models.Q(
                    course_offering__isnull=False,
                    lifecycle_status=ENROLLMENT_LIFECYCLE_ACTIVE,
                ),
                name="unique_active_student_course_offering_enrollment",
            ),
        ]

    def clean(self):
        """Keep new offering-aware enrollment rows tied to one physical class."""

        super().clean()
        if not self.course_offering_id or not self.section_id:
            return
        section = self.section
        offering = self.course_offering
        if offering.academic_year_id != section.academic_year_id:
            raise ValidationError({
                "course_offering": "The offering must belong to the section's academic year."
            })
        if section.delivery_group_id:
            if offering.delivery_group_id != section.delivery_group_id:
                raise ValidationError({
                    "course_offering": "The offering must belong to the section's physical delivery group."
                })
        elif section.course_id != offering.course_id:
            raise ValidationError({
                "course_offering": "A legacy standalone section may enroll only its own course offering."
            })

    def __str__(self):
        return f"{self.student} -> {self.section}"


class CourseRequest(models.Model):
    """Student demand signal for a course in one planning year."""

    student = models.ForeignKey("people.Student", on_delete=models.CASCADE)
    academic_year = models.ForeignKey("common.AcademicYear", on_delete=models.CASCADE)

    course = models.ForeignKey(Course, on_delete=models.CASCADE)

    # Mandatory is request provenance, not a source for course priority.
    is_mandatory = models.BooleanField(default=False)

    request_type = models.CharField(max_length=20, choices=COURSE_REQUEST_TYPE_CHOICES)

    class Meta:
        ordering = ["academic_year", "student", "request_type", "course"]
        constraints = [
            models.UniqueConstraint(
                fields=["student", "course", "academic_year"],
                name="unique_student_course_request"
            ),
            models.UniqueConstraint(
                fields=["student", "academic_year"],
                condition=models.Q(request_type=COURSE_REQUEST_TYPE_ALTERNATE),
                name="unique_student_backup_request_per_year",
            ),
        ]

    def __str__(self):
        return f"{self.student} requests {self.course} ({self.request_type})"


class StudentScheduleCommitmentRequest(models.Model):
    """A requested Study or Focus commitment that is not a catalog course."""

    student = models.ForeignKey(
        "people.Student",
        on_delete=models.CASCADE,
        related_name="schedule_commitment_requests",
    )
    academic_year = models.ForeignKey("common.AcademicYear", on_delete=models.CASCADE)
    commitment_type = models.CharField(
        max_length=20,
        choices=STUDENT_SCHEDULE_COMMITMENT_REQUEST_TYPE_CHOICES,
    )
    # Study has two independently placeable annual requests. Focus is a single
    # semester-wide request and therefore always uses index one.
    request_index = models.PositiveSmallIntegerField(default=1)

    class Meta:
        ordering = ["academic_year", "student", "commitment_type", "request_index"]
        constraints = [
            models.UniqueConstraint(
                fields=["student", "academic_year", "commitment_type", "request_index"],
                name="unique_student_schedule_commitment_request",
            ),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if self.commitment_type == STUDENT_SCHEDULE_COMMITMENT_REQUEST_TYPE_STUDY:
            if self.request_index not in {1, 2}:
                errors["request_index"] = "A student may request at most two Study sessions per year."
        elif self.commitment_type == STUDENT_SCHEDULE_COMMITMENT_REQUEST_TYPE_FOCUS:
            if self.request_index != 1:
                errors["request_index"] = "Focus has one semester-wide request per year."
        if self.student_id and self.academic_year_id and self.student.academic_year_id != self.academic_year_id:
            errors["student"] = "The student must belong to the request academic year."
        if errors:
            raise ValidationError(errors)


class CoursePrerequisite(models.Model):
    """Directed catalog prerequisite relation for student scheduling."""

    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name="prerequisites")

    prerequisite = models.ForeignKey(Course, on_delete=models.CASCADE, related_name="required_for")

    class Meta:
        ordering = ["course", "prerequisite"]
        constraints = [
            models.UniqueConstraint(
                fields=["course", "prerequisite"],
                name="unique_course_prerequisite"
            )
        ]

    def __str__(self):
        return f"{self.course} requires {self.prerequisite}"


class CourseSequencePreference(models.Model):
    """A non-binding catalog preference for same-year course order."""

    earlier_course = models.ForeignKey(
        Course,
        on_delete=models.CASCADE,
        related_name="sequence_preferences_as_earlier",
    )
    later_course = models.ForeignKey(
        Course,
        on_delete=models.CASCADE,
        related_name="sequence_preferences_as_later",
    )
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey("auth.User", null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["earlier_course", "later_course"]
        constraints = [
            models.UniqueConstraint(
                fields=["earlier_course", "later_course"],
                name="unique_course_sequence_preference",
            ),
            models.CheckConstraint(
                condition=~models.Q(earlier_course=models.F("later_course")),
                name="course_sequence_preference_not_self_referential",
            ),
        ]

    def __str__(self):
        return f"Prefer {self.earlier_course} before {self.later_course}"


class CourseCategoryRelationship(models.Model):
    """One school-wide similarity value for an unordered pair of categories.

    Equal categories are inherently maximally similar and therefore do not
    need stored rows. Missing cross-category rows are intentionally neutral:
    they do not manufacture a relationship the school has not defined.
    """

    category_a = models.CharField(max_length=50, choices=COURSE_CATEGORY_CHOICES)
    category_b = models.CharField(max_length=50, choices=COURSE_CATEGORY_CHOICES)
    similarity_score = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        default=0,
    )

    class Meta:
        ordering = ["category_a", "category_b"]
        constraints = [
            models.UniqueConstraint(
                fields=["category_a", "category_b"],
                name="unique_course_category_relationship",
            ),
            models.CheckConstraint(
                condition=models.Q(category_a__lt=models.F("category_b")),
                name="course_category_relationship_canonical_pair",
            ),
        ]

    def clean(self):
        super().clean()
        if self.category_a >= self.category_b:
            raise ValidationError({
                "category_b": "Category relationships must use two distinct categories in alphabetical order.",
            })

    def __str__(self):
        return f"{self.category_a} / {self.category_b}: {self.similarity_score}"


class StudentCourseHistoricalResult(models.Model):
    """One immutable historical final mark used as difficulty evidence.

    Enrollment history does not contain achievement data. Keeping the observed
    mark as its own source fact lets summaries be recomputed transparently
    instead of treating a cached course average as authoritative evidence.
    """

    student = models.ForeignKey("people.Student", on_delete=models.PROTECT, related_name="historical_course_results")
    course = models.ForeignKey(Course, on_delete=models.PROTECT, related_name="historical_results")
    academic_year = models.ForeignKey("common.AcademicYear", on_delete=models.PROTECT, related_name="historical_course_results")
    final_mark = models.DecimalField(max_digits=5, decimal_places=2, validators=[MinValueValidator(0), MaxValueValidator(100)])
    source_record_id = models.CharField(max_length=100, blank=True, default="")
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["academic_year__name", "student", "course"]
        constraints = [
            models.UniqueConstraint(fields=["student", "course", "academic_year"], name="unique_student_course_historical_result"),
        ]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("Historical course results are immutable; correct source data with a new reviewed import record.")
        return super().save(*args, **kwargs)
