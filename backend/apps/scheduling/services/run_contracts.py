"""Small reusable contracts for immutable planning-run workflows."""

from backend.apps.common.exceptions import DomainValidationError


def require_text_reason(reason, *, message, error_class=DomainValidationError):
    """Normalize a human audit reason and reject missing text."""

    if not isinstance(reason, str) or not reason.strip():
        raise error_class({"reason": message})
    return reason.strip()


def ensure_unique_selection(items, key, *, field, message, error_class=DomainValidationError):
    """Reject duplicate selected IDs before preview/approval loops run."""

    seen = set()
    duplicates = set()
    for item in items:
        value = item[key]
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    if duplicates:
        raise error_class({field: message})
