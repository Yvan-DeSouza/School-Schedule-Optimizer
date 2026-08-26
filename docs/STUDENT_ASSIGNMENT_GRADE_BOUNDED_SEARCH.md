# Student Assignment Grade-Bounded Escape (Diagnostic)

## Status

Grade-bounded escape is an opt-in, diagnostic-only operator. It is not called
by ordinary student assignment, approval, or persistence workflows. Its
purpose is to characterize whether a mature incumbent can improve when one
actual student grade is allowed to move without the smaller R2/R4/R8 student
or source-decision bounds.

The supported operator identities are `grade_bounded_g9`,
`grade_bounded_g10`, `grade_bounded_g11`, and `grade_bounded_g12`. A session
selects one grade at construction time and does not switch grades during the
session.

## Scope semantics

The scope is based on the immutable `StudentAssignmentInputDTO.student_grades`
mapping, which is populated from the student's actual `grade_level` at the
ORM-to-engine adapter boundary. A course's catalog grade is never used as a
proxy for the student's grade.

The unchanged full student-assignment model is retained. Source decisions
owned by students outside the selected grade are fixed to the validated
incumbent. Source decisions owned by students in the selected grade are
unrestricted by a neighborhood radius or changed-student cap. Fixed context,
locks, capacities, conflicts, prerequisites, Study, Focus, Co-op, online
supervision, and half-semester rules continue to apply to the entire model.

This is a search restriction, not a scheduling rule. CP-SAT must produce the
candidate, and the existing full-model validator must approve it before any
diagnostic incumbent is replaced. `UNKNOWN` is inconclusive; only a proven
`INFEASIBLE` scope is reported as exhausted.

## Opportunity facts

The pure-engine `grade_guidance.py` module reports bounded, solver-neutral
facts for grades 9 through 12: student and source-decision counts, local
quality pressure, utilization pressure share, pressured delivery groups,
ordinary and special lock counts, and whether any source decisions are
available to search. These facts explain the experimental scope; they do not
authorize a move or claim an improvement is possible.

## Evidence and promotion boundary

Every grade experiment should use the same detached input and validated source
seed when comparing grades. Record input and source-seed fingerprints,
selected actual grade, opportunity facts, CP-SAT status, candidate and
validation times, changed source decisions/students, component deltas,
resource telemetry, and stopping reason. Do not mutate a canonical checkpoint.

Target-scale characterization is progressive: qualify a small multi-grade
fixture first, then a medium fixture, then only promising target-scale grade
probes, followed by a final full-pipeline check if warranted. This diagnostic
operator does not authorize adaptive allocation, change Objective Semantics
v2, or establish a production policy.

The current durable production-scale v1 artifact predates the grade fact and
originated from a fixture whose 1,400 students are all Grade 12. A temporary
in-memory Grade-12 screen can therefore exercise the operator's target-scale
plumbing, but it is not evidence about cross-grade opportunity. Cross-grade
target-scale conclusions require a current adapter-produced snapshot with
real mixed student grades and its own fingerprint; the canonical v1 artifact
must remain unchanged.
