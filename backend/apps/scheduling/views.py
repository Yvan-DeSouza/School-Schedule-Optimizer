from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from backend.apps.access.action_policies.demand import DemandPlanningAction, DemandPlanningActionPolicy
from backend.apps.access.permissions import ActionPolicyPermission
from backend.apps.common.models import AcademicYear
from backend.apps.common.views import ReferenceDataViewSet
from backend.apps.scheduling.models import TimeSlot
from backend.apps.scheduling.serializers import SectionCountRecommendationSerializer, TimeSlotSerializer
from backend.apps.scheduling.services.engine_adapter import get_section_count_recommendations


class TimeSlotViewSet(ReferenceDataViewSet):
    queryset = TimeSlot.objects.select_related("academic_year")
    serializer_class = TimeSlotSerializer
    filter_fields = ("academic_year", "semester", "block", "is_available")


class SectionCountRecommendationView(APIView):
    permission_classes = [ActionPolicyPermission]
    action_policy_class = DemandPlanningActionPolicy
    action_name = DemandPlanningAction.RECOMMEND_SECTION_COUNTS

    def get(self, request):
        academic_year_id = request.query_params.get("academic_year")
        if not academic_year_id or not str(academic_year_id).isdigit():
            raise ValidationError({"academic_year": "This query parameter must be a valid academic year id."})
        if not AcademicYear.objects.filter(pk=academic_year_id).exists():
            raise NotFound("Academic year not found.")
        try:
            recommendations = get_section_count_recommendations(academic_year_id)
        except ValueError as error:
            raise ValidationError({"detail": str(error)}) from error
        return Response(SectionCountRecommendationSerializer(recommendations, many=True).data)
