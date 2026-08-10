"""End-to-end safety contract for revising already-materialized section plans.

These tests intentionally create compact immutable planning runs directly. The
section-count solver has its own coverage; this module concentrates on the
counselor review/apply workflow and its operational-data protections.
"""

import pytest
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework.test import APIClient

from backend.apps.common.constants import (
    SECTION_LIFECYCLE_ACTIVE,
    SECTION_LIFECYCLE_RETIRED,
    SECTION_PLANNING_RUN_STATUS_COMPLETE,
    SECTION_RECONCILIATION_ACTION_CREATED,
    SEMESTER_FALL,
    SEMESTER_WINTER,
)
from backend.apps.control.models import ManualOverride, SectionLock
from backend.apps.courses.models import Enrollment, Section
from backend.apps.scheduling.models import (
    SectionPlanningApproval,
    SectionPlanningReconciliation,
    SectionPlanningReconciliationAction,
    SectionPlanningReconciliationCourse,
    SectionPlanningRun,
    SectionSchedule,
)
from backend.apps.scheduling.services.engine_adapter import load_scheduling_input
from backend.apps.scheduling.services.section_planning import approve_section_planning_run
from backend.apps.scheduling.services.section_reconciliation import (
    reconcile_section_planning_run,
    preview_section_plan_reconciliation,
)


def create_frozen_run(academic_year, user, course, semester_1_count, semester_2_count):
    """Persist the minimum valid result shape consumed by approval services."""

    profile = course.capacity_profile
    course_result = {
        "course_id": course.id,
        "course_code": course.course_code,
        "priority_tier": course.priority_profile.tier,
        "predicted_enrollment": 24,
        "unmet_demand": 0,
        "semester_1_count": semester_1_count,
        "semester_2_count": semester_2_count,
        "capacity_policy": {
            "hard_min": profile.hard_min,
            "soft_min": profile.soft_min,
            "target": profile.target,
            "soft_max": profile.soft_max,
            "hard_max": profile.hard_max,
        },
        "allowed_semester": course.allowed_semester,
        "warnings": [],
        "reasons": [],
    }
    return SectionPlanningRun.objects.create(
        academic_year=academic_year,
        created_by=user,
        status=SECTION_PLANNING_RUN_STATUS_COMPLETE,
        input_snapshot={},
        result={
            "status": SECTION_PLANNING_RUN_STATUS_COMPLETE,
            "courses": [course_result],
            "diagnostics": [],
        },
    )


def materialize_initial_plan(academic_year, user, course, semester_1_count, semester_2_count):
    """Use the original approval path to give initial sections real provenance."""

    run = create_frozen_run(
        academic_year,
        user,
        course,
        semester_1_count,
        semester_2_count,
    )
    approve_section_planning_run(
        run,
        approved_by=user,
        selections=[{
            "course_id": course.id,
            "semester_1_count": semester_1_count,
            "semester_2_count": semester_2_count,
        }],
        reason="Initial approved plan.",
    )
    return run


def selection_for(course, semester_1_count, semester_2_count):
    return {
        "courses": [{
            "course_id": course.id,
            "semester_1_count": semester_1_count,
            "semester_2_count": semester_2_count,
        }],
    }


def preview_and_apply(client, run, course, semester_1_count, semester_2_count):
    """Exercise the same two-request handshake used by a counselor UI."""

    payload = selection_for(course, semester_1_count, semester_2_count)
    preview = client.post(
        f"/api/planning/section-count-runs/{run.id}/reconciliation-preview/",
        payload,
        format="json",
    )
    assert preview.status_code == 200
    assert preview.data["can_reconcile"] is True
    apply_payload = {
        **payload,
        "preview_token": preview.data["preview_token"],
        "reason": "Enrollment forecast changed after counselor review.",
    }
    response = client.post(
        f"/api/planning/section-count-runs/{run.id}/reconcile/",
        apply_payload,
        format="json",
    )
    assert response.status_code == 201
    return preview, response


