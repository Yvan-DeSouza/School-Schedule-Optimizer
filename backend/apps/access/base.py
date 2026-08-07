from backend.apps.access.action_policies.base import BaseActionPolicy
from backend.apps.access.resource_policies.base import BaseResourcePolicy


BaseAccessPolicy = BaseResourcePolicy

__all__ = ["BaseAccessPolicy", "BaseActionPolicy", "BaseResourcePolicy"]
