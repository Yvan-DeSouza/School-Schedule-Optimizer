"""HTTP endpoints for timeslots, planning configuration, runs, and approvals.

Views remain thin: policies authorize actions, serializers validate transport
data, and services own business rules/transactions. ORM-to-engine translation
is restricted to the scheduling adapter/snapshot boundary by project convention.
"""

from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import APIException, NotFound, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView
from django.db.models import Count

from backend.apps.access.action_policies.demand import DemandPlanningAction, DemandPlanningActionPolicy
from backend.apps.access.action_policies.scheduling import SchedulingAction, SchedulingActionPolicy
from backend.apps.access.permissions import ActionPolicyPermission
from backend.apps.access.resource_policies.planning import PlanningConfigurationPolicy
from backend.apps.access.viewsets import PolicyFilteredModelViewSet
from backend.apps.common.models import AcademicYear
from backend.apps.common.views import ReferenceDataViewSet
from backend.apps.scheduling.codes import (
    SECTION_PLACEMENT_CONFLICT,
    SECTION_PLANNING_APPROVAL_CONFLICT,
    TEACHER_ASSIGNMENT_CONFLICT,
)
from backend.apps.scheduling.models import (
    CapacityProfile,
    CoursePriorityProfile,
    SectionPlanningApproval,
    SectionPlanningRun,
    SectionPlacementApproval,
    SectionPlacementRun,
    SectionBudgetApproval,
    SectionBudgetRun,
    StaffingPlanApproval,
    StaffingPlanRun,
    TeacherPlanningCapacity,
    TeacherPlanningAnnualCapacity,
    TeacherCourseAssignmentRule,
    TeacherTimePreference,
    TeacherAssignmentRun,
    TeacherAssignmentApproval,
    TeacherPlanningRoster,
    TimeSlot,
    AnnualPlacementLock,
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
    SectionPlacementApprovalRequestSerializer,
    SectionPlacementApprovalSerializer,
    SectionPlacementRunCreateSerializer,
    SectionPlacementRunSerializer,
    SectionBudgetApprovalRequestSerializer,
    SectionBudgetApprovalSerializer,
    SectionBudgetRunCreateSerializer,
    SectionBudgetRunSerializer,
    StaffingPlanApprovalSerializer,
    StaffingPlanRunCreateSerializer,
    StaffingPlanRunSerializer,
    StaffingRequestResolutionSerializer,
    PlanningRequestResolutionSerializer,
    TeacherPlanningCapacitySerializer,
    TeacherPlanningAnnualCapacitySerializer,
    TeacherCourseAssignmentRuleSerializer,
    TeacherTimePreferenceSerializer,
    TeacherAssignmentRunCreateSerializer,
    TeacherAssignmentRunSerializer,
    TeacherAssignmentApprovalRequestSerializer,
    TeacherAssignmentApprovalSerializer,
    TeacherPlanningRosterMembersSerializer,
    TeacherPlanningRosterSerializer,
    TimeSlotSerializer,
    AnnualPlacementLockSerializer,
)
from backend.apps.scheduling.services.engine_adapter import get_section_count_recommendations
from backend.apps.scheduling.services.planning_configuration import apply_course_capacity_policy


class SectionPlanningApprovalConflict(APIException):
    """HTTP 409 used when approval would overwrite existing decisions."""

    status_code = status.HTTP_409_CONFLICT
    default_code = SECTION_PLANNING_APPROVAL_CONFLICT


class SectionPlacementConflict(APIException):
    """HTTP 409 for stale placement input or an already-approved run."""

    status_code = status.HTTP_409_CONFLICT
    default_code = SECTION_PLACEMENT_CONFLICT


class TeacherAssignmentConflict(APIException):
    """HTTP 409 for stale named-teacher candidates or concurrent approval."""

    status_code = status.HTTP_409_CONFLICT
    default_code = TEACHER_ASSIGNMENT_CONFLICT


class TimeSlotViewSet(ReferenceDataViewSet):
    """CRUD recurring A-D slots using the shared reference-data policy."""

    queryset = TimeSlot.objects.select_related("academic_year")
    serializer_class = TimeSlotSerializer
    filter_fields = ("academic_year", "semester", "block", "is_available")


