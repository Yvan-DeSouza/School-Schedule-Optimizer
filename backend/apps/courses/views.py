from rest_framework import viewsets
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from backend.apps.access.action_policies.demand import DemandPlanningAction, DemandPlanningActionPolicy
from backend.apps.access.permissions import ActionPolicyPermission, ResourcePolicyPermission
from backend.apps.access.resource_policies.courses import CoursePolicy, CourseRequestPolicy, SectionPolicy
from backend.apps.common.models import AcademicYear
from backend.apps.courses.models import Course, CourseRequest, Section
from backend.apps.courses.serializers import CourseRequestSerializer, CourseSerializer, SectionSerializer
from backend.apps.courses.services.demand import get_course_demand_summary
from backend.apps.people.models import RoleChoices
from backend.apps.people.roles import get_user_role


class PolicyFilteredViewSet(viewsets.ModelViewSet):
    permission_classes = [ResourcePolicyPermission]
    filter_fields = ()

    def get_queryset(self):
        queryset = self.resource_policy_class.filter_read_queryset(self.request.user, self.queryset.all())
        for field in self.filter_fields:
            value = self.request.query_params.get(field)
            if value is not None:
                queryset = queryset.filter(**{field: value})
        return queryset


class CourseViewSet(PolicyFilteredViewSet):
    queryset = Course.objects.all()
    serializer_class = CourseSerializer
    resource_policy_class = CoursePolicy
    filter_fields = ("grade_level", "category", "is_online")


class SectionViewSet(PolicyFilteredViewSet):
    queryset = Section.objects.select_related("course", "academic_year", "teacher__user")
    serializer_class = SectionSerializer
    resource_policy_class = SectionPolicy
    filter_fields = ("academic_year", "course", "semester", "teacher")


class CourseRequestViewSet(PolicyFilteredViewSet):
    queryset = CourseRequest.objects.select_related("student__user", "course", "academic_year")
    serializer_class = CourseRequestSerializer
    resource_policy_class = CourseRequestPolicy
    filter_fields = ("academic_year", "student", "course", "request_type")

    def perform_create(self, serializer):
        if get_user_role(self.request.user) == RoleChoices.STUDENT:
            serializer.save(student=self.request.user.student_profile)
        else:
            serializer.save()


class DemandSummaryView(APIView):
    permission_classes = [ActionPolicyPermission]
    action_policy_class = DemandPlanningActionPolicy
    action_name = DemandPlanningAction.VIEW_DEMAND_SUMMARY

    def get(self, request):
        academic_year_id = request.query_params.get("academic_year")
        if not academic_year_id:
            raise ValidationError({"academic_year": "This query parameter is required."})
        if not str(academic_year_id).isdigit():
            raise ValidationError({"academic_year": "Must be a valid academic year id."})
        if not AcademicYear.objects.filter(pk=academic_year_id).exists():
            raise NotFound("Academic year not found.")
        return Response(get_course_demand_summary(academic_year_id))
