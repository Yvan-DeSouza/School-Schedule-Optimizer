# Section Lifecycle and Reconciliation Decision

Status: accepted and implemented

## Context

An approved section-count plan creates durable `Section` rows. During the same
planning season, demand and staffing assumptions can change. Replacing every
row would break stable identifiers and could erase or detach teacher, placement,
lock, enrollment, override, and approval history.

## Decision

`Section.lifecycle_status` has two values:

- `active`: the section participates in operational planning and solver input.
- `retired`: the section is preserved as read-only history and excluded from
  active lists and solver input.

Only the explicit section-plan reconciliation service changes lifecycle state.
Ordinary section CRUD exposes the value as read-only.

A reconciliation always starts from a newer immutable completed planning run.
The counselor previews selected or adjusted Semester 1/2 counts, reviews the
exact delta, and applies it with the preview token and a nonblank reason. Apply
locks current rows, recomputes the preview, rejects stale tokens, and writes all
section changes and audit rows in one transaction.

## Identity Rules

- An unchanged section keeps its database ID, semester, and number.
- A dependency-free generated section moved between semesters keeps its ID and
  receives the next unused number for the destination semester.
- A surplus dependency-free generated section is retired, never deleted.
- An eligible retired generated section is reactivated before a new row is
  created. It keeps its ID.
- Newly needed sections receive `S{semester}-{sequence}` numbers.
- Current, retired, previous, and new audited numbers are reserved permanently
  for that course and academic year; historical labels are not reused.

## Fixed Sections

An active section is fixed when any of the following is true:

- it was manually created and has no planning-approval provenance;
- it has an assigned teacher;
- `Section.is_locked` is true;
- it has a `SectionLock`;
- it has a `SectionSchedule`;
- it has an `Enrollment`;
- it has a `ManualOverride`.

An approved named-teacher assignment is recorded as an immutable approval line
and then becomes fixed through `Section.teacher`. Re-running teacher assignment
must treat that value as context; changing it requires a later explicit
reassignment/override workflow rather than replacing it during approval.

Fixed sections count toward the requested semester target. If their number is
already above that target, preview reports a conflict and apply writes nothing.
This conservative rule protects downstream work even while the project retains
both the boolean lock flag and structured lock row.

## Audit Rules

Each successful apply creates a new immutable `SectionPlanningApproval` linked
to the newer planning run and actor. A one-to-one reconciliation header records
the reviewed token and total before/after active counts. Per-course records hold
semester totals, and one immutable action per section records lifecycle,
semester, number, capacity, and fixed-reason before/after facts.

Created rows point to the new approval course. Existing rows retain their
original creation provenance and acquire reconciliation-action history. The
normal approval endpoint continues to refuse courses that already have any
section history; reconciliation is the only replacement workflow.

## Consequences

Downstream placement, teacher-assignment, and student-assignment stages can use
active sections as a stable input set. Retired history remains explainable and
cannot be silently edited or deleted. Reconciliation may be blocked by accepted
downstream work; resolving such a conflict requires a future explicit workflow,
not an automatic cascade.

Cross-listing and automatic course merging are outside this decision.