class AnnualPlacementLockViewSet(PolicyFilteredModelViewSet):
    """Manage pre-section annual locks through the transactional lock service."""

    queryset = AnnualPlacementLock.objects.select_related(
        "academic_year", "delivery_group", "locked_timeslot", "materialized_section",
    )
    serializer_class = AnnualPlacementLockSerializer
    resource_policy_class = PlanningConfigurationPolicy
    filter_fields = ("academic_year", "delivery_group")

    def perform_create(self, serializer):
        from backend.apps.scheduling.services.annual_placement_locks import create_annual_placement_lock

        try:
            lock = create_annual_placement_lock(actor=self.request.user, **serializer.validated_data)
        except DomainValidationError as error:
            raise ValidationError(error.detail) from error
        except DomainConflictError as error:
            raise SectionPlanningApprovalConflict(error.detail) from error
        serializer.instance = lock

    def perform_update(self, serializer):
        from backend.apps.scheduling.services.annual_placement_locks import update_annual_placement_lock

        try:
            if "reason" not in serializer.validated_data:
                raise DomainValidationError({"reason": "A reason is required when changing an annual placement lock."})
            lock = update_annual_placement_lock(
                serializer.instance,
                actor=self.request.user,
                locked_timeslot=serializer.validated_data.get("locked_timeslot", serializer.instance.locked_timeslot),
                reason=serializer.validated_data.get("reason", serializer.instance.reason),
            )
        except DomainValidationError as error:
            raise ValidationError(error.detail) from error
        except DomainConflictError as error:
            raise SectionPlanningApprovalConflict(error.detail) from error
        serializer.instance = lock

    def perform_destroy(self, instance):
        from backend.apps.scheduling.services.annual_placement_locks import delete_annual_placement_lock

        try:
            delete_annual_placement_lock(instance, reason=self.request.data.get("reason"))
        except DomainValidationError as error:
            raise ValidationError(error.detail) from error
        except DomainConflictError as error:
            raise SectionPlanningApprovalConflict(error.detail) from error


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


class PlanningConfigurationViewSet(PolicyFilteredModelViewSet):
    """Shared base applying role-safe policy filtering before query filters."""

    resource_policy_class = PlanningConfigurationPolicy


class CapacityProfileViewSet(PlanningConfigurationViewSet):
    """Manage reusable and course-specific class-size profiles."""

    queryset = CapacityProfile.objects.all()
    serializer_class = CapacityProfileSerializer
    filter_fields = ("scope",)

    def get_policy_queryset(self):
        # CapacityProfileSerializer exposes usage_count, so annotate once for
        # list/detail instead of counting attached courses per serialized row.
        return CapacityProfile.objects.annotate(course_usage_count=Count("courses"))

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

    def perform_create(self, serializer):
        item = serializer.save()
        from backend.apps.scheduling.services.staffing_configuration import invalidate_roster

        invalidate_roster(item.academic_year_id)

    def perform_update(self, serializer):
        previous_year_id = serializer.instance.academic_year_id
        item = serializer.save()
        from backend.apps.scheduling.services.staffing_configuration import invalidate_roster

        invalidate_roster(previous_year_id)
        invalidate_roster(item.academic_year_id)

    def perform_destroy(self, instance):
        academic_year_id = instance.academic_year_id
        instance.delete()
        from backend.apps.scheduling.services.staffing_configuration import invalidate_roster

        invalidate_roster(academic_year_id)


class TeacherPlanningAnnualCapacityViewSet(PlanningConfigurationViewSet):
    """Manage year-wide teacher load ceilings required by a ready roster."""

    queryset = TeacherPlanningAnnualCapacity.objects.select_related("teacher", "academic_year")
    serializer_class = TeacherPlanningAnnualCapacitySerializer
    filter_fields = ("teacher", "academic_year")

    def perform_create(self, serializer):
        from backend.apps.scheduling.services.teacher_assignment_configuration import save_annual_capacity

        save_annual_capacity(serializer, actor=self.request.user)

    def perform_update(self, serializer):
        previous_year_id = serializer.instance.academic_year_id
        from backend.apps.scheduling.services.teacher_assignment_configuration import save_annual_capacity
        from backend.apps.scheduling.services.staffing_configuration import invalidate_roster

        item = save_annual_capacity(serializer, actor=self.request.user)
        invalidate_roster(previous_year_id)
        return item

    def perform_destroy(self, instance):
        from backend.apps.scheduling.services.teacher_assignment_configuration import delete_annual_capacity

        delete_annual_capacity(instance)


