"""Qualification review workflow shared by HTTP and future orchestration code."""

from django.db import transaction
from django.utils import timezone

from backend.apps.common.exceptions import DomainValidationError
from backend.apps.common.constants import (
    QUALIFICATION_REVIEW_REJECTED,
    QUALIFICATION_REVIEW_VERIFIED,
)
from backend.apps.constraints.models import TeacherQualification
from backend.apps.scheduling.services.staffing_configuration import (
    invalidate_teacher_rosters,
)


@transaction.atomic
def review_teacher_qualification(item, *, actor, review_status, reason=""):
    """Verify or reject one teacher qualification and invalidate ready rosters."""

    if review_status not in {
        QUALIFICATION_REVIEW_VERIFIED,
        QUALIFICATION_REVIEW_REJECTED,
    }:
        raise DomainValidationError({"review_status": "Unknown qualification review status."})
    reason = reason.strip() if isinstance(reason, str) else ""
    if review_status == QUALIFICATION_REVIEW_REJECTED and not reason:
        raise DomainValidationError({"reason": "A rejection reason is required."})

    item = TeacherQualification.objects.select_for_update().get(pk=item.pk)
    item.review_status = review_status
    item.reviewed_by = actor
    item.reviewed_at = timezone.now()
    item.review_reason = reason
    item.save(update_fields=[
        "review_status",
        "reviewed_by",
        "reviewed_at",
        "review_reason",
    ])
    invalidate_teacher_rosters(item.teacher_id)
    return item
