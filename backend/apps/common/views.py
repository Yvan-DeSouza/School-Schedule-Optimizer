"""Policy-filtered CRUD for school-wide reference data."""

from backend.apps.access.viewsets import PolicyFilteredModelViewSet
from backend.apps.common.exceptions import DomainValidationError
from backend.apps.access.resource_policies.reference_data import ReferenceDataPolicy
from backend.apps.common.models import AcademicYear, Room
from backend.apps.common.serializers import AcademicYearSerializer, RoomSerializer
from backend.apps.common.services.reference_data import ensure_reference_data_can_be_deleted
from rest_framework.exceptions import ValidationError


class ReferenceDataViewSet(PolicyFilteredModelViewSet):
    """Shared base enforcing policy-first filtering and guarded deletion."""

    resource_policy_class = ReferenceDataPolicy

    def perform_destroy(self, instance):
        # Guard before delete so callers receive a domain explanation rather than
        # an opaque protected/cascade side effect.
        try:
            ensure_reference_data_can_be_deleted(instance)
        except DomainValidationError as error:
            raise ValidationError(error.detail) from error
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
