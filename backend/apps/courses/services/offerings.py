"""Year-specific course offering, cancellation, and combination workflows."""

from __future__ import annotations

from math import ceil

from django.db import transaction
from django.db.models import Q

from backend.apps.common.exceptions import DomainConflictError, DomainValidationError
from backend.apps.common.constants import (
    COURSE_ALLOWED_SEMESTER_1_ONLY,
    COURSE_ALLOWED_SEMESTER_2_ONLY,
    COURSE_ALLOWED_SEMESTER_EITHER,
    COURSE_OFFERING_ACTION_CANCELLED,
    COURSE_OFFERING_ACTION_COMBINED,
    COURSE_OFFERING_ACTION_RESTORED,
    COURSE_OFFERING_ACTION_SEPARATED,
    COURSE_OFFERING_STATUS_CANCELLED,
    COURSE_OFFERING_STATUS_OFFERED,
    DELIVERY_GROUP_STATUS_ACTIVE,
    DELIVERY_GROUP_STATUS_RETIRED,
)
from backend.apps.courses.models import (
    Course,
    CourseCombinationRule,
    CourseOffering,
    CourseOfferingDecision,
    DeliveryGroup,
    Section,
)
from backend.apps.courses.selectors import active_sections_queryset


class OfferingValidationError(DomainValidationError):
    """The requested offering decision is malformed or pedagogically illegal."""


class OfferingConflictError(DomainConflictError):
    """The decision would overwrite an active operational section state."""


def _clean_reason(reason):
    if not isinstance(reason, str) or not reason.strip():
        raise OfferingValidationError({"reason": "A decision reason is required."})
    return reason.strip()


def _standalone_group(course, academic_year, *, actor=None, reason=""):
    return DeliveryGroup.objects.create(
        academic_year=academic_year,
        name=course.course_code,
        capacity_profile=course.capacity_profile,
        created_by=actor,
        reason=reason,
    )


@transaction.atomic
def ensure_academic_year_offerings(academic_year, *, actor=None):
    """Create the default one-course delivery group for missing catalog rows."""

    existing_course_ids = set(
        CourseOffering.objects.filter(academic_year=academic_year).values_list(
            "course_id",
            flat=True,
        )
    )
    created = []
    for course in Course.objects.select_related("capacity_profile").exclude(
        id__in=existing_course_ids
    ).order_by("course_code", "id"):
        group = _standalone_group(course, academic_year, actor=actor)
        created.append(
            CourseOffering.objects.create(
                course=course,
                academic_year=academic_year,
                delivery_group=group,
            )
        )
    return created


def _active_section_conflict(groups):
    groups = list(groups)
    member_course_ids = CourseOffering.objects.filter(
        delivery_group__in=groups
    ).values_list("course_id", flat=True)
    sections = list(
        active_sections_queryset(
            Section.objects.filter(
                Q(delivery_group__in=groups) | Q(
                    delivery_group__isnull=True,
                    course_id__in=member_course_ids,
                )
            )
        ).values("id", "section_number", "delivery_group_id")
    )
    if sections:
        raise OfferingConflictError({
            "detail": "Active sections must be retired before changing their course offering.",
            "conflicts": [{
                "code": "active_sections_block_offering_change",
                "sections": sections,
            }],
        })


def combined_allowed_semester(courses):
    """Return the legal intersection of member-course semester restrictions."""

    values = {course.allowed_semester for course in courses}
    if (
        COURSE_ALLOWED_SEMESTER_1_ONLY in values
        and COURSE_ALLOWED_SEMESTER_2_ONLY in values
    ):
        return None
    if COURSE_ALLOWED_SEMESTER_1_ONLY in values:
        return COURSE_ALLOWED_SEMESTER_1_ONLY
    if COURSE_ALLOWED_SEMESTER_2_ONLY in values:
        return COURSE_ALLOWED_SEMESTER_2_ONLY
    return COURSE_ALLOWED_SEMESTER_EITHER


def _predicted_primary_demand(academic_year):
    """Use the same historical conversion forecast as section planning."""

    # Keep engine-specific forecasting behind the scheduling application layer.
    # This courses service owns offering decisions, but it should not learn the
    # shape of pure-engine DTOs or demand-analysis result objects.
    from backend.apps.scheduling.services.demand_forecasting import (
        predicted_primary_demand_by_course,
    )

    return predicted_primary_demand_by_course(academic_year)


