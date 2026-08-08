from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import APIException, NotFound, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from backend.apps.access.action_policies.demand import DemandPlanningAction, DemandPlanningActionPolicy
from backend.apps.access.permissions import ActionPolicyPermission
from backend.apps.access.permissions import ResourcePolicyPermission
from backend.apps.access.resource_policies.planning import PlanningConfigurationPolicy
from backend.apps.common.models import AcademicYear
from backend.apps.common.views import ReferenceDataViewSet
from backend.apps.scheduling.models import (
    CapacityProfile,
    CoursePriorityProfile,
    SectionPlanningApproval,
    SectionPlanningRun,
    TeacherPlanningCapacity,
    TimeSlot,
)
from backend.apps.scheduling.serializers import (
    CapacityProfileSerializer,
    CourseCapacityPolicySerializer,
    CoursePriorityProfileSerializer,
    SectionCountRecommendationSerializer,
    SectionPlanningApprovalRequestSerializer,
    SectionPlanningApprovalSerializer,
    SectionPlanningRunCreateSerializer,
    SectionPlanningRunSerializer,
    TeacherPlanningCapacitySerializer,
    TimeSlotSerializer,
)
from backend.apps.scheduling.services.engine_adapter import get_section_count_recommendations
from backend.apps.scheduling.services.planning_configuration import apply_course_capacity_policy


class SectionPlanningApprovalConflict(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_code = "section_planning_approval_conflict"


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


class PlanningConfigurationViewSet(viewsets.ModelViewSet):
    permission_classes = [ResourcePolicyPermission]
    resource_policy_class = PlanningConfigurationPolicy
    filter_fields = ()

    def get_queryset(self):
        queryset = self.resource_policy_class.filter_read_queryset(self.request.user, self.queryset.all())
        for field in self.filter_fields:
            if (value := self.request.query_params.get(field)) is not None:
                queryset = queryset.filter(**{field: value})
        return queryset


class CapacityProfileViewSet(PlanningConfigurationViewSet):
    queryset = CapacityProfile.objects.all()
    serializer_class = CapacityProfileSerializer
    filter_fields = ("scope",)

    def perform_destroy(self, instance):
        if instance.courses.exists():
            raise ValidationError({"detail": "A capacity profile assigned to courses cannot be deleted."})
        instance.delete()


class CoursePriorityProfileViewSet(PlanningConfigurationViewSet):
    queryset = CoursePriorityProfile.objects.all()
    serializer_class = CoursePriorityProfileSerializer
    filter_fields = ("tier",)


class TeacherPlanningCapacityViewSet(PlanningConfigurationViewSet):
    queryset = TeacherPlanningCapacity.objects.select_related("teacher", "academic_year")
    serializer_class = TeacherPlanningCapacitySerializer
    filter_fields = ("teacher", "academic_year", "semester")


class CourseCapacityPolicyView(APIView):
    permission_classes = [ActionPolicyPermission]
    action_policy_class = DemandPlanningActionPolicy
    action_name = DemandPlanningAction.MANAGE_PLANNING_CONFIGURATION

    def post(self, request, course_id):
        from backend.apps.courses.models import Course

        try:
            course = Course.objects.get(pk=course_id)
        except Course.DoesNotExist as error:
            raise NotFound("Course not found.") from error
        serializer = CourseCapacityPolicySerializer(data=request.data, context={"course": course})
        serializer.is_valid(raise_exception=True)
        profile = apply_course_capacity_policy(
            course,
            profile=serializer.validated_data.get("capacity_profile"),
            values={key: serializer.validated_data[key] for key in ("hard_min", "soft_min", "target", "soft_max", "hard_max") if key in serializer.validated_data} or None,
        )
        return Response(CapacityProfileSerializer(profile).data)


class SectionPlanningRunViewSet(mixins.CreateModelMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    permission_classes = [ActionPolicyPermission]
    action_policy_class = DemandPlanningActionPolicy
    action_name = DemandPlanningAction.RUN_SECTION_PLANNING
    queryset = SectionPlanningRun.objects.select_related(
        "academic_year",
        "created_by",
    ).prefetch_related(
        "approvals__course_approvals__generated_sections",
    )

    def get_permissions(self):
        if self.action == "approve":
            self.action_name = DemandPlanningAction.APPROVE_SECTION_PLAN
        else:
            self.action_name = DemandPlanningAction.RUN_SECTION_PLANNING
        return super().get_permissions()

    def get_serializer_class(self):
        return SectionPlanningRunCreateSerializer if self.action == "create" else SectionPlanningRunSerializer

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        academic_year_id = serializer.validated_data["academic_year"]
        if not AcademicYear.objects.filter(pk=academic_year_id).exists():
            raise NotFound("Academic year not found.")
        try:
            from backend.apps.scheduling.services.section_planning import create_section_planning_run

            run = create_section_planning_run(
                academic_year_id=academic_year_id,
                created_by=request.user,
                course_constraints=serializer.validated_data["course_constraints"],
                teacher_capacity_adjustments=serializer.validated_data["teacher_capacity_adjustments"],
            )
        except ValueError as error:
            raise ValidationError({"detail": str(error)}) from error
        return Response(SectionPlanningRunSerializer(run).data, status=201)

    @action(detail=True, methods=["get"], url_path="review")
    def review(self, request, pk=None):
        from backend.apps.scheduling.services.section_planning import (
            PlanningApprovalValidationError,
            preview_section_planning_approval,
        )

        run = self.get_object()
        try:
            preview = preview_section_planning_approval(run)
        except PlanningApprovalValidationError as error:
            raise ValidationError(error.detail) from error
        return Response(preview)

    @action(detail=True, methods=["post"], url_path="approval-preview")
    def approval_preview(self, request, pk=None):
        from backend.apps.scheduling.services.section_planning import (
            PlanningApprovalValidationError,
            preview_section_planning_approval,
        )

        serializer = SectionPlanningApprovalRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        run = self.get_object()
        try:
            preview = preview_section_planning_approval(
                run,
                selections=serializer.validated_data.get("courses"),
            )
        except PlanningApprovalValidationError as error:
            raise ValidationError(error.detail) from error
        return Response(preview)

    @action(detail=True, methods=["post"], url_path="approve")
    def approve(self, request, pk=None):
        from backend.apps.scheduling.services.section_planning import (
            PlanningApprovalConflictError,
            PlanningApprovalValidationError,
            approve_section_planning_run,
        )

        serializer = SectionPlanningApprovalRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        run = self.get_object()
        try:
            approval = approve_section_planning_run(
                run,
                approved_by=request.user,
                selections=serializer.validated_data.get("courses"),
                reason=serializer.validated_data["reason"],
            )
        except PlanningApprovalValidationError as error:
            raise ValidationError(error.detail) from error
        except PlanningApprovalConflictError as error:
            raise SectionPlanningApprovalConflict(error.detail) from error
        approval = SectionPlanningApproval.objects.prefetch_related(
            "course_approvals__generated_sections",
        ).get(pk=approval.pk)
        return Response(
            SectionPlanningApprovalSerializer(approval).data,
            status=status.HTTP_201_CREATED,
        )
