"""Section-lock mutation service.

The structured ``SectionLock`` row owns locked teacher/timeslot/room values.
``Section.is_locked`` remains a coarse fixed-context flag for legacy/manual
sections; fixed-context helpers consider both sources instead of requiring the
two fields to be synchronized.
"""

from django.db import transaction

from backend.apps.common.exceptions import DomainConflictError
from backend.apps.constraints.services import validate_locked_teacher_qualifications
from backend.apps.control.models import SectionLock
from backend.apps.scheduling.constants import SECTION_LIFECYCLE_RETIRED


@transaction.atomic
def apply_section_lock(section, *, locked_teacher=None, locked_timeslot=None, locked_room=None):
    """Create or update the singleton structured lock for a section."""

    if section.lifecycle_status == SECTION_LIFECYCLE_RETIRED:
        raise DomainConflictError({
            "detail": "Retired sections are read-only and cannot receive scheduling locks."
        })

    if locked_timeslot is not None:
        # A timing lock is a promise about this concrete section, so accepting a
        # block from another year or semester would create a configuration that
        # no placement stage can faithfully honour.
        if locked_timeslot.academic_year_id != section.academic_year_id:
            raise DomainConflictError({
                "code": "locked_timeslot_outside_section_year",
                "detail": "A section lock must use a timeslot from the section's academic year.",
            })
        if locked_timeslot.semester != section.semester:
            raise DomainConflictError({
                "code": "locked_timeslot_outside_section_semester",
                "detail": "A section lock must use a timeslot in the section's semester.",
            })

    validate_locked_teacher_qualifications(section, locked_teacher)
    lock = (
        SectionLock.objects
        .select_for_update()
        # SectionLock's default ordering follows Section, whose nullable course
        # relation can produce an outer join that PostgreSQL refuses to lock.
        # Ordering by the lock row keeps the row lock precise and portable.
        .filter(section_id=section.id)
        .order_by("id")
        .first()
    )
    created = lock is None
    if lock is None:
        lock = SectionLock(section=section)
    lock.locked_teacher = locked_teacher
    lock.locked_timeslot = locked_timeslot
    lock.locked_room = locked_room
    lock.save()
    return lock, created
