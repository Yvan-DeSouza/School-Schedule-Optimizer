"""Policy-filtered CRUD endpoints for scheduling constraints and locks."""

from django.shortcuts import get_object_or_404
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import APIException, PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from backend.apps.access.permissions import ResourcePolicyPermission
from backend.apps.access.resource_policies.constraints import PlanningResourcePolicy, TeacherOwnedResourcePolicy
from backend.apps.constraints.models import (
    CounselorConstraintPreference, CourseConflict, CourseQualificationRequirement,
    CourseRoomRequirement, HardConstraint, Qualification, SoftConstraint,
    TeacherAvailability, TeacherCoursePreference, TeacherCurrentCourse, TeacherQualification,
)
from backend.apps.constraints.serializers import (
    CounselorConstraintPreferenceSerializer, CourseConflictSerializer,
    CourseQualificationRequirementSerializer, CourseRoomRequirementSerializer,
    HardConstraintSerializer, QualificationSerializer, SectionLockSerializer,
    SoftConstraintSerializer, TeacherAvailabilitySerializer, TeacherCoursePreferenceSerializer,
    TeacherCurrentCourseSerializer, TeacherQualificationReviewSerializer,
    TeacherQualificationSerializer,
)
from backend.apps.control.models import SectionLock
from backend.apps.common.constants import (
    QUALIFICATION_REVIEW_PENDING,
    QUALIFICATION_REVIEW_REJECTED,
    QUALIFICATION_REVIEW_VERIFIED,
    SECTION_LIFECYCLE_RETIRED,
)
from backend.apps.courses.models import Section
from backend.apps.people.models import RoleChoices, Teacher
from backend.apps.people.roles import get_user_role


class RetiredSectionConflict(APIException):
    """Prevent new hard-lock state from being attached to retired history."""

    status_code = status.HTTP_409_CONFLICT
    default_code = "retired_section_conflict"


class PolicyFilteredViewSet(viewsets.ModelViewSet):
    """Apply resource policy before optional query-parameter narrowing."""

    permission_classes = [ResourcePolicyPermission]
    filter_fields = ()

    def get_queryset(self):
        # Authorization always precedes client filtering; changing this order can
        # leak records through apparently harmless query parameters.
        queryset = self.resource_policy_class.filter_read_queryset(self.request.user, self.queryset.all())
        for field in self.filter_fields:
            value = self.request.query_params.get(field)
            if value is not None:
                queryset = queryset.filter(**{field: value})
        return queryset


class SharedConstraintViewSet(PolicyFilteredViewSet):
    """Base for school-wide configuration managed by planning roles."""

    resource_policy_class = PlanningResourcePolicy


class TeacherNestedViewSet(PolicyFilteredViewSet):
    """Base for /teachers/{id}/... resources with URL-owned teacher identity."""

    resource_policy_class = TeacherOwnedResourcePolicy
    filter_fields = ()

    def get_teacher(self):
        # Cache the object because queryset, serializer context, and create may
        # all need it during one request.
        if not hasattr(self, "_teacher"):
            self._teacher = get_object_or_404(Teacher.objects.select_related("user"), pk=self.kwargs["teacher_id"])
        return self._teacher

    def get_policy_context(self):
        # POST permission is evaluated before get_object/queryset. Return a small
        # context safely even when the URL teacher does not exist.
        teacher = Teacher.objects.select_related("user").filter(pk=self.kwargs["teacher_id"]).first()
        return {"teacher_user_id": teacher.user_id if teacher else None}

    def get_queryset(self):
        # Policy first, then enforce the URL parent relationship.
        return super().get_queryset().filter(teacher=self.get_teacher())

    def get_serializer_context(self):
        # TeacherHiddenSerializer uses this authoritative object for uniqueness.
        context = super().get_serializer_context()
        context["teacher"] = self.get_teacher()
        return context

    def perform_create(self, serializer):
        # Never accept teacher identity from request JSON.
        serializer.save(teacher=self.get_teacher())


