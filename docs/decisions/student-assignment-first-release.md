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

This describes the implemented first release. The accepted and now-implemented
follow-on decision
[`student-assignment-reruns-and-locks.md`](student-assignment-reruns-and-locks.md)
defines the explicitly approved replacement workflow for scoped, unlocked
active enrollments. It does not change the behavior or audit meaning of
existing first-release runs.

The accepted special-program extension
[`special-student-schedule-commitments.md`](special-student-schedule-commitments.md)
adds Study, Focus, Co-op, online supervision, and the school's narrow
half-semester pattern without changing the meaning of a normal instructional
section or enrollment. Its special occupancy records and online-enrollment
provenance join this stage's immutable snapshot/approval model.

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
locks are outside this first-release contract and are available only through
the follow-on controlled-rerun decision in `final_staffing`.

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
balance, soft sequence preferences, difficulty balance, and course-category
diversity. Labels are compiled to deterministic engine priorities and no
numeric weight is exposed through the API. Mandatory fulfillment, primary
fulfillment by course priority tier, approved backups, capacity, timeslot
safety, and hard prerequisite sequencing always take precedence over these
soft controls.

Course difficulty is a scheduling-oriented 0--100 estimate, not a claim of
objective academic difficulty. `StudentCourseHistoricalResult` now stores the
immutable source fact (student, course, academic year, final mark); enrollment
and demand history are not treated as achievement evidence. Without usable
history, `metadata_and_relative_history_v2` combines a bounded Grade 7--12
baseline with deliberately small category and Ontario course-designation
signals. With history, it compares each course mark with that student's
same-year leave-one-course-out average, applies a 0.70-per-year recency decay,
and blends the historical estimate toward metadata until twelve weighted
observations establish full confidence. A counselor may set a bounded
`manual_difficulty_override`; it becomes the effective value for future runs.
Each immutable input snapshot records calculation, override, effective value,
source, metadata, designation, historical observation/year counts, confidence,
and relative-performance evidence, so later data changes stale rather than
silently reinterpret a reviewed run.

Category diversity is also soft. Equal course categories are inherently fully
similar. `CourseCategoryRelationship` supplies an optional, school-wide,
unordered 0--100 similarity for distinct categories; an unspecified pair is
neutral. The engine penalizes similar course pairs concentrated in the same
semester, without assigning categories artificial scalar positions. Missing
category data is neutral. Neither new objective can move a fixed enrollment or
violate a capacity, collision, prerequisite, eligibility, or lock rule.

Runs, approvals, and enrollment-provenance rows are immutable. Review and
approval reload and fingerprint the current input; approval locks the relevant
target-year facts, does not re-solve, and writes every new enrollment and its
provenance in one transaction. Only a complete candidate, meaning every
mandatory and primary effective request is fulfilled, may be approved. Unmet
approved alternates may remain in a complete result.

## Outside first-release scope

- transcript/CSV/SIS prerequisite-completion evidence;
- student, course, section-roster, whole-schedule, and teacher-dependent locks
  (implemented in the follow-on controlled-rerun increment, not in this first
  release);
- partial/scoped reruns and controlled active-enrollment replacement
  (implemented in the follow-on increment; general manual overrides remain
  deferred);
- conflict-analysis and composed personal-timetable endpoints;
- room assignment, frontend work, asynchronous workers, and background jobs.

The first two areas have an accepted and implemented follow-on contract in
[`student-assignment-reruns-and-locks.md`](student-assignment-reruns-and-locks.md).
They remain outside this first-release contract, even though the follow-on
controlled-rerun capability is now present.

Development schema changes follow the repository's migrationless local
`migrate --run-syncdb` workflow. This decision does not authorize generating
Django migration files.

`scheduling_engine/benchmark_student_assignment.py` provides a deterministic
approximately 1,400-student/300-section fixture for manual target-scale
measurement. Step 7 showed that its 9,800 required requests have 10,500 usable
seats, no course-specific shortage, and a complete independent
capacity/timeslot assignment. The original one-worker lexicographic CP-SAT pass
timed out with `solver_outcome: unknown` before finding any candidate; it was
not an infeasibility proof.

Step 9 keeps the one-worker, seed-0 CP-SAT configuration and now first asks
CP-SAT for a complete hard-feasible student schedule before it starts the
existing lexicographic improvement passes. The feasibility model is a clone of
the production model's shared hard-constraint prefix: it requires every
movable mandatory/primary request and requested Study, Focus, or Co-op
commitment to be selected exactly once, while fixed enrollment/commitment
context remains accepted context rather than being selected again. It contains
the same eligibility, capacity, occupancy, lock, group, half-semester, online
supervision, and prerequisite constraints as production; it adds no soft
objective and does not use a greedy schedule.

When CP-SAT returns a complete feasibility candidate, the engine fixes its
source decisions in the full production model and performs a bounded
validation solve before using it as the initial incumbent for the unchanged
lexicographic objective sequence. The objective solver remains free to improve
the seed. If a later lower-priority pass times out, the validated complete
candidate from the prior pass (or the feasibility seed) remains the returned
recommendation. If feasibility cannot produce or validate a complete candidate
within its configured solve, the established independent-request hint and
ordinary CP-SAT fallback remain available; no incomplete candidate is presented
as complete. Empty objective tiers and the redundant final cold solve are
skipped; a fully protected run with no decision variables still receives one
feasibility solve so it remains reviewable. These reliability changes neither
weaken a hard rule nor replace CP-SAT as the constraint authority.

Accepted placement and aggregate course capacity are necessary input facts, but
they are not by themselves proof that every mandatory personal timetable can
be completed. A shared timing/capacity bottleneck can still make a set of
individually eligible requests impossible together. In that case the
hard-feasibility stage must return `infeasible`; neither a seed nor the later
lexicographic stage may present an incomplete result as complete.

On the deterministic 1,400-student benchmark, this architecture returned all
9,800 assignments with no unmet request in 91.187 seconds (the final objective
pass reported `unknown` after retaining the complete incumbent). This remains
representative-fixture evidence, not a claim that real-school data, HTTP
request limits, or future larger deployments have been performance-qualified.

## Step 10 realistic-condition validation

`scheduling_engine/realistic_student_assignment_validation.py` adds a separate
deterministic validation surface. Its compact scenario exercises uneven
capacities, an approved backup, a missing offering, same-year prerequisite
sequencing, A-D collision safety, protected and historical enrollments, an
exact lock, schedule preservation, and a scoped rerun. The intentionally
unmet requests remain partial results with stable diagnostics; the engine does
not manufacture seats or relax a collision.

Its separate 1,400-student/300-section fixture has ten high-demand, twenty
medium-demand, and twenty low-demand courses with capacities of 336, 216, and
141 seats respectively against demand of 280, 210, and 140. Step 10 found that
the initial seed previously considered mandatory requests before tight
low-capacity requests and could leave avoidable demand unseeded. The seed now
orders each student's guidance by course-offering slack first; CP-SAT still
validates the completed hint against every hard rule. The unchanged fixture
then completed all 9,800 requests in 72.132 and 76.375 seconds with identical
assignment-level and section-load output.

This validation also records current model boundaries rather than inventing
unsupported rules: student-specific course eligibility and optional
(non-primary/non-approved-backup) requests are not represented in the DTO or
adapter. Catalog/offering availability, teacher-specific locks in final
staffing, and no-placed-section diagnostics are not substitutes for a
student-program eligibility model. Historical prerequisite completion remains
assumed under this first-release decision.
