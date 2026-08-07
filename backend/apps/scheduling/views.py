from backend.apps.common.views import ReferenceDataViewSet
from backend.apps.scheduling.models import TimeSlot
from backend.apps.scheduling.serializers import TimeSlotSerializer


class TimeSlotViewSet(ReferenceDataViewSet):
    queryset = TimeSlot.objects.select_related("academic_year")
    serializer_class = TimeSlotSerializer
    filter_fields = ("academic_year", "semester", "block", "is_available")
