"""Counselor/administrator teacher-directory endpoints."""

from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from backend.apps.access.resource_policies.planning import PlanningConfigurationPolicy
from backend.apps.access.viewsets import PolicyFilteredModelViewSet
from backend.apps.common.exceptions import DomainValidationError
from backend.apps.people.models import Teacher
from backend.apps.people.serializers import TeacherArchiveSerializer, TeacherSerializer
from backend.apps.people.services.teacher_directory import (
    archive_teacher,
    restore_teacher,
)
from backend.apps.scheduling.services.staffing_configuration import (
    invalidate_teacher_rosters,
)


class TeacherViewSet(PolicyFilteredModelViewSet):
    """Planning-role CRUD with recoverable archive instead of hard deletion."""

    resource_policy_class = PlanningConfigurationPolicy
    queryset = Teacher.objects.select_related("user")
    serializer_class = TeacherSerializer

    def get_queryset(self):
        queryset = super().get_queryset()
        archived = self.request.query_params.get("archived")
        if archived in ("true", "false"):
            queryset = queryset.filter(is_archived=archived == "true")
        return queryset

    def perform_update(self, serializer):
        teacher = serializer.save()
        invalidate_teacher_rosters(teacher.id)

    def destroy(self, request, *args, **kwargs):
        raise ValidationError({
            "detail": "Teachers are archived, not deleted, so historical planning remains auditable."
        })

    @action(detail=True, methods=["post"])
    def archive(self, request, pk=None):
        serializer = TeacherArchiveSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            teacher = archive_teacher(
                self.get_object(),
                actor=request.user,
                reason=serializer.validated_data["reason"],
            )
        except DomainValidationError as error:
            raise ValidationError(error.detail) from error
        return Response(TeacherSerializer(teacher).data)

    @action(detail=True, methods=["post"])
    def restore(self, request, pk=None):
        serializer = TeacherArchiveSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            teacher = restore_teacher(
                self.get_object(),
                actor=request.user,
                reason=serializer.validated_data["reason"],
            )
        except DomainValidationError as error:
            raise ValidationError(error.detail) from error
        return Response(TeacherSerializer(teacher).data, status=status.HTTP_200_OK)