@pytest.mark.django_db
def test_reconciliation_preview_and_apply_move_sections_without_changing_identity(
    authenticated_client,
    academic_year,
    course,
    counselor_user,
):
    materialize_initial_plan(academic_year, counselor_user, course, 2, 0)
    original_sections = list(Section.objects.order_by("section_number"))
    revised_run = create_frozen_run(academic_year, counselor_user, course, 1, 1)
    client = authenticated_client(counselor_user)

    preview, response = preview_and_apply(client, revised_run, course, 1, 1)

    # The oldest matching draft stays in Semester 1 and the surplus draft moves
    # to Semester 2. A move is an update, not a delete/create identity swap.
    course_preview = preview.data["courses"][0]
    assert preview.data["action_totals"] == {
        "keep": 1,
        "move": 1,
        "retire": 0,
        "reactivate": 0,
        "create": 0,
    }
    assert course_preview["actions"]["move"][0]["section_id"] == original_sections[1].id
    moved = Section.objects.get(pk=original_sections[1].id)
    assert moved.semester == SEMESTER_WINTER
    assert moved.section_number == "S2-01"
    assert set(Section.objects.values_list("id", flat=True)) == {
        section.id for section in original_sections
    }

    # The response exposes the actor/run/reason and concrete immutable actions,
    # not merely the final count pair.
    assert response.data["planning_run"] == revised_run.id
    assert response.data["reconciled_by"] == counselor_user.id
    assert response.data["reason"] == "Enrollment forecast changed after counselor review."
    assert response.data["course_reconciliations"][0]["course"] == course.id
    assert {item["action"] for item in response.data["course_reconciliations"][0]["actions"]} == {
        "kept",
        "moved",
    }
    assert SectionPlanningReconciliation.objects.count() == 1

    # A reconciled course is now approved from this immutable run. The original
    # approval endpoint remains conservative and cannot overwrite it afterward.
    duplicate = client.post(
        f"/api/planning/section-count-runs/{revised_run.id}/approve/",
        selection_for(course, 1, 1),
        format="json",
    )
    assert duplicate.status_code == 409

    retry_preview = client.post(
        f"/api/planning/section-count-runs/{revised_run.id}/reconciliation-preview/",
        selection_for(course, 1, 1),
        format="json",
    )
    assert retry_preview.status_code == 200
    assert retry_preview.data["can_reconcile"] is False
    assert retry_preview.data["conflicts"][0]["code"] == "course_already_approved_from_run"
    retry = client.post(
        f"/api/planning/section-count-runs/{revised_run.id}/reconcile/",
        {
            **selection_for(course, 1, 1),
            "preview_token": retry_preview.data["preview_token"],
            "reason": "Accidental duplicate submission.",
        },
        format="json",
    )
    assert retry.status_code == 409
    assert SectionPlanningReconciliation.objects.count() == 1


@pytest.mark.django_db
def test_retirement_reactivation_and_number_history_are_lossless(
    authenticated_client,
    academic_year,
    course,
    counselor_user,
):
    client = authenticated_client(counselor_user)
    materialize_initial_plan(academic_year, counselor_user, course, 2, 0)
    retiring = Section.objects.get(section_number="S1-02")

    # Reducing the target soft-retires surplus generated drafts. They disappear
    # from normal operational lists but remain directly inspectable for audit.
    reduction_run = create_frozen_run(academic_year, counselor_user, course, 1, 0)
    preview_and_apply(client, reduction_run, course, 1, 0)
    retiring.refresh_from_db()
    assert retiring.lifecycle_status == SECTION_LIFECYCLE_RETIRED
    assert client.get("/api/sections/").data["count"] == 1
    retired_list = client.get("/api/sections/?lifecycle_status=retired")
    assert retired_list.data["count"] == 1
    assert retired_list.data["results"][0]["id"] == retiring.id
    assert client.get(f"/api/sections/{retiring.id}/").data["lifecycle_status"] == "retired"
    assert client.patch(
        f"/api/sections/{retiring.id}/",
        {"capacity_max": retiring.capacity_max + 1},
        format="json",
    ).status_code == 400
    assert client.patch(
        f"/api/sections/{retiring.id}/lock/",
        {},
        format="json",
    ).status_code == 409
    assert client.delete(f"/api/sections/{retiring.id}/").status_code == 409

    # When demand returns, reconciliation revives the eligible historical row
    # before creating a new one, preserving its primary key.
    revival_run = create_frozen_run(academic_year, counselor_user, course, 1, 1)
    revival_preview, _ = preview_and_apply(client, revival_run, course, 1, 1)
    assert revival_preview.data["action_totals"]["reactivate"] == 1
    retiring.refresh_from_db()
    assert retiring.lifecycle_status == SECTION_LIFECYCLE_ACTIVE
    assert retiring.semester == SEMESTER_WINTER
    assert retiring.section_number == "S2-01"

    # S1-02 remains reserved in immutable history after the move. The next new
    # Semester 1 draft receives S1-03 rather than reusing a former identity.
    growth_run = create_frozen_run(academic_year, counselor_user, course, 2, 1)
    growth_preview, _ = preview_and_apply(client, growth_run, course, 2, 1)
    assert growth_preview.data["action_totals"]["create"] == 1
    assert Section.objects.filter(section_number="S1-03").exists()
    assert not Section.objects.filter(section_number="S1-02").exists()


