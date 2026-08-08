"""HTTP endpoints for timeslots, planning configuration, runs, and approvals.

Views remain thin: policies authorize actions, serializers validate transport
data, and services own business rules/transactions. Imports of the pure engine
are restricted to ``engine_adapter`` by project convention.
"""

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
    SectionPlanningReconciliationApplySerializer,
    SectionPlanningReconciliationSerializer,
    SectionPlanningRunCreateSerializer,
    SectionPlanningRunSerializer,
    TeacherPlanningCapacitySerializer,
    TimeSlotSerializer,
)
from backend.apps.scheduling.services.engine_adapter import get_section_count_recommendations
from backend.apps.scheduling.services.planning_configuration import apply_course_capacity_policy


class SectionPlanningApprovalConflict(APIException):
    """HTTP 409 used when approval would overwrite existing decisions."""

    status_code = status.HTTP_409_CONFLICT
    default_code = "section_planning_approval_conflict"


class TimeSlotViewSet(ReferenceDataViewSet):
    """CRUD recurring A-D slots using the shared reference-data policy."""

    queryset = TimeSlot.objects.select_related("academic_year")
    serializer_class = TimeSlotSerializer
    filter_fields = ("academic_year", "semester", "block", "is_available")


class SectionCountRecommendationView(APIView):
    """Legacy read-only per-course heuristic recommendation endpoint."""

    permission_classes = [ActionPolicyPermission]
    action_policy_class = DemandPlanningActionPolicy
    action_name = DemandPlanningAction.RECOMMEND_SECTION_COUNTS

    def get(self, request):
        academic_year_id = request.query_params.get("academic_year")
        # Validate syntax separately from existence to provide useful 400 vs 404
        # responses rather than leaking ORM conversion exceptions.
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
    """Shared base applying role-safe policy filtering before query filters."""

    permission_classes = [ResourcePolicyPermission]
    resource_policy_class = PlanningConfigurationPolicy
    filter_fields = ()

    def get_queryset(self):
        # Policy filtering must happen first. Query parameters may narrow an
        # authorized set but must never broaden it.
        queryset = self.resource_policy_class.filter_read_queryset(self.request.user, self.queryset.all())
        for field in self.filter_fields:
            if (value := self.request.query_params.get(field)) is not None:
                queryset = queryset.filter(**{field: value})
        return queryset


class CapacityProfileViewSet(PlanningConfigurationViewSet):
    """Manage reusable and course-specific class-size profiles."""

    queryset = CapacityProfile.objects.all()
    serializer_class = CapacityProfileSerializer
    filter_fields = ("scope",)

    def perform_destroy(self, instance):
        # Course FKs are PROTECTed as a database backstop; this precheck returns a
        # counselor-readable API error.
        if instance.courses.exists():
            raise ValidationError({"detail": "A capacity profile assigned to courses cannot be deleted."})
        instance.delete()


class CoursePriorityProfileViewSet(PlanningConfigurationViewSet):
    """Manage explicit four-tier course-demand priorities."""

    queryset = CoursePriorityProfile.objects.all()
    serializer_class = CoursePriorityProfileSerializer
    filter_fields = ("tier",)


class TeacherPlanningCapacityViewSet(PlanningConfigurationViewSet):
    """Manage per-teacher, year, and semester planning ceilings."""

    queryset = TeacherPlanningCapacity.objects.select_related("teacher", "academic_year")
    serializer_class = TeacherPlanningCapacitySerializer
    filter_fields = ("teacher", "academic_year", "semester")


class CourseCapacityPolicyView(APIView):
    """Attach a shared profile or apply custom copy-on-write values."""

    permission_classes = [ActionPolicyPermission]
    action_policy_class = DemandPlanningActionPolicy
    action_name = DemandPlanningAction.MANAGE_PLANNING_CONFIGURATION

    def post(self, request, course_id):
        # Local import avoids making this view module part of the Course model's
        # default-profile import cycle.
        from backend.apps.courses.models import Course

        try:
            course = Course.objects.get(pk=course_id)
        except Course.DoesNotExist as error:
            raise NotFound("Course not found.") from error
        serializer = CourseCapacityPolicySerializer(data=request.data, context={"course": course})
        serializer.is_valid(raise_exception=True)
        # Service owns copy-on-write behavior and its transaction boundary.
        profile = apply_course_capacity_policy(
            course,
            profile=serializer.validated_data.get("capacity_profile"),
            values={key: serializer.validated_data[key] for key in ("hard_min", "soft_min", "target", "soft_max", "hard_max") if key in serializer.validated_data} or None,
        )
        return Response(CapacityProfileSerializer(profile).data)


