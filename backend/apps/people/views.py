"""Counselor/administrator teacher-directory endpoints."""

from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from backend.apps.access.permissions import ResourcePolicyPermission
from backend.apps.access.resource_policies.planning import PlanningConfigurationPolicy
from backend.apps.people.models import Teacher, TeacherStatusDecision
from backend.apps.people.serializers import TeacherArchiveSerializer, TeacherSerializer
from backend.apps.scheduling.services.staffing_configuration import (
    invalidate_teacher_rosters,
)


class TeacherViewSet(viewsets.ModelViewSet):
    """Planning-role CRUD with recoverable archive instead of hard deletion."""

    permission_classes = [ResourcePolicyPermission]
    resource_policy_class = PlanningConfigurationPolicy
    queryset = Teacher.objects.select_related("user")
    serializer_class = TeacherSerializer

    def get_queryset(self):
        queryset = self.resource_policy_class.filter_read_queryset(
            self.request.user,
            self.queryset.all(),
        )
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
        teacher = self.get_object()
        if teacher.is_archived:
            raise ValidationError({"detail": "This teacher is already archived."})
        teacher.is_archived = True
        teacher.save(update_fields=["is_archived"])
        TeacherStatusDecision.objects.create(
            teacher=teacher,
            action="archived",
            reason=serializer.validated_data["reason"],
            decided_by=request.user,
        )
        invalidate_teacher_rosters(teacher.id)
        return Response(TeacherSerializer(teacher).data)

    @action(detail=True, methods=["post"])
    def restore(self, request, pk=None):
        serializer = TeacherArchiveSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        teacher = self.get_object()
        if not teacher.is_archived:
            raise ValidationError({"detail": "This teacher is not archived."})
        teacher.is_archived = False
        teacher.save(update_fields=["is_archived"])
        TeacherStatusDecision.objects.create(
            teacher=teacher,
            action="restored",
            reason=serializer.validated_data["reason"],
            decided_by=request.user,
        )
        return Response(TeacherSerializer(teacher).data, status=status.HTTP_200_OK)
