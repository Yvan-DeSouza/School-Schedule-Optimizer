"""Compatibility imports for the earlier access-policy module layout."""

from backend.apps.access.action_policies.base import BaseActionPolicy
from backend.apps.access.resource_policies.base import BaseResourcePolicy


# Historical name retained so existing imports resolve to the current resource
# policy implementation rather than maintaining a second authorization system.
BaseAccessPolicy = BaseResourcePolicy

__all__ = ["BaseAccessPolicy", "BaseActionPolicy", "BaseResourcePolicy"]
