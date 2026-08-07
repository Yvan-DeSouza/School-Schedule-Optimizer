class ReadScope:
    NONE = "none"
    OWN = "own"
    ASSIGNED = "assigned"
    ALL = "all"

    VALUES = {NONE, OWN, ASSIGNED, ALL}


class WriteScope:
    NONE = "none"
    OWN = "own"
    ASSIGNED = "assigned"
    ALL = "all"

    VALUES = {NONE, OWN, ASSIGNED, ALL}


class ActionScope:
    DENIED = "denied"
    ALLOWED = "allowed"

    VALUES = {DENIED, ALLOWED}
