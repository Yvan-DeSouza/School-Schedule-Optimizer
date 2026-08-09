"""Policy-filtered CRUD endpoints for scheduling constraints and locks."""

from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import APIException, PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from backend.apps.access.permissions import ResourcePolicyPermission
from backend.apps.access.resource_policies.constraints import PlanningResourcePolicy, TeacherOwnedResourcePolicy
from backend.apps.access.viewsets import PolicyFilteredModelViewSet
from backend.apps.common.exceptions import DomainConflictError, DomainValidationError
from backend.apps.constraints.codes import RETIRED_SECTION_CONFLICT
from backend.apps.constraints.models import (
    CounselorConstraintPreference, CourseConflict, CourseConflictMatrix, CourseQualificationRequirement,
    CourseRoomRequirement, HardConstraint, Qualification, SoftConstraint,
    TeacherAvailability, TeacherCoursePreference, TeacherCurrentCourse, TeacherQualification,
)
from backend.apps.constraints.serializers import (
    CounselorConstraintPreferenceSerializer, CourseConflictAdjustSerializer,
    CourseConflictMatrixCreateSerializer, CourseConflictMatrixSerializer, CourseConflictSerializer,
    CourseQualificationRequirementSerializer, CourseRoomRequirementSerializer,
    HardConstraintSerializer, QualificationSerializer, SectionLockSerializer,
    SoftConstraintSerializer, TeacherAvailabilitySerializer, TeacherCoursePreferenceSerializer,
    TeacherCurrentCourseSerializer, TeacherQualificationReviewSerializer,
    TeacherQualificationSerializer,
)
from backend.apps.constraints.qualification_review import review_teacher_qualification
from backend.apps.control.models import SectionLock
from backend.apps.control.services.locks import apply_section_lock
from backend.apps.common.constants import (
    QUALIFICATION_REVIEW_PENDING,
    QUALIFICATION_REVIEW_REJECTED,
    QUALIFICATION_REVIEW_VERIFIED,
)
from backend.apps.courses.models import Section
from backend.apps.people.models import RoleChoices, Teacher
from backend.apps.people.roles import get_user_role
from backend.apps.access.resource_policies.planning import PlanningConfigurationPolicy


class RetiredSectionConflict(APIException):
    """Prevent new hard-lock state from being attached to retired history."""

    status_code = status.HTTP_409_CONFLICT
    default_code = RETIRED_SECTION_CONFLICT


class PolicyFilteredViewSet(PolicyFilteredModelViewSet):
    """Local alias preserving constraint-view naming while sharing the contract."""


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
        self._require_planning_role()
        serializer = TeacherQualificationReviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            item = review_teacher_qualification(
                self.get_object(),
                actor=request.user,
                review_status=review_status,
                reason=serializer.validated_data["reason"],
            )
        except DomainValidationError as error:
            raise ValidationError(error.detail) from error
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
    """Read matrix rows; all score changes go through the audited action."""

    queryset = CourseConflict.objects.select_related("matrix", "course_a", "course_b").filter(matrix__isnull=False)
    serializer_class = CourseConflictSerializer
    filter_fields = ("matrix", "course_a", "course_b")

    def create(self, request, *args, **kwargs):
        # Preserve a useful 400 for legacy clients while refusing the old
        # untracked mutable conflict write path.
        raise ValidationError({"detail": "Create a yearly conflict matrix, then use its audited adjust action."})

    def update(self, request, *args, **kwargs):
        raise ValidationError({"detail": "Conflict scores are changed only through matrix adjustments."})

    def partial_update(self, request, *args, **kwargs):
        return self.update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        raise ValidationError({"detail": "Conflict matrix rows are retained as planning history."})


class CourseConflictMatrixViewSet(PolicyFilteredViewSet):
    """Set up, inspect, refresh, and audit counselor co-request matrices."""

    resource_policy_class = PlanningConfigurationPolicy
    queryset = CourseConflictMatrix.objects.select_related(
        "academic_year", "source_matrix", "created_by", "refreshed_by",
    )
    serializer_class = CourseConflictMatrixSerializer
    filter_fields = ("academic_year",)

    def get_serializer_class(self):
        return CourseConflictMatrixCreateSerializer if self.action == "create" else CourseConflictMatrixSerializer

    def create(self, request, *args, **kwargs):
        from backend.apps.constraints.conflict_matrix import create_course_conflict_matrix

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            matrix = create_course_conflict_matrix(actor=request.user, **serializer.validated_data)
        except DomainValidationError as error:
            raise ValidationError(error.detail) from error
        except DomainConflictError as error:
            raise RetiredSectionConflict(error.detail) from error
        return Response(CourseConflictMatrixSerializer(matrix).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["get"])
    def grid(self, request, pk=None):
        """Render symmetric logical matrix data without storing mirrored rows."""

        matrix = self.get_object()
        conflicts = CourseConflict.objects.filter(matrix=matrix).select_related("course_a", "course_b")
        course_ids = sorted({
            identifier
            for item in conflicts
            for identifier in (item.course_a_id, item.course_b_id)
        })
        return Response({
            "matrix": CourseConflictMatrixSerializer(matrix).data,
            "course_ids": course_ids,
            "conflicts": CourseConflictSerializer(conflicts, many=True).data,
        })

    @action(detail=True, methods=["post"])
    def refresh(self, request, pk=None):
        from backend.apps.constraints.conflict_matrix import refresh_course_conflict_matrix

        try:
            matrix = refresh_course_conflict_matrix(self.get_object(), actor=request.user)
        except DomainValidationError as error:
            raise ValidationError(error.detail) from error
        return Response(CourseConflictMatrixSerializer(matrix).data)

    @action(detail=True, methods=["post"], url_path=r"conflicts/(?P<conflict_id>[^/.]+)/adjust")
    def adjust(self, request, pk=None, conflict_id=None):
        from backend.apps.constraints.conflict_matrix import adjust_course_conflict

        serializer = CourseConflictAdjustSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            conflict = adjust_course_conflict(
                matrix=self.get_object(), conflict_id=conflict_id, actor=request.user,
                new_weight=serializer.validated_data["weight"], reason=serializer.validated_data["reason"],
            )
        except DomainValidationError as error:
            raise ValidationError(error.detail) from error
        return Response(CourseConflictSerializer(conflict).data)


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
        lock = SectionLock.objects.filter(section=section).first()
        created = lock is None
        if lock is None:
            # Build an unsaved instance so the same partial serializer handles
            # first creation and subsequent updates.
            lock = SectionLock(section=section)
        serializer = SectionLockSerializer(lock, data=request.data, partial=True, context={"section": section})
        serializer.is_valid(raise_exception=True)
        try:
            lock, created = apply_section_lock(
                section,
                locked_teacher=serializer.validated_data.get(
                    "locked_teacher",
                    lock.locked_teacher,
                ),
                locked_timeslot=serializer.validated_data.get(
                    "locked_timeslot",
                    lock.locked_timeslot,
                ),
                locked_room=serializer.validated_data.get(
                    "locked_room",
                    lock.locked_room,
                ),
            )
        except DomainValidationError as error:
            raise ValidationError(error.detail) from error
        except DomainConflictError as error:
            raise RetiredSectionConflict(error.detail) from error
        # Communicate whether PATCH created the singleton or updated it.
        return Response(
            SectionLockSerializer(lock, context={"section": section}).data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )
