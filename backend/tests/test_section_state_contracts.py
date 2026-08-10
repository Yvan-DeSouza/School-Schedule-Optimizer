"""Contracts for active-section selectors and fixed-context interpretation."""

import pytest

from backend.apps.common.constants import SECTION_LIFECYCLE_RETIRED
from backend.apps.courses.constants import ENROLLMENT_LIFECYCLE_HISTORICAL
from backend.apps.courses.models import Enrollment
from backend.apps.people.models import RoleChoices
from backend.apps.control.models import SectionLock
from backend.apps.courses.selectors import active_sections_for_year
from backend.apps.courses.services.section_state import (
    FIXED_REASON_ASSIGNED_TEACHER,
    FIXED_REASON_MANUAL_SECTION,
    FIXED_REASON_SECTION_FLAG_LOCKED,
    FIXED_REASON_SECTION_LOCK,
    FIXED_REASON_ENROLLMENTS,
    FIXED_REASON_ENROLLMENT_HISTORY,
    fixed_context_reasons,
    is_fixed_context,
    section_delete_conflicts,
)
from backend.tests import factories


@pytest.mark.django_db
def test_active_sections_selector_excludes_retired_sections():
    year = factories.academic_year()
    item_course = factories.course()
    active = factories.section(year, item_course, section_number="S1-01")
    factories.section(
        year,
        item_course,
        section_number="S1-02",
        lifecycle_status=SECTION_LIFECYCLE_RETIRED,
    )

    assert list(active_sections_for_year(year.id)) == [active]


@pytest.mark.django_db
def test_fixed_context_reasons_explain_manual_locked_and_assigned_sections():
    year = factories.academic_year()
    item_course = factories.course()
    teacher = factories.teacher()
    section = factories.section(
        year,
        item_course,
        teacher_obj=teacher,
        is_locked=True,
    )
    SectionLock.objects.create(section=section)

    reasons = fixed_context_reasons(section)

    assert FIXED_REASON_MANUAL_SECTION in reasons
    assert FIXED_REASON_ASSIGNED_TEACHER in reasons
    assert FIXED_REASON_SECTION_FLAG_LOCKED in reasons
    assert FIXED_REASON_SECTION_LOCK in reasons
    assert is_fixed_context(section)


@pytest.mark.django_db
def test_delete_conflicts_share_fixed_context_lock_reason_codes():
    year = factories.academic_year()
    item_course = factories.course()
    section = factories.section(year, item_course, is_locked=True)
    SectionLock.objects.create(section=section)

    conflicts = section_delete_conflicts(section)

    assert FIXED_REASON_SECTION_FLAG_LOCKED in conflicts
    assert FIXED_REASON_SECTION_LOCK in conflicts


@pytest.mark.django_db
def test_historical_enrollment_is_audit_evidence_not_operational_section_dependency():
    year = factories.academic_year()
    item_course = factories.course()
    student = factories.user("historical-student", role=RoleChoices.STUDENT).student_profile
    section = factories.section(year, item_course)
    enrollment = Enrollment.objects.create(student=student, section=section)
    enrollment.lifecycle_status = ENROLLMENT_LIFECYCLE_HISTORICAL
    enrollment.save(update_fields=["lifecycle_status"])

    reasons = fixed_context_reasons(section)
    conflicts = section_delete_conflicts(section)

    assert FIXED_REASON_ENROLLMENTS not in reasons
    assert FIXED_REASON_ENROLLMENT_HISTORY in conflicts
