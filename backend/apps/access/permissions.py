from rest_framework.permissions import SAFE_METHODS, BasePermission


class ResourcePolicyPermission(BasePermission):
    def has_permission(self, request, view):
        policy = self._get_policy(view)
        if policy is None:
            return False

        if request.method in SAFE_METHODS:
            return policy.rule_for(request.user).read != "none"
        if request.method == "POST":
            return policy.can_create(request.user, getattr(request, "data", None))
        return policy.rule_for(request.user).write != "none"

    def has_object_permission(self, request, view, obj):
        policy = self._get_policy(view)
        if policy is None:
            return False

        if request.method in SAFE_METHODS:
            return policy.can_read_object(request.user, obj)
        if request.method == "DELETE":
            return policy.can_delete_object(request.user, obj)
        return policy.can_write_object(request.user, obj)

    def _get_policy(self, view):
        return getattr(view, "resource_policy_class", None) or getattr(
            view,
            "policy_class",
            None,
        )


class ActionPolicyPermission(BasePermission):
    def has_permission(self, request, view):
        policy = getattr(view, "action_policy_class", None)
        action = getattr(view, "action_name", None)
        if policy is None or action is None:
            return False

        return policy.can_execute(request.user, action=action, context=view)

    def has_object_permission(self, request, view, obj):
        return self.has_permission(request, view)


PolicyPermission = ResourcePolicyPermission
