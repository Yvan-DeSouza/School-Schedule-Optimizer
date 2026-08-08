"""Immutable validated policy-rule values."""

from dataclasses import dataclass

from backend.apps.access.scopes import ActionScope, ReadScope, WriteScope


@dataclass(frozen=True)
class AccessRule:
    """Independent read and write scopes for a resource/role pair."""

    read: str = ReadScope.NONE
    write: str = WriteScope.NONE

    def __post_init__(self):
        # Reject lists/unknown strings early; a malformed rule must never become
        # accidental broad access.
        if not isinstance(self.read, str) or self.read not in ReadScope.VALUES:
            raise ValueError(f"Invalid read scope: {self.read}")
        if not isinstance(self.write, str) or self.write not in WriteScope.VALUES:
            raise ValueError(f"Invalid write scope: {self.write}")


@dataclass(frozen=True)
class ActionRule:
    """Binary execute permission before action-specific policy logic."""

    execute: str = ActionScope.DENIED

    def __post_init__(self):
        if not isinstance(self.execute, str) or self.execute not in ActionScope.VALUES:
            raise ValueError(f"Invalid action scope: {self.execute}")
