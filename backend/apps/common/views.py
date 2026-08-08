"""Policy-filtered CRUD for school-wide reference data."""

from rest_framework import viewsets

from backend.apps.access.permissions import ResourcePolicyPermission
from backend.apps.access.resource_policies.reference_data import ReferenceDataPolicy
from backend.apps.common.models import AcademicYear, Room
from backend.apps.common.serializers import AcademicYearSerializer, RoomSerializer
from backend.apps.common.services.reference_data import ensure_reference_data_can_be_deleted


class ReferenceDataViewSet(viewsets.ModelViewSet):
    """Shared base enforcing policy-first filtering and guarded deletion."""

    permission_classes = [ResourcePolicyPermission]
    resource_policy_class = ReferenceDataPolicy
    filter_fields = ()

    def get_queryset(self):
        # User filters may narrow only the already authorized queryset.
        queryset = self.resource_policy_class.filter_read_queryset(self.request.user, self.queryset.all())
        for field in self.filter_fields:
            value = self.request.query_params.get(field)
            if value is not None:
                queryset = queryset.filter(**{field: value})
        return queryset

    def perform_destroy(self, instance):
        # Guard before delete so callers receive a domain explanation rather than
        # an opaque protected/cascade side effect.
        ensure_reference_data_can_be_deleted(instance)
        instance.delete()


class AcademicYearViewSet(ReferenceDataViewSet):
    """Academic-year reference CRUD."""

    queryset = AcademicYear.objects.all()
    serializer_class = AcademicYearSerializer
    filter_fields = ("name",)


class RoomViewSet(ReferenceDataViewSet):
    """Room reference CRUD with type/specialization filtering."""

    queryset = Room.objects.all()
    serializer_class = RoomSerializer
    filter_fields = ("room_type", "is_specialized")
