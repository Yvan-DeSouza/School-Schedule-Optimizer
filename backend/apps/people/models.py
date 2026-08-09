"""Application role linkage and student/teacher/counselor domain profiles."""

from django.db import models

from backend.apps.common.constants import (
    GRADE_LEVEL_CHOICES,
    USER_ROLE_CHOICES,
    USER_ROLE_COUNSELOR,
    USER_ROLE_COUNSELOR_LABEL,
    USER_ROLE_DIRECTOR,
    USER_ROLE_DIRECTOR_LABEL,
    USER_ROLE_STAFF,
    USER_ROLE_STAFF_LABEL,
    USER_ROLE_STUDENT,
    USER_ROLE_STUDENT_LABEL,
    USER_ROLE_TEACHER,
    USER_ROLE_TEACHER_LABEL,
    USER_ROLE_UNKNOWN,
    USER_ROLE_UNKNOWN_LABEL,
)


class RoleChoices(models.TextChoices):
    """Django-friendly facade over canonical role constants."""

    STUDENT = USER_ROLE_STUDENT, USER_ROLE_STUDENT_LABEL
    TEACHER = USER_ROLE_TEACHER, USER_ROLE_TEACHER_LABEL
    COUNSELOR = USER_ROLE_COUNSELOR, USER_ROLE_COUNSELOR_LABEL
    STAFF = USER_ROLE_STAFF, USER_ROLE_STAFF_LABEL
    DIRECTOR = USER_ROLE_DIRECTOR, USER_ROLE_DIRECTOR_LABEL
    UNKNOWN = USER_ROLE_UNKNOWN, USER_ROLE_UNKNOWN_LABEL


class UserRoleProfile(models.Model):
    """Explicit role for users without a student/teacher/counselor profile."""

    # Domain profiles take precedence during role resolution. This model mainly
    # represents staff, directors, and explicitly unknown accounts.
    user = models.OneToOneField(
        "auth.User",
        on_delete=models.CASCADE,
        related_name="role_profile",
    )
    role = models.CharField(
        max_length=30,
        choices=USER_ROLE_CHOICES,
        default=RoleChoices.UNKNOWN,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["user__username"]

    def __str__(self):
        return f"{self.user.username} - {self.role}"


class Student(models.Model):
    """Student identity and school-year context linked optionally to auth.User."""

    # SET_NULL preserves imported school records if a login account is removed.
    user = models.OneToOneField(
        "auth.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="student_profile",
    )

    student_number = models.CharField(max_length=30, unique=True)

    email = models.EmailField(unique=True)

    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)

    date_of_birth = models.DateField()
    grade_level = models.IntegerField(choices=GRADE_LEVEL_CHOICES)

    phone = models.CharField(max_length=30, null=True, blank=True)

    academic_year = models.ForeignKey("common.AcademicYear", on_delete=models.CASCADE)

    attendance_rate = models.FloatField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["last_name", "first_name", "student_number"]

    def __str__(self):
        return f"{self.student_number} - {self.first_name} {self.last_name}"


class Teacher(models.Model):
    """Teacher identity plus default workload values used when no plan override exists."""

    user = models.OneToOneField(
        "auth.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="teacher_profile",
    )

    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)

    email = models.EmailField(unique=True)

    department = models.CharField(max_length=100)

    seniority = models.IntegerField(default=0)

    # These are fallback maxima. TeacherPlanningCapacity can override each
    # semester for a specific academic year.
    max_courses_per_semester = models.IntegerField(default=3)

    max_courses_total = models.IntegerField(default=6)

    # is_reduced_load is descriptive context; effective numeric limits still come
    # from the maximum fields/planning-capacity records.
    is_reduced_load = models.BooleanField(default=False)
    # Referenced teachers are archived rather than deleted so old runs,
    # assignments, and qualification evidence remain explainable.
    is_archived = models.BooleanField(default=False)

    class Meta:
        ordering = ["last_name", "first_name"]

    def __str__(self):
        return f"{self.last_name}, {self.first_name}"


class TeacherStatusDecision(models.Model):
    """Append-only explanation for teacher archive/restore transitions."""

    teacher = models.ForeignKey(
        Teacher,
        on_delete=models.PROTECT,
        related_name="status_decisions",
    )
    action = models.CharField(
        max_length=20,
        choices=(("archived", "Archived"), ("restored", "Restored")),
    )
    reason = models.TextField()
    decided_by = models.ForeignKey("auth.User", null=True, on_delete=models.SET_NULL)
    decided_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["decided_at", "id"]

    def save(self, *args, **kwargs):
        if self.pk:
            from django.core.exceptions import ValidationError

            raise ValidationError("Teacher status decisions are immutable.")
        return super().save(*args, **kwargs)


class Counselor(models.Model):
    """Counselor domain profile used for ownership and planning preferences."""

    user = models.OneToOneField(
        "auth.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="counselor_profile",
    )

    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)

    email = models.EmailField(unique=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["last_name", "first_name"]

    def __str__(self):
        return f"{self.last_name}, {self.first_name}"
