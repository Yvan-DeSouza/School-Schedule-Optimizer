"""DRF permission adapters for resource and named-action policy classes."""

from rest_framework.permissions import SAFE_METHODS, BasePermission


class ResourcePolicyPermission(BasePermission):
    """Translate HTTP method/object checks into a BaseResourcePolicy."""

    def has_permission(self, request, view):
        policy = self._get_policy(view)
        if policy is None:
            # Missing policy declarations are configuration errors and fail closed.
            return False

        if request.method in SAFE_METHODS:
            return policy.rule_for(request.user).read != "none"
        if request.method == "POST":
            # Create has no object yet; nested views may supply safe URL context.
            context_getter = getattr(view, "get_policy_context", None)
            context = context_getter() if context_getter else None
            return policy.can_create(request.user, getattr(request, "data", None), context=context)
        return policy.rule_for(request.user).write != "none"

    def has_object_permission(self, request, view, obj):
        # DRF calls this after has_permission for retrieve/update/delete.
        policy = self._get_policy(view)
        if policy is None:
            return False

        if request.method in SAFE_METHODS:
            return policy.can_read_object(request.user, obj)
        if request.method == "DELETE":
            return policy.can_delete_object(request.user, obj)
        return policy.can_write_object(request.user, obj)

    def _get_policy(self, view):
        # resource_policy_class is the canonical name; policy_class is retained
        # for compatibility with earlier views/tests.
        return getattr(view, "resource_policy_class", None) or getattr(
            view,
            "policy_class",
            None,
        )


class ActionPolicyPermission(BasePermission):
    """Authorize non-CRUD operations by stable semantic action name."""

    def has_permission(self, request, view):
        policy = getattr(view, "action_policy_class", None)
        action = getattr(view, "action_name", None)
        if policy is None or action is None:
            # A view must opt into both a policy and an explicit action.
            return False

        return policy.can_execute(request.user, action=action, context=view)

    def has_object_permission(self, request, view, obj):
        # Current action policies are role/action based rather than object scoped.
        return self.has_permission(request, view)


# Backward-compatible import name used by older modules/tests.
PolicyPermission = ResourcePolicyPermission
