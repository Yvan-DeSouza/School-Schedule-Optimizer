# Student Assignment Grade-Bounded Escape (Diagnostic)

This document owns the grade-bounded diagnostic operator's scope semantics and
promotion boundary. It does not enable grade-bounded search in production.

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
operator does not authorize production adaptive allocation, change Objective
Semantics v2, or establish a production policy. The diagnostic adaptive
allocator may select this operator for offline calibration, but CP-SAT and
full-model validation remain the only candidate authority.

The current durable production-scale v1 artifact predates the grade fact and
originated from a fixture whose 1,400 students are all Grade 12. It remains
unchanged. A separate durable synthetic mixed-grade-v2 artifact records a
deterministic 350/350/350/350 Grade 9–12 profile with its own input and
source-seed fingerprints. It provides reproducible diagnostic cross-grade
plumbing and capability evidence, but it is not an adapter-produced school
dataset and must not be presented as real cross-grade production evidence.
Stronger production conclusions still require a current adapter-produced
snapshot with real mixed student grades.

## Current target-scale diagnostic evidence

The current mixed-grade-v2 target-shaped artifact was used for bounded
one-attempt probes with the same complete `37,596` source seed and eight
workers. Grades 9, 10, 11, and 12 each produced a complete candidate that
passed the unchanged full-model validator. The resulting values were
`37,590`, `37,488`, `37,590`, and `37,590`, respectively. Grade 10's move
improved difficulty by `38` and category diversity by `100`; the other first
probes improved utilization by `4`.

Clean repeats reproduced the Grade-9, Grade-10, and Grade-12 value/component
outcomes. Grade 11 reproduced its value and changed-student count but not the
exact source-decision set, so its repeatability is directional. A validated
Grade-10 branch was returned to ordinary R2; that bounded R2 attempt returned
`UNKNOWN` without a new adopted candidate and retained the complete branch.
This does not prove a grade branch is locally optimal. All runs preserved
`10,635` assignments, zero unmet required requests, and `310` special
commitments. These results are characterization evidence only and do not
authorize adaptive allocation or production grade selection.
