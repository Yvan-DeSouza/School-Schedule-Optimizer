"""Canonical scope strings used by access rules and policies."""


class ReadScope:
    """How much of a resource collection a role may read."""

    NONE = "none"
    OWN = "own"
    ASSIGNED = "assigned"
    ALL = "all"

    VALUES = {NONE, OWN, ASSIGNED, ALL}


class WriteScope:
    """Which resource instances a role may mutate."""

    NONE = "none"
    OWN = "own"
    ASSIGNED = "assigned"
    ALL = "all"

    VALUES = {NONE, OWN, ASSIGNED, ALL}


class ActionScope:
    """Whether a role reaches named-action-specific policy checks."""

    DENIED = "denied"
    ALLOWED = "allowed"

    VALUES = {DENIED, ALLOWED}