class SectionPlanningRunViewSet(mixins.CreateModelMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """Create/read immutable runs and review/approve their recommendations."""

    permission_classes = [ActionPolicyPermission]
    action_policy_class = DemandPlanningActionPolicy
    action_name = DemandPlanningAction.RUN_SECTION_PLANNING
    queryset = SectionPlanningRun.objects.select_related(
        "academic_year",
        "created_by",
    ).prefetch_related(
        # Run responses nest approval lines and generated section IDs. Prefetching
        # prevents one query per approval/course on list and detail responses.
        "approvals__course_approvals__generated_sections",
        "approvals__reconciliation__course_reconciliations__approval_course",
        "approvals__reconciliation__course_reconciliations__actions",
    )

    def get_permissions(self):
        # Approval is a distinct action even though current planning roles may do
        # both. Keeping policy names separate supports future role refinement.
        if self.action in {"approve", "reconciliation_preview", "reconcile"}:
            self.action_name = DemandPlanningAction.APPROVE_SECTION_PLAN
        else:
            self.action_name = DemandPlanningAction.RUN_SECTION_PLANNING
        return super().get_permissions()

    def get_serializer_class(self):
        # Create input is intentionally much smaller than the immutable run
        # representation returned by list/retrieve.
        return SectionPlanningRunCreateSerializer if self.action == "create" else SectionPlanningRunSerializer

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        academic_year_id = serializer.validated_data["academic_year"]
        if not AcademicYear.objects.filter(pk=academic_year_id).exists():
            raise NotFound("Academic year not found.")
        try:
            # Deferred import keeps the view lightweight and preserves the
            # adapter as the only Django-to-engine boundary.
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
        """Preview every not-yet-approved recommendation without writing."""

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
        """Preview a selected/adjusted subset using the approval request shape."""

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
        """Atomically create draft sections and return the immutable audit row."""

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
            # Invalid current semester rules/configuration are ordinary 400s.
            raise ValidationError(error.detail) from error
        except PlanningApprovalConflictError as error:
            # Existing sections or prior approvals require reconciliation, so
            # communicate a state conflict rather than malformed input.
            raise SectionPlanningApprovalConflict(error.detail) from error
        # Reload with generated sections prefetched so the 201 response contains
        # complete audit provenance without N+1 queries.
        approval = SectionPlanningApproval.objects.prefetch_related(
            "course_approvals__generated_sections",
        ).get(pk=approval.pk)
        return Response(
            SectionPlanningApprovalSerializer(approval).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"], url_path="reconciliation-preview")
    def reconciliation_preview(self, request, pk=None):
        """Describe how revised approved counts would affect existing sections."""

        from backend.apps.scheduling.services.section_reconciliation import (
            preview_section_plan_reconciliation,
        )
        from backend.apps.scheduling.services.section_planning import (
            PlanningApprovalValidationError,
        )

        serializer = SectionPlanningApprovalRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            preview = preview_section_plan_reconciliation(
                self.get_object(),
                selections=serializer.validated_data.get("courses"),
            )
        except PlanningApprovalValidationError as error:
            raise ValidationError(error.detail) from error
        return Response(preview)

    @action(detail=True, methods=["post"], url_path="reconcile")
    def reconcile(self, request, pk=None):
        """Apply a reviewed reconciliation atomically and return its audit graph."""

        from backend.apps.scheduling.services.section_reconciliation import (
            reconcile_section_planning_run,
        )
        from backend.apps.scheduling.services.section_planning import (
            PlanningApprovalConflictError,
            PlanningApprovalValidationError,
        )

        serializer = SectionPlanningReconciliationApplySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            reconciliation = reconcile_section_planning_run(
                self.get_object(),
                reconciled_by=request.user,
                preview_token=serializer.validated_data["preview_token"],
                selections=serializer.validated_data.get("courses"),
                reason=serializer.validated_data["reason"],
            )
        except PlanningApprovalValidationError as error:
            raise ValidationError(error.detail) from error
        except PlanningApprovalConflictError as error:
            raise SectionPlanningApprovalConflict(error.detail) from error
        reconciliation = (
            type(reconciliation).objects
            .select_related("approval")
            .prefetch_related(
                "course_reconciliations__approval_course",
                "course_reconciliations__actions__section",
            )
            .get(pk=reconciliation.pk)
        )
        return Response(
            SectionPlanningReconciliationSerializer(reconciliation).data,
            status=status.HTTP_201_CREATED,
        )