@transaction.atomic
def cancel_course_offering(offering, *, actor, reason):
    """Cancel a course explicitly while preserving requests and catalog identity."""

    reason = _clean_reason(reason)
    offering = CourseOffering.objects.select_for_update(of=("self",)).select_related(
        "delivery_group"
    ).get(pk=offering.pk)
    if offering.status == COURSE_OFFERING_STATUS_CANCELLED:
        raise OfferingConflictError({
            "detail": "This course offering is already cancelled.",
            "conflicts": [{"code": "offering_already_cancelled"}],
        })
    old_group = offering.delivery_group
    _active_section_conflict([old_group] if old_group else [])
    if old_group and old_group.offerings.exclude(pk=offering.pk).exists():
        raise OfferingConflictError({
            "detail": (
                "Separate the combined delivery group before cancelling one of "
                "its member courses."
            ),
            "conflicts": [{"code": "combined_offering_must_be_separated_first"}],
        })
    previous_status = offering.status
    offering.status = COURSE_OFFERING_STATUS_CANCELLED
    offering.delivery_group = None
    offering.decision_reason = reason
    offering.decided_by = actor
    offering.save(update_fields=[
        "status", "delivery_group", "decision_reason", "decided_by", "decided_at",
    ])
    if old_group and old_group.offerings.exclude(pk=offering.pk).count() == 0:
        old_group.status = DELIVERY_GROUP_STATUS_RETIRED
        old_group.save(update_fields=["status"])
    CourseOfferingDecision.objects.create(
        offering=offering,
        action=COURSE_OFFERING_ACTION_CANCELLED,
        previous_status=previous_status,
        new_status=offering.status,
        previous_delivery_group=old_group,
        new_delivery_group=None,
        decided_by=actor,
        reason=reason,
    )
    return offering


@transaction.atomic
def restore_course_offering(offering, *, actor, reason):
    """Restore a cancelled course into a new standalone delivery group."""

    reason = _clean_reason(reason)
    offering = CourseOffering.objects.select_for_update().select_related(
        "course__capacity_profile", "academic_year"
    ).get(pk=offering.pk)
    if offering.status != COURSE_OFFERING_STATUS_CANCELLED:
        raise OfferingConflictError({
            "detail": "Only a cancelled offering can be restored.",
            "conflicts": [{"code": "offering_not_cancelled"}],
        })
    group = _standalone_group(
        offering.course,
        offering.academic_year,
        actor=actor,
        reason=reason,
    )
    previous_status = offering.status
    offering.status = COURSE_OFFERING_STATUS_OFFERED
    offering.delivery_group = group
    offering.decision_reason = reason
    offering.decided_by = actor
    offering.save(update_fields=[
        "status", "delivery_group", "decision_reason", "decided_by", "decided_at",
    ])
    CourseOfferingDecision.objects.create(
        offering=offering,
        action=COURSE_OFFERING_ACTION_RESTORED,
        previous_status=previous_status,
        new_status=offering.status,
        previous_delivery_group=None,
        new_delivery_group=group,
        decided_by=actor,
        reason=reason,
    )
    return offering


def _rule_offerings(rule, academic_year):
    course_ids = list(rule.members.values_list("course_id", flat=True))
    if len(course_ids) < 2:
        raise OfferingValidationError({
            "detail": "A combination rule must contain at least two courses."
        })
    offerings = list(
        CourseOffering.objects.select_for_update(of=("self",))
        .select_related("course", "delivery_group", "academic_year")
        .filter(academic_year=academic_year, course_id__in=course_ids)
        .order_by("course__course_code", "id")
    )
    if len(offerings) != len(course_ids):
        raise OfferingValidationError({
            "detail": "Every combination-rule course needs an offering for this year."
        })
    return offerings


@transaction.atomic
def combine_course_offerings(rule, academic_year, *, actor, reason):
    """Replace standalone groups with one audited, one-section combined group."""

    reason = _clean_reason(reason)
    rule = CourseCombinationRule.objects.select_for_update().select_related(
        "capacity_profile"
    ).get(pk=rule.pk)
    if not rule.is_active:
        raise OfferingValidationError({"detail": "The combination rule is inactive."})
    offerings = _rule_offerings(rule, academic_year)
    if any(item.status != COURSE_OFFERING_STATUS_OFFERED for item in offerings):
        raise OfferingValidationError({
            "detail": "Cancelled courses must be restored before they can be combined."
        })
    old_groups = {item.delivery_group for item in offerings if item.delivery_group_id}
    if any(group.offerings.count() != 1 for group in old_groups):
        raise OfferingConflictError({
            "detail": "Every selected course must currently be a standalone offering.",
            "conflicts": [{"code": "offering_already_combined"}],
        })
    _active_section_conflict(old_groups)
    courses = [item.course for item in offerings]
    if combined_allowed_semester(courses) is None:
        raise OfferingValidationError({
            "detail": "The selected courses have no common legal semester."
        })
    predicted_by_course = _predicted_primary_demand(academic_year)
    predicted_demand = sum(predicted_by_course.get(course.id, 0) for course in courses)
    if ceil(predicted_demand) > rule.capacity_profile.hard_max:
        raise OfferingValidationError({
            "detail": (
                f"Combined predicted demand ({predicted_demand:g}) exceeds the shared "
                f"section capacity ({rule.capacity_profile.hard_max})."
            )
        })
    group = DeliveryGroup.objects.create(
        academic_year=academic_year,
        name=" / ".join(item.course.course_code for item in offerings),
        capacity_profile=rule.capacity_profile,
        combination_rule=rule,
        created_by=actor,
        reason=reason,
    )
    for offering in offerings:
        previous_group = offering.delivery_group
        offering.delivery_group = group
        offering.decision_reason = reason
        offering.decided_by = actor
        offering.save(update_fields=[
            "delivery_group", "decision_reason", "decided_by", "decided_at",
        ])
        CourseOfferingDecision.objects.create(
            offering=offering,
            action=COURSE_OFFERING_ACTION_COMBINED,
            previous_status=offering.status,
            new_status=offering.status,
            previous_delivery_group=previous_group,
            new_delivery_group=group,
            decided_by=actor,
            reason=reason,
        )
    for old_group in old_groups:
        old_group.status = DELIVERY_GROUP_STATUS_RETIRED
        old_group.save(update_fields=["status"])
    return group


