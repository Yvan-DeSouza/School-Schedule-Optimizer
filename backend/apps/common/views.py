from rest_framework import viewsets

from backend.apps.access.permissions import ResourcePolicyPermission
from backend.apps.access.resource_policies.reference_data import ReferenceDataPolicy
from backend.apps.common.models import AcademicYear, Room
from backend.apps.common.serializers import AcademicYearSerializer, RoomSerializer
from backend.apps.common.services.reference_data import ensure_reference_data_can_be_deleted


class ReferenceDataViewSet(viewsets.ModelViewSet):
    permission_classes = [ResourcePolicyPermission]
    resource_policy_class = ReferenceDataPolicy
    filter_fields = ()

    def get_queryset(self):
        queryset = self.resource_policy_class.filter_read_queryset(self.request.user, self.queryset.all())
        for field in self.filter_fields:
            value = self.request.query_params.get(field)
            if value is not None:
                queryset = queryset.filter(**{field: value})
        return queryset

    def perform_destroy(self, instance):
        ensure_reference_data_can_be_deleted(instance)
        instance.delete()


class AcademicYearViewSet(ReferenceDataViewSet):
    queryset = AcademicYear.objects.all()
    serializer_class = AcademicYearSerializer
    filter_fields = ("name",)


class RoomViewSet(ReferenceDataViewSet):
    queryset = Room.objects.all()
    serializer_class = RoomSerializer
    filter_fields = ("room_type", "is_specialized")
