"""Shared DRF viewset bases for policy-scoped resources."""

from rest_framework import viewsets

from backend.apps.access.permissions import ResourcePolicyPermission


class PolicyFilteredModelViewSet(viewsets.ModelViewSet):
    """Apply resource-policy scoping before whitelisted query filters.

    This is deliberately small. It standardizes the one repeated contract every
    resource endpoint needs without hiding resource-specific behavior such as
    guarded deletion, nested ownership, or workflow actions.
    """

    permission_classes = [ResourcePolicyPermission]
    filter_fields = ()

    def get_policy_queryset(self):
        """Return the unfiltered base queryset for this request.

        Subclasses can override this instead of replacing ``get_queryset`` when
        they need specialized prefetches or annotations. The authorization and
        query-parameter ordering remains centralized here.
        """

        return self.queryset.all()

    def get_queryset(self):
        if not getattr(self, "resource_policy_class", None):
            # Permission classes fail closed too, but keeping the queryset empty
            # makes accidental use inside list/detail code safe and obvious.
            return self.get_policy_queryset().none()

        queryset = self.resource_policy_class.filter_read_queryset(
            self.request.user,
            self.get_policy_queryset(),
        )
        for field in self.filter_fields:
            value = self.request.query_params.get(field)
            if value is not None:
                queryset = queryset.filter(**{field: value})
        return queryset

