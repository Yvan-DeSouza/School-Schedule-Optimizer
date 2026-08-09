# Named Teacher Assignment After Accepted Placement

**Status:** Accepted and implemented

## Decision

Named teacher assignment is a separate counselor-reviewed stage after
semester/A-D placement approval. It consumes active `Section` rows with an
accepted `SectionSchedule.timeslot`, recommends a named teacher for each
otherwise unassigned section, and writes `Section.teacher` only after a
counselor/director approves an unchanged complete run.

The stage does not place sections, choose rooms, assign students, or modify an
accepted timeslot. A room remains nullable and untouched.

## Fixed context and locks

- An existing `Section.teacher` is fixed context and is never overwritten by a
  teacher-assignment approval.
- `SectionLock.locked_teacher` is an exact hard requirement for that section.
  A lock containing only a timeslot or room fixes that other dimension, not the
  choice of teacher.
- Active sections without an accepted target-year timeslot are configuration
  errors. They must go through the placement workflow before teacher assignment.
- Retired sections and inactive/cancelled delivery context are excluded through
  shared selectors.

## Staffing and preference contract

Only ready-roster teachers may be considered. Each roster member requires
explicit semester capacities and an annual capacity. Workload counts physical
sections; a combined physical section counts once toward workload but once for
each member course when evaluating a course-specific teacher rule.

Grades 11-12 use the existing normalized fail-closed qualification compiler.
Grades 9-10 remain legally permissive. Teacher availability is available by
default: only an explicit unavailable availability row removes a timeslot.

Counselor course rules are annual minimum/maximum bounds. `0` maximum prevents
a course, equal minimum/maximum expresses an exact count, and a minimum of one
requires at least one section. These rules never override qualifications,
availability, workload, timetable collisions, or an exact teacher lock.

Soft evidence is factual only: requested courses, courses taught in the prior
academic year, preferred/avoided slots, and seniority. The objective prioritizes
requested courses before continuity, then slot preference, seniority, and stable
deterministic tie-breaking. It does not invent numeric teacher preference scores.

## Approval and drift

Runs, approvals, and approval lines are append-only. The adapter stores a
detached DTO snapshot and deterministic fingerprint. Approval locks relevant
rows, reloads the input, and rejects roster, capacity, qualification,
availability, rule, preference, lock, placement, or fixed-assignment drift.
It never re-solves while writing because approval means accepting the reviewed
candidate, not a newly calculated one.

Partial and infeasible results are reviewable for diagnostics but are never
approvable. Approved lines preserve the exact relationship between the run,
section, and teacher.
