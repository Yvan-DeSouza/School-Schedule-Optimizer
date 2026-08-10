# Student Assignment Reruns, Enrollment History, and Locks

**Status:** Accepted and implemented through Step 6

## Context

The original first student-assignment release creates only new `Enrollment`
rows. Existing enrollments are fixed context and approval never moves or
deletes them. That behavior remains historically correct for all first-release
runs and approvals. Steps 1-6 now add a separate controlled-rerun path for
later reviewed changes.

Counselors next need to make carefully scoped changes after enrollment has
begun, without turning an accepted schedule into an editable draft or erasing
the evidence that explains a student's prior placement. The project also needs
to explain the cost of counselor locks before a counselor chooses to keep or
release one.

## Decision

### Enrollment history and controlled changes

The controlled-rerun release distinguishes operationally active enrollments from
historical enrollment records. Only active enrollments will consume a section
seat, occupy a student's timeslot, or participate in future solver input.
Historical records remain attached to their original student, section, and
course offering as audit evidence; they are never deleted to make room for a
new recommendation.

When a counselor approves a reviewed move, the approval will retire the prior
active enrollment and create the replacement active enrollment in the same
transaction. Immutable approval provenance will connect the replacement to the
enrollment it superseded. This is an explicit student-assignment correction,
not a general manual override and not an in-place rewrite of an earlier
approval.

Full and scoped reruns remain immutable `StudentAssignmentRun` records. Their
input snapshot will record the resolved scope, selected locks, effective
priority-request limit, and soft-priority settings. Approval will revalidate
that context under transaction locks and accept or reject the whole reviewed
candidate; individual student moves cannot be partially approved.

Outside-scope students, whole-schedule locks, exact enrollment locks, and
frozen rosters remain fixed context. Only the selected scope's unlocked active
enrollments may be retired and replaced. A scoped run may therefore report an
unresolved protected request, but it must explain that protected condition and
may not silently move the protected assignment.

### Section cancellation and reconciliation

The current section-lifecycle decision correctly prevents ordinary
reconciliation from retiring a section with active enrollment dependencies.
The implemented bridge returns
`student_assignment_section_cancellation_requires_rerun` and identifies the
affected students. Their active enrollments must first be resolved through a
reviewed student-assignment rerun. Once those rows are historical, ordinary
reconciliation may retire the section; historical rows remain audit evidence
and do not block operational retirement.

This is not an automatic cascade or a single cross-workflow transaction. A
cancellation with unresolved required student demand, stale input, or
conflicting locks remains non-approvable. The section's prior schedules,
teachers, placement facts, and historical enrollments remain preserved for
audit. Active sections, accepted A-D timeslots, rooms, and `Section.teacher`
continue to be fixed context for student assignment; the workflow never moves
them.

The shared section-state helper remains the sole owner of fixed-context rules.
It now treats an active enrollment as an operational dependency while a
historical enrollment remains an audit dependency. Placement and named-teacher
assignment were not redesigned by this decision.

### Locks, priorities, and staffing context

Counselor-created student-assignment locks require a nonblank reason and
append-only creation/release audit. They support the accepted
graduated lock types: exact student-to-section, whole student schedule,
section-roster freeze, course-roster freeze, same-section student group, and a
student-to-teacher-for-course lock.

The teacher-specific lock is available only in `final_staffing`. It constrains
student eligibility using the already final `Section.teacher` mapping and does
not authorize the student workflow to alter teacher assignments. The other
three staffing modes keep their first-release behavior.

The future global `StudentAssignmentConfiguration` will own
`max_priority_requests`, defaulting to `100`. It is one school-wide planning
setting, not an academic-year value. Until that model exists, the current
effective cap is the documented 100-request placeholder in
`backend/apps/scheduling/constants.py`. A run snapshots the effective limit and
may nominate at most that many specific primary requests. A nominated request
ranks after mandatory fulfillment and before ordinary primary fulfillment; it
never displaces a mandatory request.

Schedule preservation is a fourth, rerun-only soft priority with the separate
labels `none`, `slight`, `moderate`, and `strong`. The existing three
five-level student-assignment soft controls remain unchanged. No API exposes
numeric solver weights.

### Stable result and workflow contract

The following codes are defined and covered by tests so solver results and
workflow errors can be consumed without clients depending on English wording.
Existing
capacity, timeslot-collision, and hard-prerequisite codes remain the canonical
codes for those already-defined conditions.

New solver diagnostics in `scheduling_engine/diagnostics.py`:

- `student_assignment_no_active_placed_section`
- `student_assignment_locked_enrollment_blocks_request`
- `student_assignment_section_below_target_capacity`
- `student_assignment_section_over_target_concentration`
- `student_assignment_limited_seat_contention`
- `student_assignment_requires_additional_capacity`
- `student_assignment_requires_timeslot_change`
- `student_assignment_requires_lock_release`
- `student_assignment_requires_placed_section`
- `student_assignment_requires_prerequisite_sequence_change`

New scheduling-workflow codes in `backend.apps.scheduling.codes`:

- `student_assignment_lock_invalid_target`
- `student_assignment_lock_final_staffing_required`
- `student_assignment_rerun_scope_invalid`
- `student_assignment_rerun_context_changed`
- `student_assignment_what_if_lock_not_active`
- `student_assignment_section_cancellation_requires_rerun`

## Consequences

This decision deliberately extends the student-assignment contract from
append-only new enrollment creation to controlled active enrollment
replacement with immutable history. It does not change the meaning of
existing first-release approval records.

Steps 1-6 implemented the enrollment lifecycle state, replacement provenance,
student-assignment locks, group membership, scoped-run fields, adapter/engine
support, review/what-if behavior, approval drift checks, and the cancellation
bridge. Development remains migrationless: no Django migration files were
generated; the project owner recreates a local database with
`migrate --run-syncdb` after approved schema work.

The global `StudentAssignmentConfiguration` model, transcript/CSV/SIS
completion evidence, general manual overrides, conflict analysis, personal
timetable endpoints, frontend work, and background jobs remain deferred.

## Step 6 verification record

The hardening pass added policy- and endpoint-level authorization assertions,
engine regressions for scope, locks, priorities, preservation, staffing, and
structured explanations, approval rollback/drift tests, cancellation and
historical-enrollment lifecycle tests, stable-code coverage, and a deterministic
read-only what-if test.

The approximately 1,400-student/300-section benchmark was run with:

```powershell
$env:PYTHONPATH='.'; .\.venv\Scripts\python.exe -m scheduling_engine.benchmark_student_assignment
```

On the development Windows environment, it completed in 36.285
seconds but returned `infeasible` with 0 assignments and 9,800 unmet requests.
That is evidence that target-scale quality is not yet established; it is not a
claim of target-scale readiness. No speculative solver or infrastructure change
was made in this hardening pass.
