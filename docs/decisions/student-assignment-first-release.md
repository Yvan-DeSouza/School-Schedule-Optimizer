# Student Assignment First Release

**Status:** Accepted and implemented

## Decision

Student-to-section assignment is a separate, counselor-reviewed stage after
active sections have accepted semester/A-D placement. It consumes those sections
as fixed context and recommends only new `Enrollment` rows. It never changes a
section, its accepted timeslot, its room, or `Section.teacher`.

Only active target-year sections participate. Every such section must have a
target-year `SectionSchedule.timeslot`; rooms remain out of scope. Existing
enrollments are fixed capacity and student-timeslot context. Approval neither
deletes nor moves them.

## Staffing-assumption modes

The counselor chooses one transparent staffing mode for every immutable run:

- `sections_only`: teacher information is excluded completely, including from
  stale-input detection.
- `partial_staffing`: final `Section.teacher` values may be present or absent;
  their current mapping is review context and drift-sensitive.
- `provisional_staffing`: no active section has a final teacher. A complete,
  unapproved `TeacherAssignmentRun` must cover every active section, and its
  mapping is provisional review context only. Approval or staleness of that
  source invalidates the student run.
- `final_staffing`: every active section has a final `Section.teacher`, whose
  mapping is drift-sensitive context.

Teacher identity does not change student eligibility or scoring in this first
release. The modes record what the counselor trusted, rather than authorizing
the student workflow to modify teacher assignments. Teacher-dependent student
locks are deferred.

## Prerequisites, sequencing, and alternates

`CoursePrerequisite` remains the catalog-owned hard relationship. Earlier
completion is assumed for this first release: no transcript, credit, grade, or
CSV/SIS evidence is imported or validated. When a student receives both sides
of a prerequisite in the same target year, the prerequisite must be in Semester
1 and the dependent course in Semester 2. A dependent course on its own is
allowed under the assumed-prior-completion policy. Directed hard cycles are
invalid configuration; an impossible same-year chain produces a reviewable,
non-approvable partial result.

`CourseSequencePreference` is a separate active/inactive, directional soft
catalog rule. It rewards, but never requires, the outcome of its earlier course
in Semester 1 and later course in Semester 2 when a student takes both. Self
links, duplicate directional links, and directed cycles are rejected.

An alternate enters this stage only if approved active upstream budget/staffing
provenance records `backup_promoted` for a cancelled primary request. The stage
does not independently promote, discard, or reinterpret alternates. Ambiguous
or unresolved cancellation provenance fails closed.

## Objectives and approval

Counselors select a label from `not_important` through
`extremely_important` for section-utilization balance, student semester-load
balance, and soft sequence preferences. Labels are compiled to deterministic
engine priorities and no numeric weight is exposed through the API. Mandatory
fulfillment, primary fulfillment by course priority tier, approved backups,
capacity, timeslot safety, and hard prerequisite sequencing always take
precedence over these soft controls.

Runs, approvals, and enrollment-provenance rows are immutable. Review and
approval reload and fingerprint the current input; approval locks the relevant
target-year facts, does not re-solve, and writes every new enrollment and its
provenance in one transaction. Only a complete candidate, meaning every
mandatory and primary effective request is fulfilled, may be approved. Unmet
approved alternates may remain in a complete result.

## Deliberately deferred

- transcript/CSV/SIS prerequisite-completion evidence;
- student, course, section-roster, whole-schedule, and teacher-dependent locks;
- partial/scoped reruns and manual enrollment overrides;
- conflict-analysis and composed personal-timetable endpoints;
- room assignment, frontend work, asynchronous workers, and background jobs.

Development schema changes follow the repository's migrationless local
`migrate --run-syncdb` workflow. This decision does not authorize generating
Django migration files.

`scheduling_engine/benchmark_student_assignment.py` provides a deterministic
approximately 1,400-student/300-section fixture for manual target-scale
measurement. Its elapsed time and fulfillment result must be recorded before
this stage is described as target-scale ready.