def apply_protection(section, protection, teacher_user, student_user):
    """Attach one representative dependency that makes a section fixed."""

    if protection == "assigned_teacher":
        section.teacher = teacher_user.teacher_profile
        section.save(update_fields=["teacher"])
    elif protection == "section_flag_locked":
        section.is_locked = True
        section.save(update_fields=["is_locked"])
    elif protection == "section_lock":
        SectionLock.objects.create(section=section)
    elif protection == "section_schedule":
        SectionSchedule.objects.create(section=section)
    elif protection == "enrollments":
        Enrollment.objects.create(student=student_user.student_profile, section=section)
    elif protection == "manual_overrides":
        ManualOverride.objects.create(section=section, action="manual_test_change")


@pytest.mark.django_db
@pytest.mark.parametrize(
    "protection",
    (
        "assigned_teacher",
        "section_flag_locked",
        "section_lock",
        "section_schedule",
        "enrollments",
        "manual_overrides",
    ),
)
def test_dependency_bearing_generated_sections_are_fixed_not_retired(
    protection,
    authenticated_client,
    academic_year,
    course,
    counselor_user,
    teacher_user,
    student_user,
):
    materialize_initial_plan(academic_year, counselor_user, course, 1, 0)
    section = Section.objects.get()
    apply_protection(section, protection, teacher_user, student_user)
    revised_run = create_frozen_run(academic_year, counselor_user, course, 0, 0)
    payload = selection_for(course, 0, 0)
    client = authenticated_client(counselor_user)

    preview = client.post(
        f"/api/planning/section-count-runs/{revised_run.id}/reconciliation-preview/",
        payload,
        format="json",
    )
    assert preview.status_code == 200
    assert preview.data["can_reconcile"] is False
    assert preview.data["conflicts"][0]["code"] == "protected_sections_exceed_target"
    if protection == "enrollments":
        cancellation_conflict = next(
            item for item in preview.data["conflicts"]
            if item["code"] == "student_assignment_section_cancellation_requires_rerun"
        )
        assert cancellation_conflict["student_ids"] == [student_user.student_profile.id]
    keep = preview.data["courses"][0]["actions"]["keep"][0]
    assert protection in keep["protection_reasons"]

    # Applying a conflict-bearing preview is a state conflict, and no approval
    # or reconciliation history is partially written.
    response = client.post(
        f"/api/planning/section-count-runs/{revised_run.id}/reconcile/",
        {
            **payload,
            "preview_token": preview.data["preview_token"],
            "reason": "Attempted reduction.",
        },
        format="json",
    )
    assert response.status_code == 409
    section.refresh_from_db()
    assert section.lifecycle_status == SECTION_LIFECYCLE_ACTIVE
    assert not SectionPlanningApproval.objects.filter(planning_run=revised_run).exists()


@pytest.mark.django_db
def test_manual_sections_count_toward_target_and_cannot_be_displaced(
    authenticated_client,
    academic_year,
    course,
    counselor_user,
):
    manual = Section.objects.create(
        course=course,
        section_number="MAN-01",
        academic_year=academic_year,
        semester=SEMESTER_FALL,
        capacity_min=10,
        capacity_max=30,
    )
    revised_run = create_frozen_run(academic_year, counselor_user, course, 0, 0)
    preview = preview_section_plan_reconciliation(
        revised_run,
        selections=selection_for(course, 0, 0)["courses"],
    )

    assert preview["can_reconcile"] is False
    assert preview["conflicts"][0]["section_ids"] == [manual.id]
    assert preview["courses"][0]["actions"]["keep"][0]["protection_reasons"] == [
        "manual_section"
    ]


