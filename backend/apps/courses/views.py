"""Policy-filtered course, section, request, and demand-summary endpoints."""

from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import APIException
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from backend.apps.access.action_policies.demand import DemandPlanningAction, DemandPlanningActionPolicy
from backend.apps.access.permissions import ActionPolicyPermission, ResourcePolicyPermission
from backend.apps.access.resource_policies.courses import CoursePolicy, CourseRequestPolicy, SectionPolicy
from backend.apps.access.resource_policies.planning import PlanningConfigurationPolicy
from backend.apps.access.viewsets import PolicyFilteredModelViewSet
from backend.apps.common.models import AcademicYear
from backend.apps.common.constants import SECTION_LIFECYCLE_ACTIVE
from backend.apps.courses.codes import SECTION_STATE_CONFLICT
from backend.apps.courses.models import (
    Course,
    CourseCombinationRule,
    CourseOffering,
    CourseRequest,
    DeliveryGroup,
    Section,
)
from backend.apps.courses.serializers import (
    CombineOfferingsRequestSerializer,
    CourseCombinationRuleSerializer,
    CourseOfferingSerializer,
    CourseRequestSerializer,
    CourseSerializer,
    DeliveryGroupSerializer,
    OfferingDecisionRequestSerializer,
    SectionSerializer,
)
from backend.apps.courses.services.section_state import section_delete_conflicts
from backend.apps.courses.services.demand import get_course_demand_summary
from backend.apps.people.models import RoleChoices
from backend.apps.people.roles import get_user_role


class SectionStateConflict(APIException):
    """HTTP 409 for deletion or mutation that would bypass section history."""

    status_code = status.HTTP_409_CONFLICT
    default_code = SECTION_STATE_CONFLICT


class PolicyFilteredViewSet(PolicyFilteredModelViewSet):
    """Local alias preserving course-view naming while sharing the contract."""


class CourseViewSet(PolicyFilteredViewSet):
    """Catalog CRUD with broad read and planning-role write access."""

    queryset = Course.objects.all()
    serializer_class = CourseSerializer
    resource_policy_class = CoursePolicy
    filter_fields = ("grade_level", "category", "is_online")


class SectionViewSet(PolicyFilteredViewSet):
    """Operational section CRUD including approval provenance joins."""

    queryset = Section.objects.select_related(
        "course",
        "academic_year",
        "teacher__user",
        "planning_approval_course__approval",
        "staffing_approval_offering__approval",
        "delivery_group",
    ).prefetch_related(
        # SectionSerializer exposes member course codes for combined groups.
        # Prefetching here keeps list endpoints from querying each delivery
        # group and member course separately.
        "delivery_group__offerings__course",
    )
    serializer_class = SectionSerializer
    resource_policy_class = SectionPolicy
    filter_fields = (
        "academic_year",
        "course",
        "delivery_group",
        "semester",
        "teacher",
        "lifecycle_status",
    )

    def get_queryset(self):
        queryset = super().get_queryset()
        # Normal operational lists contain only sections that can still be
        # staffed/scheduled. An explicit lifecycle filter lets authorized users
        # inspect retired audit rows without mixing them into daily work.
        if self.action == "list" and "lifecycle_status" not in self.request.query_params:
            queryset = queryset.filter(lifecycle_status=SECTION_LIFECYCLE_ACTIVE)
        return queryset

    def perform_destroy(self, instance):
        # Manual, dependency-free sections may still be removed. Everything
        # created or touched by planning, or referenced by downstream work, is
        # retained so approvals and schedules never point to missing facts.
        conflicts = section_delete_conflicts(instance)
        if conflicts:
            raise SectionStateConflict({
                "detail": "This section is audited or has dependent scheduling data and cannot be deleted.",
                "conflicts": conflicts,
            })
        instance.delete()


class CourseRequestViewSet(PolicyFilteredViewSet):
    """Student-owned/planning-managed course demand records."""

    queryset = CourseRequest.objects.select_related("student__user", "course", "academic_year")
    serializer_class = CourseRequestSerializer
    resource_policy_class = CourseRequestPolicy
    filter_fields = ("academic_year", "student", "course", "request_type")

    def perform_create(self, serializer):
        if get_user_role(self.request.user) == RoleChoices.STUDENT:
            # Enforce authenticated ownership even if request JSON omits student.
            serializer.save(student=self.request.user.student_profile)
        else:
            serializer.save()