@transaction.atomic
def separate_delivery_group(group, *, actor, reason):
    """Return an unmaterialized combined group to standalone offerings."""

    reason = _clean_reason(reason)
    group = DeliveryGroup.objects.select_for_update().get(pk=group.pk)
    offerings = list(
        group.offerings.select_for_update()
        .select_related("course__capacity_profile", "academic_year")
        .order_by("course__course_code")
    )
    if len(offerings) < 2:
        raise OfferingValidationError({"detail": "This delivery group is not combined."})
    _active_section_conflict([group])
    for offering in offerings:
        standalone = _standalone_group(
            offering.course,
            offering.academic_year,
            actor=actor,
            reason=reason,
        )
        offering.delivery_group = standalone
        offering.decision_reason = reason
        offering.decided_by = actor
        offering.save(update_fields=[
            "delivery_group", "decision_reason", "decided_by", "decided_at",
        ])
        CourseOfferingDecision.objects.create(
            offering=offering,
            action=COURSE_OFFERING_ACTION_SEPARATED,
            previous_status=offering.status,
            new_status=offering.status,
            previous_delivery_group=group,
            new_delivery_group=standalone,
            decided_by=actor,
            reason=reason,
        )
    group.status = DELIVERY_GROUP_STATUS_RETIRED
    group.save(update_fields=["status"])
    return offerings


def get_combination_suggestions(academic_year):
    """Return safe, non-writing suggestions from active approved rules only."""

    ensure_academic_year_offerings(academic_year)
    suggestions = []
    primary_counts = _predicted_primary_demand(academic_year)
    for rule in CourseCombinationRule.objects.filter(is_active=True).select_related(
        "capacity_profile"
    ).prefetch_related("members__course"):
        courses = [member.course for member in rule.members.all()]
        if len(courses) < 2 or combined_allowed_semester(courses) is None:
            continue
        offerings = list(CourseOffering.objects.filter(
            academic_year=academic_year,
            course__in=courses,
            status=COURSE_OFFERING_STATUS_OFFERED,
            delivery_group__status=DELIVERY_GROUP_STATUS_ACTIVE,
        ).select_related("delivery_group"))
        if len(offerings) != len(courses):
            continue
        if any(item.delivery_group.offerings.count() != 1 for item in offerings):
            continue
        if active_sections_queryset(
            Section.objects.filter(
                delivery_group__in=[item.delivery_group for item in offerings],
            )
        ).exists():
            continue
        pooled = sum(primary_counts.get(course.id, 0) for course in courses)
        if not pooled or pooled > rule.capacity_profile.hard_max:
            continue
        standalone_count = sum(
            max(1, ceil(primary_counts.get(course.id, 0) / course.capacity_profile.target))
            for course in courses
            if primary_counts.get(course.id, 0)
        )
        below_min = any(
            0 < primary_counts.get(course.id, 0) < course.capacity_profile.hard_min
            for course in courses
        )
        if standalone_count <= 1 and not below_min:
            continue
        suggestions.append({
            "rule_id": rule.id,
            "rule_name": rule.name,
            "course_ids": [course.id for course in courses],
            "course_codes": [course.course_code for course in courses],
            "pooled_predicted_enrollment": pooled,
            "shared_capacity_max": rule.capacity_profile.hard_max,
            "estimated_sections_saved": max(0, standalone_count - 1),
            "reasons": [
                reason for condition, reason in (
                    (below_min, "member_below_hard_min"),
                    (standalone_count > 1, "reduces_physical_sections"),
                ) if condition
            ],
        })
    return suggestions
