"""Transactional annual course-conflict matrix setup, refresh, and adjustment."""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from hashlib import sha256
from itertools import combinations

from django.db import transaction
from django.utils import timezone

from backend.apps.common.exceptions import DomainConflictError, DomainValidationError
from backend.apps.constraints.constants import (
    COURSE_CONFLICT_MATRIX_CARRY_OVERRIDES,
    COURSE_CONFLICT_MATRIX_FRESH,
)
from backend.apps.constraints.models import (
    CourseConflict,
    CourseConflictAdjustment,
    CourseConflictMatrix,
)
from backend.apps.courses.selectors import (
    offered_course_offerings_for_year,
    primary_course_requests_queryset,
)
from backend.apps.scheduling.services.demand_forecasting import (
    historical_conversion_evidence_by_course,
)


def _active_course_ids(academic_year_id):
    return sorted(set(
        offered_course_offerings_for_year(academic_year_id)
        .values_list("course_id", flat=True)
    ))


def _primary_request_sets(academic_year_id, course_ids):
    """Load primary demand once for pair metrics and a stable safe fingerprint."""

    request_sets = defaultdict(set)
    rows = primary_course_requests_queryset().filter(
        academic_year_id=academic_year_id,
        course_id__in=course_ids,
    ).values_list("student_id", "course_id")
    digest = sha256()
    for student_id, course_id in sorted(rows):
        request_sets[course_id].add(student_id)
        # IDs detect source drift while keeping student identities out of the
        # matrix audit record itself.
        digest.update(f"{student_id}:{course_id};".encode())
    return request_sets, digest.hexdigest()


def _matrix_rows(matrix, *, carry_source=None):
    course_ids = _active_course_ids(matrix.academic_year_id)
    request_sets, fingerprint = _primary_request_sets(matrix.academic_year_id, course_ids)
    ratios = historical_conversion_evidence_by_course(matrix.academic_year)
    source_overrides = {
        (row.course_a_id, row.course_b_id): row.weight
        for row in (carry_source.conflicts.filter(is_overridden=True) if carry_source else ())
    }
    rows = []
    for course_a_id, course_b_id in combinations(course_ids, 2):
        left, right = request_sets[course_a_id], request_sets[course_b_id]
        co_requested, union = len(left & right), len(left | right)
        computed = Decimal("0") if not union else Decimal(100 * co_requested / union).quantize(Decimal("0.01"))
        evidence_a = ratios.get(course_a_id, {"ratio": 1.0, "uses_current_demand_fallback": True})
        evidence_b = ratios.get(course_b_id, {"ratio": 1.0, "uses_current_demand_fallback": True})
        fallback = evidence_a["uses_current_demand_fallback"] or evidence_b["uses_current_demand_fallback"]
        retained = Decimal(
            co_requested if fallback else co_requested * min(evidence_a["ratio"], evidence_b["ratio"])
        ).quantize(Decimal("0.01"))
        pair = (course_a_id, course_b_id)
        overridden = pair in source_overrides
        rows.append({
            "matrix": matrix,
            "course_a_id": course_a_id,
            "course_b_id": course_b_id,
            "computed_weight": computed,
            "weight": source_overrides[pair] if overridden else computed,
            "co_request_count": co_requested,
            "union_request_count": union,
            "estimated_retained_co_request_count": retained,
            "uses_current_demand_fallback": fallback,
            "is_overridden": overridden,
        })
    return rows, fingerprint


@transaction.atomic
def create_course_conflict_matrix(*, academic_year, actor, initialization_mode, source_matrix=None):
    """Create the one annual matrix, carrying forward counselor judgment only."""

    if initialization_mode not in {COURSE_CONFLICT_MATRIX_FRESH, COURSE_CONFLICT_MATRIX_CARRY_OVERRIDES}:
        raise DomainValidationError({"initialization_mode": "Unsupported matrix setup mode."})
    if CourseConflictMatrix.objects.select_for_update().filter(academic_year=academic_year).exists():
        raise DomainConflictError({"detail": "This academic year already has a course conflict matrix."})
    if initialization_mode == COURSE_CONFLICT_MATRIX_CARRY_OVERRIDES and source_matrix is None:
        raise DomainValidationError({"source_matrix": "Carry-forward setup requires a source matrix."})
    matrix = CourseConflictMatrix.objects.create(
        academic_year=academic_year,
        initialization_mode=initialization_mode,
        source_matrix=source_matrix,
        created_by=actor,
    )
    rows, fingerprint = _matrix_rows(
        matrix,
        carry_source=source_matrix if initialization_mode == COURSE_CONFLICT_MATRIX_CARRY_OVERRIDES else None,
    )
    CourseConflict.objects.bulk_create([CourseConflict(**row) for row in rows])
    matrix.request_fingerprint = fingerprint
    matrix.save(update_fields=["request_fingerprint"])
    return matrix


@transaction.atomic
def refresh_course_conflict_matrix(matrix, *, actor):
    """Refresh evidence without silently erasing a counselor's override."""

    matrix = CourseConflictMatrix.objects.select_for_update().get(pk=matrix.pk)
    rows, fingerprint = _matrix_rows(matrix)
    existing = {
        (item.course_a_id, item.course_b_id): item
        for item in CourseConflict.objects.select_for_update().filter(matrix=matrix)
    }
    for row in rows:
        item = existing.get((row["course_a_id"], row["course_b_id"]))
        if item is None:
            CourseConflict.objects.create(**row)
            continue
        item.computed_weight = row["computed_weight"]
        item.co_request_count = row["co_request_count"]
        item.union_request_count = row["union_request_count"]
        item.estimated_retained_co_request_count = row["estimated_retained_co_request_count"]
        item.uses_current_demand_fallback = row["uses_current_demand_fallback"]
        if not item.is_overridden:
            item.weight = row["computed_weight"]
        item.save(update_fields=[
            "computed_weight", "co_request_count", "union_request_count",
            "estimated_retained_co_request_count", "uses_current_demand_fallback", "weight",
        ])
    matrix.revision += 1
    matrix.request_fingerprint = fingerprint
    matrix.refreshed_by = actor
    matrix.refreshed_at = timezone.now()
    matrix.save(update_fields=["revision", "request_fingerprint", "refreshed_by", "refreshed_at"])
    return matrix


@transaction.atomic
def adjust_course_conflict(*, matrix, conflict_id, new_weight, reason, actor):
    """Write an immutable audit entry before changing an effective score."""

    if not str(reason).strip():
        raise DomainValidationError({"reason": "A reason is required when adjusting a conflict score."})
    matrix = CourseConflictMatrix.objects.select_for_update().get(pk=matrix.pk)
    try:
        conflict = CourseConflict.objects.select_for_update().get(pk=conflict_id, matrix=matrix)
    except CourseConflict.DoesNotExist as error:
        raise DomainValidationError({"conflict_id": "This conflict does not belong to the selected matrix."}) from error
    weight = Decimal(str(new_weight)).quantize(Decimal("0.01"))
    if not Decimal("0") <= weight <= Decimal("100"):
        raise DomainValidationError({"weight": "Weight must be between 0 and 100."})
    CourseConflictAdjustment.objects.create(
        conflict=conflict, previous_weight=conflict.weight, new_weight=weight,
        reason=reason.strip(), adjusted_by=actor,
    )
    conflict.weight = weight
    conflict.is_overridden = True
    conflict.save(update_fields=["weight", "is_overridden"])
    matrix.revision += 1
    matrix.save(update_fields=["revision"])
    return conflict