class CourseOfferingViewSet(PolicyFilteredViewSet):
    """Inspect offering state and explicitly cancel or restore one course/year."""

    queryset = CourseOffering.objects.select_related(
        "course",
        "academic_year",
        "delivery_group",
        "decided_by",
    ).prefetch_related(
        # Offering responses show the complete physical delivery membership.
        "delivery_group__offerings__course",
    )
    serializer_class = CourseOfferingSerializer
    resource_policy_class = PlanningConfigurationPolicy
    filter_fields = ("academic_year", "course", "status", "delivery_group")
    http_method_names = ("get", "post", "head", "options")

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        from backend.apps.courses.services.offerings import (
            OfferingConflictError,
            OfferingValidationError,
            cancel_course_offering,
        )

        serializer = OfferingDecisionRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            offering = cancel_course_offering(
                self.get_object(),
                actor=request.user,
                reason=serializer.validated_data["reason"],
            )
        except OfferingValidationError as error:
            raise ValidationError(error.detail) from error
        except OfferingConflictError as error:
            raise SectionStateConflict(error.detail) from error
        return Response(CourseOfferingSerializer(offering).data)

    @action(detail=True, methods=["post"])
    def restore(self, request, pk=None):
        from backend.apps.courses.services.offerings import (
            OfferingConflictError,
            OfferingValidationError,
            restore_course_offering,
        )

        serializer = OfferingDecisionRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            offering = restore_course_offering(
                self.get_object(),
                actor=request.user,
                reason=serializer.validated_data["reason"],
            )
        except OfferingValidationError as error:
            raise ValidationError(error.detail) from error
        except OfferingConflictError as error:
            raise SectionStateConflict(error.detail) from error
        return Response(CourseOfferingSerializer(offering).data)


class CourseCombinationRuleViewSet(PolicyFilteredViewSet):
    queryset = CourseCombinationRule.objects.select_related("capacity_profile").prefetch_related(
        "members__course"
    )
    serializer_class = CourseCombinationRuleSerializer
    resource_policy_class = PlanningConfigurationPolicy
    filter_fields = ("is_active",)


class DeliveryGroupViewSet(PolicyFilteredViewSet):
    queryset = DeliveryGroup.objects.select_related(
        "academic_year", "capacity_profile", "combination_rule", "created_by"
    ).prefetch_related("offerings__course")
    serializer_class = DeliveryGroupSerializer
    resource_policy_class = PlanningConfigurationPolicy
    filter_fields = ("academic_year", "status", "combination_rule")
    http_method_names = ("get", "post", "head", "options")

    @action(detail=True, methods=["post"])
    def separate(self, request, pk=None):
        from backend.apps.courses.services.offerings import (
            OfferingConflictError,
            OfferingValidationError,
            separate_delivery_group,
        )

        serializer = OfferingDecisionRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            separate_delivery_group(
                self.get_object(),
                actor=request.user,
                reason=serializer.validated_data["reason"],
            )
        except OfferingValidationError as error:
            raise ValidationError(error.detail) from error
        except OfferingConflictError as error:
            raise SectionStateConflict(error.detail) from error
        return Response({"detail": "The combined offering was separated."})


class CombinationSuggestionView(APIView):
    permission_classes = [ResourcePolicyPermission]
    resource_policy_class = PlanningConfigurationPolicy

    def get(self, request):
        academic_year_id = request.query_params.get("academic_year")
        if not academic_year_id or not str(academic_year_id).isdigit():
            raise ValidationError({"academic_year": "A valid academic year id is required."})
        academic_year = get_object_or_404(AcademicYear, pk=academic_year_id)
        from backend.apps.courses.services.offerings import get_combination_suggestions

        return Response(get_combination_suggestions(academic_year))


class CombineOfferingsView(APIView):
    permission_classes = [ResourcePolicyPermission]
    resource_policy_class = PlanningConfigurationPolicy

    def post(self, request):
        from backend.apps.courses.services.offerings import (
            OfferingConflictError,
            OfferingValidationError,
            combine_course_offerings,
            ensure_academic_year_offerings,
        )

        serializer = CombineOfferingsRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        academic_year = get_object_or_404(
            AcademicYear,
            pk=serializer.validated_data["academic_year"],
        )
        rule = get_object_or_404(
            CourseCombinationRule,
            pk=serializer.validated_data["rule_id"],
        )
        ensure_academic_year_offerings(academic_year, actor=request.user)
        try:
            group = combine_course_offerings(
                rule,
                academic_year,
                actor=request.user,
                reason=serializer.validated_data["reason"],
            )
        except OfferingValidationError as error:
            raise ValidationError(error.detail) from error
        except OfferingConflictError as error:
            raise SectionStateConflict(error.detail) from error
        return Response(DeliveryGroupSerializer(group).data, status=status.HTTP_201_CREATED)


class DemandSummaryView(APIView):
    """Read-only database aggregation of request totals for one year."""

    permission_classes = [ActionPolicyPermission]
    action_policy_class = DemandPlanningActionPolicy
    action_name = DemandPlanningAction.VIEW_DEMAND_SUMMARY

    def get(self, request):
        academic_year_id = request.query_params.get("academic_year")
        if not academic_year_id:
            raise ValidationError({"academic_year": "This query parameter is required."})
        if not str(academic_year_id).isdigit():
            # Keep invalid syntax as 400 and a missing numeric ID as 404.
            raise ValidationError({"academic_year": "Must be a valid academic year id."})
        if not AcademicYear.objects.filter(pk=academic_year_id).exists():
            raise NotFound("Academic year not found.")
        return Response(get_course_demand_summary(academic_year_id))
