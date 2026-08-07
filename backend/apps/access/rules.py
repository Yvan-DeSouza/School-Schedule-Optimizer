from dataclasses import dataclass

from backend.apps.access.scopes import ActionScope, ReadScope, WriteScope


@dataclass(frozen=True)
class AccessRule:
    read: str = ReadScope.NONE
    write: str = WriteScope.NONE

    def __post_init__(self):
        if not isinstance(self.read, str) or self.read not in ReadScope.VALUES:
            raise ValueError(f"Invalid read scope: {self.read}")
        if not isinstance(self.write, str) or self.write not in WriteScope.VALUES:
            raise ValueError(f"Invalid write scope: {self.write}")


@dataclass(frozen=True)
class ActionRule:
    execute: str = ActionScope.DENIED

    def __post_init__(self):
        if not isinstance(self.execute, str) or self.execute not in ActionScope.VALUES:
            raise ValueError(f"Invalid action scope: {self.execute}")