class TeacherCourseAssignmentRuleViewSet(PlanningConfigurationViewSet):
    """Manage exact/min/max annual hard course assignments per teacher."""

    queryset = TeacherCourseAssignmentRule.objects.select_related("academic_year", "teacher", "course")
    serializer_class = TeacherCourseAssignmentRuleSerializer
    filter_fields = ("academic_year", "teacher", "course")

    def perform_create(self, serializer):
        from backend.apps.scheduling.services.teacher_assignment_configuration import save_course_rule

        save_course_rule(serializer, actor=self.request.user)

    def perform_update(self, serializer):
        from backend.apps.scheduling.services.teacher_assignment_configuration import save_course_rule

        save_course_rule(serializer, actor=self.request.user)


class TeacherTimePreferenceViewSet(PlanningConfigurationViewSet):
    """Manage soft preferred/avoid recurring slots independently of availability."""

    queryset = TeacherTimePreference.objects.select_related("academic_year", "teacher", "timeslot")
    serializer_class = TeacherTimePreferenceSerializer
    filter_fields = ("academic_year", "teacher", "timeslot", "preference")

    def perform_create(self, serializer):
        from backend.apps.scheduling.services.teacher_assignment_configuration import save_time_preference

        save_time_preference(serializer, actor=self.request.user)

    def perform_update(self, serializer):
        from backend.apps.scheduling.services.teacher_assignment_configuration import save_time_preference

        save_time_preference(serializer, actor=self.request.user)


class TeacherPlanningRosterViewSet(PlanningConfigurationViewSet):
    """Build a year roster and explicitly confirm complete capacity evidence."""

    queryset = TeacherPlanningRoster.objects.select_related(
        "academic_year", "confirmed_by"
    ).prefetch_related("members")
    serializer_class = TeacherPlanningRosterSerializer
    filter_fields = ("academic_year", "status")

    def perform_update(self, serializer):
        from backend.apps.common.constants import TEACHER_ROSTER_STATUS_DRAFT

        # Moving/configuring the roster is a new unconfirmed staffing input.
        serializer.save(
            status=TEACHER_ROSTER_STATUS_DRAFT,
            confirmed_by=None,
            confirmed_at=None,
        )

    def perform_destroy(self, instance):
        if instance.staffing_runs.exists():
            raise ValidationError({
                "detail": "A roster used by an immutable staffing run cannot be deleted."
            })
        instance.delete()

    @action(detail=True, methods=["post"], url_path="set-members")
    def set_members(self, request, pk=None):
        from backend.apps.scheduling.services.staffing_configuration import (
            StaffingConfigurationError,
            set_roster_members,
        )

        serializer = TeacherPlanningRosterMembersSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            roster = set_roster_members(
                self.get_object(),
                teacher_ids=serializer.validated_data["teacher_ids"],
                actor=request.user,
            )
        except StaffingConfigurationError as error:
            raise ValidationError(error.detail) from error
        return Response(TeacherPlanningRosterSerializer(roster).data)

    @action(detail=True, methods=["post"])
    def confirm(self, request, pk=None):
        from backend.apps.scheduling.services.staffing_configuration import (
            StaffingConfigurationError,
            confirm_roster_ready,
        )

        try:
            roster = confirm_roster_ready(self.get_object(), actor=request.user)
        except StaffingConfigurationError as error:
            raise ValidationError(error.detail) from error
        return Response(TeacherPlanningRosterSerializer(roster).data)


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