class TeacherQualificationViewSet(TeacherNestedViewSet):
    queryset = TeacherQualification.objects.select_related("teacher__user", "qualification")
    serializer_class = TeacherQualificationSerializer
    filter_fields = ("qualification", "source_system")

    def perform_create(self, serializer):
        item = serializer.save(
            teacher=self.get_teacher(),
            submitted_by=self.request.user,
            review_status=QUALIFICATION_REVIEW_PENDING,
        )
        from backend.apps.scheduling.services.staffing_configuration import (
            invalidate_teacher_rosters,
        )

        invalidate_teacher_rosters(item.teacher_id)

    def perform_update(self, serializer):
        # Changing the evidence invalidates an earlier review. The planning role
        # must verify the resulting record again before a solver can use it.
        item = serializer.save(
            review_status=QUALIFICATION_REVIEW_PENDING,
            reviewed_by=None,
            reviewed_at=None,
            review_reason="",
        )
        from backend.apps.scheduling.services.staffing_configuration import (
            invalidate_teacher_rosters,
        )

        invalidate_teacher_rosters(item.teacher_id)

    def perform_destroy(self, instance):
        if instance.review_status != QUALIFICATION_REVIEW_PENDING:
            raise ValidationError({
                "detail": (
                    "Reviewed qualification evidence is retained for audit. "
                    "Reject it or edit it back into pending review instead."
                )
            })
        teacher_id = instance.teacher_id
        instance.delete()
        from backend.apps.scheduling.services.staffing_configuration import (
            invalidate_teacher_rosters,
        )

        invalidate_teacher_rosters(teacher_id)

    def _require_planning_role(self):
        if get_user_role(self.request.user) not in {
            RoleChoices.COUNSELOR,
            RoleChoices.STAFF,
            RoleChoices.DIRECTOR,
        }:
            raise PermissionDenied("Only a planning role may review qualifications.")

    def _review(self, request, *, review_status):
        from django.utils import timezone
        from backend.apps.scheduling.services.staffing_configuration import (
            invalidate_teacher_rosters,
        )

        self._require_planning_role()
        serializer = TeacherQualificationReviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        reason = serializer.validated_data["reason"].strip()
        if review_status == QUALIFICATION_REVIEW_REJECTED and not reason:
            raise ValidationError({"reason": "A rejection reason is required."})
        item = self.get_object()
        item.review_status = review_status
        item.reviewed_by = request.user
        item.reviewed_at = timezone.now()
        item.review_reason = reason
        item.save(update_fields=[
            "review_status", "reviewed_by", "reviewed_at", "review_reason",
        ])
        invalidate_teacher_rosters(item.teacher_id)
        return Response(TeacherQualificationSerializer(item, context=self.get_serializer_context()).data)

    @action(detail=True, methods=["post"])
    def verify(self, request, **kwargs):
        return self._review(request, review_status=QUALIFICATION_REVIEW_VERIFIED)

    @action(detail=True, methods=["post"])
    def reject(self, request, **kwargs):
        return self._review(request, review_status=QUALIFICATION_REVIEW_REJECTED)


class TeacherCoursePreferenceViewSet(TeacherNestedViewSet):
    queryset = TeacherCoursePreference.objects.select_related("teacher__user", "course")
    serializer_class = TeacherCoursePreferenceSerializer
    filter_fields = ("course",)


class TeacherCurrentCourseViewSet(TeacherNestedViewSet):
    queryset = TeacherCurrentCourse.objects.select_related("teacher__user", "course", "academic_year")
    serializer_class = TeacherCurrentCourseSerializer
    filter_fields = ("course", "academic_year")


class TeacherAvailabilityViewSet(TeacherNestedViewSet):
    queryset = TeacherAvailability.objects.select_related("teacher__user", "timeslot")
    serializer_class = TeacherAvailabilitySerializer
    filter_fields = ("timeslot",)


class QualificationViewSet(SharedConstraintViewSet):
    queryset = Qualification.objects.all()
    serializer_class = QualificationSerializer
    filter_fields = ("kind", "subject_code", "division")


class HardConstraintViewSet(SharedConstraintViewSet):
    queryset = HardConstraint.objects.all()
    serializer_class = HardConstraintSerializer
    filter_fields = ("type",)


class SoftConstraintViewSet(SharedConstraintViewSet):
    queryset = SoftConstraint.objects.all()
    serializer_class = SoftConstraintSerializer
    filter_fields = ("category",)


class CounselorConstraintPreferenceViewSet(SharedConstraintViewSet):
    queryset = CounselorConstraintPreference.objects.select_related("counselor", "constraint")
    serializer_class = CounselorConstraintPreferenceSerializer
    filter_fields = ("counselor", "constraint")


class CourseConflictViewSet(SharedConstraintViewSet):
    queryset = CourseConflict.objects.select_related("course_a", "course_b")
    serializer_class = CourseConflictSerializer
    filter_fields = ("course_a", "course_b")


class CourseRoomRequirementViewSet(SharedConstraintViewSet):
    queryset = CourseRoomRequirement.objects.select_related("course")
    serializer_class = CourseRoomRequirementSerializer
    filter_fields = ("course", "room_type")


class CourseQualificationRequirementViewSet(SharedConstraintViewSet):
    queryset = CourseQualificationRequirement.objects.select_related("course", "qualification")
    serializer_class = CourseQualificationRequirementSerializer
    filter_fields = ("course", "qualification", "enforcement")


class SectionLockView(APIView):
    """Read or upsert the one lock record associated with a section."""

    permission_classes = [ResourcePolicyPermission]
    resource_policy_class = PlanningResourcePolicy

    def get_section(self):
        # Course is selected because qualification validation immediately needs
        # the section's course.
        return get_object_or_404(
            Section.objects.select_related("course", "delivery_group").prefetch_related(
                "delivery_group__offerings__course"
            ),
            pk=self.kwargs["section_id"],
        )

    def get(self, request, section_id):
        lock = get_object_or_404(SectionLock, section=self.get_section())
        return Response(SectionLockSerializer(lock, context={"section": lock.section}).data)

    def patch(self, request, section_id):
        section = self.get_section()
        if section.lifecycle_status == SECTION_LIFECYCLE_RETIRED:
            raise RetiredSectionConflict({
                "detail": "Retired sections are read-only and cannot receive scheduling locks."
            })
        lock = SectionLock.objects.filter(section=section).first()
        created = lock is None
        if lock is None:
            # Build an unsaved instance so the same partial serializer handles
            # first creation and subsequent updates.
            lock = SectionLock(section=section)
        serializer = SectionLockSerializer(lock, data=request.data, partial=True, context={"section": section})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        # Communicate whether PATCH created the singleton or updated it.
        return Response(serializer.data, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)
