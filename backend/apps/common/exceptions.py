"""Framework-independent domain exceptions.

Services raise these exceptions so they can be reused by DRF views, background
jobs, management commands, or future orchestration code without importing
``rest_framework``. HTTP layers translate ``detail`` into the appropriate API
exception type.
"""


class DomainError(Exception):
    """Base class carrying structured, API-ready error detail."""

    def __init__(self, detail):
        self.detail = detail
        super().__init__(str(detail))


class DomainValidationError(DomainError):
    """The requested operation is malformed or violates domain validation."""


class DomainConflictError(DomainError):
    """The request is valid but conflicts with current system state."""