class SectionPlacementRunViewSet(
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    """Review-first semester/A-D placement; approval alone writes schedules."""

    permission_classes = [ActionPolicyPermission]
    action_policy_class = SchedulingActionPolicy
    action_name = SchedulingAction.VIEW_SCHEDULING_RUN_STATUS
    queryset = SectionPlacementRun.objects.select_related(
        "academic_year", "budget_approval", "conflict_matrix", "teacher_roster", "created_by",
        "approval__approved_by",
    ).prefetch_related("approval__assignments__section", "approval__assignments__timeslot")

    def get_permissions(self):
        if self.action in {"create"}:
            self.action_name = SchedulingAction.RUN_SECTION_PLACEMENT
        elif self.action in {"approve"}:
            self.action_name = SchedulingAction.APPROVE_SECTION_PLACEMENT
        else:
            # Staff may read a candidate and preview approval readiness, but
            # cannot launch or accept a scheduling-changing run.
            self.action_name = SchedulingAction.VIEW_SCHEDULING_RUN_STATUS
        return super().get_permissions()

    def get_serializer_class(self):
        return SectionPlacementRunCreateSerializer if self.action == "create" else SectionPlacementRunSerializer

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if not AcademicYear.objects.filter(pk=serializer.validated_data["academic_year"]).exists():
            raise NotFound("Academic year not found.")
        try:
            from backend.apps.scheduling.services.section_placement import create_section_placement_run

            run = create_section_placement_run(created_by=request.user, **serializer.validated_data)
        except (ValueError, DomainValidationError) as error:
            detail = error.detail if isinstance(error, DomainValidationError) else {"detail": str(error)}
            raise ValidationError(detail) from error
        return Response(SectionPlacementRunSerializer(run).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["get"])
    def review(self, request, pk=None):
        from backend.apps.scheduling.services.section_placement import (
            SectionPlacementConflictError, SectionPlacementValidationError,
            preview_section_placement_approval,
        )

        try:
            preview = preview_section_placement_approval(self.get_object())
        except SectionPlacementValidationError as error:
            raise ValidationError(error.detail) from error
        except SectionPlacementConflictError as error:
            raise SectionPlacementConflict(error.detail) from error
        return Response(preview)

    @action(detail=True, methods=["post"], url_path="approval-preview")
    def approval_preview(self, request, pk=None):
        # No selection JSON is accepted: adding/changing a lock is the explicit,
        # auditable way to obtain a different candidate before making a new run.
        serializer = SectionPlacementApprovalRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return self.review(request, pk=pk)

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        from backend.apps.scheduling.services.section_placement import (
            SectionPlacementConflictError, SectionPlacementValidationError,
            approve_section_placement_run,
        )

        serializer = SectionPlacementApprovalRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            approval = approve_section_placement_run(
                self.get_object(), approved_by=request.user,
                reason=serializer.validated_data["reason"],
            )
        except SectionPlacementValidationError as error:
            raise ValidationError(error.detail) from error
        except SectionPlacementConflictError as error:
            raise SectionPlacementConflict(error.detail) from error
        approval = SectionPlacementApproval.objects.prefetch_related("assignments").get(pk=approval.pk)
        return Response(SectionPlacementApprovalSerializer(approval).data, status=status.HTTP_201_CREATED)


class TeacherAssignmentRunViewSet(
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    """Review-first named teacher assignment over accepted section timing."""

    permission_classes = [ActionPolicyPermission]
    action_policy_class = SchedulingActionPolicy
    action_name = SchedulingAction.VIEW_SCHEDULING_RUN_STATUS
    queryset = TeacherAssignmentRun.objects.select_related(
        "academic_year", "teacher_roster", "created_by", "approval__approved_by",
    ).prefetch_related("approval__assignments__section", "approval__assignments__teacher")

    def get_permissions(self):
        if self.action == "create":
            self.action_name = SchedulingAction.RUN_TEACHER_ASSIGNMENT
        elif self.action == "approve":
            self.action_name = SchedulingAction.APPROVE_TEACHER_ASSIGNMENT
        else:
            self.action_name = SchedulingAction.VIEW_SCHEDULING_RUN_STATUS
        return super().get_permissions()

    def get_serializer_class(self):
        return TeacherAssignmentRunCreateSerializer if self.action == "create" else TeacherAssignmentRunSerializer

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if not AcademicYear.objects.filter(pk=serializer.validated_data["academic_year"]).exists():
            raise NotFound("Academic year not found.")
        from backend.apps.scheduling.services.teacher_assignment import create_teacher_assignment_run

        try:
            run = create_teacher_assignment_run(created_by=request.user, **serializer.validated_data)
        except (ValueError, DomainValidationError) as error:
            detail = error.detail if isinstance(error, DomainValidationError) else {"detail": str(error)}
            raise ValidationError(detail) from error
        return Response(TeacherAssignmentRunSerializer(run).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["get"])
    def review(self, request, pk=None):
        """Show the named candidate and current stale-state readiness."""

        from backend.apps.scheduling.services.teacher_assignment import (
            TeacherAssignmentConflictError, TeacherAssignmentValidationError,
            preview_teacher_assignment_approval,
        )

        try:
            preview = preview_teacher_assignment_approval(self.get_object())
        except TeacherAssignmentValidationError as error:
            raise ValidationError(error.detail) from error
        except TeacherAssignmentConflictError as error:
            raise TeacherAssignmentConflict(error.detail) from error
        return Response(preview)

    @action(detail=True, methods=["post"], url_path="approval-preview")
    def approval_preview(self, request, pk=None):
        serializer = TeacherAssignmentApprovalRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return self.review(request, pk=pk)

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        """Atomically accept the reviewed candidate; no direct assignment edits exist."""

        from backend.apps.scheduling.services.teacher_assignment import (
            TeacherAssignmentConflictError, TeacherAssignmentValidationError,
            approve_teacher_assignment_run,
        )

        serializer = TeacherAssignmentApprovalRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            approval = approve_teacher_assignment_run(
                self.get_object(), approved_by=request.user,
                reason=serializer.validated_data["reason"],
            )
        except TeacherAssignmentValidationError as error:
            raise ValidationError(error.detail) from error
        except TeacherAssignmentConflictError as error:
            raise TeacherAssignmentConflict(error.detail) from error
        approval = TeacherAssignmentApproval.objects.prefetch_related("assignments").get(pk=approval.pk)
        return Response(TeacherAssignmentApprovalSerializer(approval).data, status=status.HTTP_201_CREATED)


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


class SectionBudgetRunViewSet(
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    """Teacher-independent physical-section budget runs and approvals."""

    permission_classes = [ActionPolicyPermission]
    action_policy_class = DemandPlanningActionPolicy
    action_name = DemandPlanningAction.RUN_SECTION_PLANNING
    queryset = SectionBudgetRun.objects.select_related(
        "academic_year", "created_by", "approval__approved_by"
    ).prefetch_related(
        "approval__offering_approvals",
        "approval__request_resolutions",
    )

    def get_permissions(self):
        if self.action in {"approval_preview", "approve", "affected_students"}:
            self.action_name = DemandPlanningAction.APPROVE_SECTION_PLAN
        else:
            self.action_name = DemandPlanningAction.RUN_SECTION_PLANNING
        return super().get_permissions()

    def get_serializer_class(self):
        return (
            SectionBudgetRunCreateSerializer
            if self.action == "create"
            else SectionBudgetRunSerializer
        )

    def create(self, request, *args, **kwargs):
        from backend.apps.scheduling.services.section_budget_planning import (
            create_section_budget_run,
        )

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            academic_year = AcademicYear.objects.get(
                pk=serializer.validated_data["academic_year"]
            )
        except AcademicYear.DoesNotExist as error:
            raise NotFound("Academic year not found.") from error
        try:
            run = create_section_budget_run(
                academic_year=academic_year,
                created_by=request.user,
                budget_type=serializer.validated_data["budget_type"],
                section_budget=serializer.validated_data["section_budget"],
                backup_policy=serializer.validated_data["backup_policy"],
                backup_overrides=serializer.validated_data["backup_overrides"],
                offering_constraints=serializer.validated_data["offering_constraints"],
            )
        except ValueError as error:
            raise ValidationError({"detail": str(error)}) from error
        return Response(SectionBudgetRunSerializer(run).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="approval-preview")
    def approval_preview(self, request, pk=None):
        from backend.apps.scheduling.services.section_budget_planning import (
            preview_section_budget_approval,
        )
        from backend.apps.scheduling.services.section_planning import (
            PlanningApprovalValidationError,
        )

        serializer = SectionBudgetApprovalRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            preview = preview_section_budget_approval(
                self.get_object(),
                selections=serializer.validated_data.get("offerings"),
            )
        except PlanningApprovalValidationError as error:
            raise ValidationError(error.detail) from error
        return Response(preview)

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        from backend.apps.scheduling.services.section_budget_planning import (
            approve_section_budget_run,
        )
        from backend.apps.scheduling.services.section_planning import (
            PlanningApprovalConflictError,
            PlanningApprovalValidationError,
        )

        serializer = SectionBudgetApprovalRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            approval = approve_section_budget_run(
                self.get_object(),
                approved_by=request.user,
                reason=serializer.validated_data["reason"],
                selections=serializer.validated_data.get("offerings"),
            )
        except PlanningApprovalValidationError as error:
            raise ValidationError(error.detail) from error
        except PlanningApprovalConflictError as error:
            raise SectionPlanningApprovalConflict(error.detail) from error
        approval = SectionBudgetApproval.objects.prefetch_related(
            "offering_approvals", "request_resolutions"
        ).get(pk=approval.pk)
        return Response(
            SectionBudgetApprovalSerializer(approval).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["get"], url_path="affected-students")
    def affected_students(self, request, pk=None):
        run = self.get_object()
        if hasattr(run, "approval"):
            return Response(
                PlanningRequestResolutionSerializer(
                    run.approval.request_resolutions.all(),
                    many=True,
                ).data
            )
        return Response(run.result.get("request_resolutions", []))


class StaffingPlanRunViewSet(
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    viewsets.GenericViewSet,
):
    """Qualified-roster planning and final physical-section approval."""

    permission_classes = [ActionPolicyPermission]
    action_policy_class = DemandPlanningActionPolicy
    action_name = DemandPlanningAction.RUN_SECTION_PLANNING
    queryset = StaffingPlanRun.objects.select_related(
        "academic_year", "budget_approval", "teacher_roster", "created_by",
        "approval__approved_by"
    ).prefetch_related(
        "request_resolutions",
        "approval__offering_approvals__generated_sections",
    )

    def get_permissions(self):
        if self.action in {"approval_preview", "approve", "affected_students"}:
            self.action_name = DemandPlanningAction.APPROVE_SECTION_PLAN
        else:
            self.action_name = DemandPlanningAction.RUN_SECTION_PLANNING
        return super().get_permissions()

    def get_serializer_class(self):
        return (
            StaffingPlanRunCreateSerializer
            if self.action == "create"
            else StaffingPlanRunSerializer
        )

    def create(self, request, *args, **kwargs):
        from backend.apps.scheduling.services.staffing_planning import (
            create_staffing_plan_run,
        )

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            academic_year = AcademicYear.objects.get(
                pk=serializer.validated_data["academic_year"]
            )
        except AcademicYear.DoesNotExist as error:
            raise NotFound("Academic year not found.") from error
        try:
            run = create_staffing_plan_run(
                academic_year=academic_year,
                created_by=request.user,
                budget_approval=serializer.validated_data.get("budget_approval"),
                backup_policy=serializer.validated_data["backup_policy"],
                backup_overrides=serializer.validated_data["backup_overrides"],
                offering_constraints=serializer.validated_data["offering_constraints"],
                teacher_capacity_adjustments=serializer.validated_data[
                    "teacher_capacity_adjustments"
                ],
            )
        except ValueError as error:
            raise ValidationError({"detail": str(error)}) from error
        return Response(StaffingPlanRunSerializer(run).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="approval-preview")
    def approval_preview(self, request, pk=None):
        from backend.apps.scheduling.services.staffing_planning import (
            preview_staffing_plan_approval,
        )
        from backend.apps.scheduling.services.section_planning import (
            PlanningApprovalValidationError,
        )

        serializer = SectionBudgetApprovalRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            preview = preview_staffing_plan_approval(
                self.get_object(),
                selections=serializer.validated_data.get("offerings"),
            )
        except PlanningApprovalValidationError as error:
            raise ValidationError(error.detail) from error
        return Response(preview)

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        from backend.apps.scheduling.services.staffing_planning import (
            approve_staffing_plan_run,
        )
        from backend.apps.scheduling.services.section_planning import (
            PlanningApprovalConflictError,
            PlanningApprovalValidationError,
        )

        serializer = SectionBudgetApprovalRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            approval = approve_staffing_plan_run(
                self.get_object(),
                approved_by=request.user,
                reason=serializer.validated_data["reason"],
                selections=serializer.validated_data.get("offerings"),
            )
        except PlanningApprovalValidationError as error:
            raise ValidationError(error.detail) from error
        except PlanningApprovalConflictError as error:
            raise SectionPlanningApprovalConflict(error.detail) from error
        approval = StaffingPlanApproval.objects.prefetch_related(
            "offering_approvals__generated_sections"
        ).get(pk=approval.pk)
        return Response(
            StaffingPlanApprovalSerializer(approval).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["get"], url_path="affected-students")
    def affected_students(self, request, pk=None):
        return Response(StaffingRequestResolutionSerializer(
            self.get_object().request_resolutions.all(),
            many=True,
        ).data)