@pytest.mark.django_db
def test_preview_permissions_reason_validation_and_stale_state_guard(
    authenticated_client,
    academic_year,
    course,
    counselor_user,
    teacher_user,
):
    materialize_initial_plan(academic_year, counselor_user, course, 1, 0)
    revised_run = create_frozen_run(academic_year, counselor_user, course, 0, 1)
    preview_url = f"/api/planning/section-count-runs/{revised_run.id}/reconciliation-preview/"
    apply_url = f"/api/planning/section-count-runs/{revised_run.id}/reconcile/"
    payload = selection_for(course, 0, 1)

    assert APIClient().post(preview_url, payload, format="json").status_code == 401
    assert authenticated_client(teacher_user).post(preview_url, payload, format="json").status_code == 403
    client = authenticated_client(counselor_user)
    preview = client.post(preview_url, payload, format="json")
    assert preview.status_code == 200

    missing_reason = client.post(
        apply_url,
        {**payload, "preview_token": preview.data["preview_token"], "reason": "   "},
        format="json",
    )
    assert missing_reason.status_code == 400

    # Any intervening section-state edit changes the canonical preview. The
    # stale token must fail instead of applying a delta the counselor never saw.
    section = Section.objects.get()
    section.is_locked = True
    section.save(update_fields=["is_locked"])
    stale = client.post(
        apply_url,
        {
            **payload,
            "preview_token": preview.data["preview_token"],
            "reason": "Reviewed before another counselor locked the section.",
        },
        format="json",
    )
    assert stale.status_code == 409
    assert stale.data["conflicts"][0]["code"] == "reconciliation_preview_stale"
    assert not SectionPlanningApproval.objects.filter(planning_run=revised_run).exists()


@pytest.mark.django_db
def test_reconciliation_transaction_rolls_back_new_sections_and_audit(
    monkeypatch,
    academic_year,
    course,
    counselor_user,
):
    materialize_initial_plan(academic_year, counselor_user, course, 1, 0)
    revised_run = create_frozen_run(academic_year, counselor_user, course, 2, 0)
    selections = selection_for(course, 2, 0)["courses"]
    preview = preview_section_plan_reconciliation(revised_run, selections=selections)
    original_create = SectionPlanningReconciliationAction.objects.create

    def fail_created_action(**kwargs):
        # The new Section row is inserted immediately before its audit action.
        # Failing here proves both operational and audit inserts roll back.
        if kwargs["action"] == SECTION_RECONCILIATION_ACTION_CREATED:
            raise RuntimeError("simulated reconciliation audit failure")
        return original_create(**kwargs)

    monkeypatch.setattr(
        SectionPlanningReconciliationAction.objects,
        "create",
        fail_created_action,
    )
    with pytest.raises(RuntimeError, match="simulated reconciliation audit failure"):
        reconcile_section_planning_run(
            revised_run,
            reconciled_by=counselor_user,
            preview_token=preview["preview_token"],
            selections=selections,
            reason="Test rollback.",
        )

    assert Section.objects.count() == 1
    assert not SectionPlanningApproval.objects.filter(planning_run=revised_run).exists()
    assert SectionPlanningReconciliation.objects.count() == 0


@pytest.mark.django_db
def test_reconciliation_records_and_generated_identity_are_immutable(
    authenticated_client,
    academic_year,
    course,
    counselor_user,
):
    materialize_initial_plan(academic_year, counselor_user, course, 1, 0)
    revised_run = create_frozen_run(academic_year, counselor_user, course, 1, 1)
    _, response = preview_and_apply(
        authenticated_client(counselor_user),
        revised_run,
        course,
        1,
        1,
    )
    reconciliation = SectionPlanningReconciliation.objects.get(pk=response.data["id"])
    course_reconciliation = SectionPlanningReconciliationCourse.objects.get()
    action = SectionPlanningReconciliationAction.objects.first()

    with pytest.raises(DjangoValidationError):
        reconciliation.save()
    with pytest.raises(DjangoValidationError):
        course_reconciliation.save()
    with pytest.raises(DjangoValidationError):
        action.save()

    generated = Section.objects.filter(planning_approval_course__isnull=False).first()
    identity_edit = authenticated_client(counselor_user).patch(
        f"/api/sections/{generated.id}/",
        {"semester": SEMESTER_WINTER if generated.semester == SEMESTER_FALL else SEMESTER_FALL},
        format="json",
    )
    assert identity_edit.status_code == 400


@pytest.mark.django_db
def test_retired_sections_are_excluded_from_engine_input(
    academic_year,
    course,
):
    active = Section.objects.create(
        course=course,
        section_number="S1-01",
        academic_year=academic_year,
        semester=SEMESTER_FALL,
        capacity_min=10,
        capacity_max=30,
    )
    retired = Section.objects.create(
        course=course,
        section_number="S1-02",
        academic_year=academic_year,
        semester=SEMESTER_FALL,
        capacity_min=10,
        capacity_max=30,
        lifecycle_status=SECTION_LIFECYCLE_RETIRED,
    )
    SectionLock.objects.create(section=retired)

    data = load_scheduling_input(academic_year.id)

    assert [item.id for item in data.sections] == [active.id]
    assert data.section_locks == ()
