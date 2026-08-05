from dataclasses import dataclass

from backend.apps.access.scopes import ReadScope, WriteScope


@dataclass(frozen=True)
class AccessRule:
    read: str = ReadScope.NONE
    write: str = WriteScope.NONE

    def __post_init__(self):
        if not isinstance(self.read, str) or self.read not in ReadScope.VALUES:
            raise ValueError(f"Invalid read scope: {self.read}")
        if not isinstance(self.write, str) or self.write not in WriteScope.VALUES:
            raise ValueError(f"Invalid write scope: {self.write}")
