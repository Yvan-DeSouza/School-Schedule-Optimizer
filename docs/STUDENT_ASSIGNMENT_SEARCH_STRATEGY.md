# Student Assignment Search Strategy

## Current status

Objective Semantics v2 is the current quality contract for the diagnostic
student-assignment runs described here. It uses one canonical counselor
importance score from 0 through 10 and input-derived normalization for the
counselor-controlled soft metrics. The v1 R2/R4/R8 search study remains
historical evidence and is not reopened by the targeted-repair diagnostics.

## Student-targeted repair guidance

`scheduling_engine/student_assignment/search_guidance.py` ranks students from
the quality facts already produced for a complete candidate. It attributes only
student-local metrics:

- student semester-load balance;
- difficulty balance;
- category diversity; and
- applicable soft course-sequence opportunities.

Section utilization is a global pairwise section-load metric and is therefore
not assigned to an individual student. The ranking reports the same v2
normalized, counselor-weighted local penalty used by the aggregate objective.
It also reports a cheap opportunity signal: the number of non-zero local
penalty components plus unsatisfied applicable sequence opportunities. This is
guidance, not proof of mobility or improvement.

The ranking does not call CP-SAT, create assignments, modify the production
objective, or authorize a candidate. It is intentionally deterministic, with
student ID as the final tie-break.

`reconcile_student_quality_pressure` compares the summed student-local
weighted pressure with the aggregate v2 local components. Section utilization
is reported as an excluded global component, and independent integer rounding
is exposed as a delta rather than silently treated as exact equality.

## Targeted S1/S2 diagnostics

The targeted repair entry points in
`scheduling_engine/student_assignment/core.py` are diagnostic-only wrappers
around the existing substantive soft-tier probe:

- S1 selects exactly one student;
- S2 selects exactly two students;
- source decisions owned by every other student are fixed to the supplied
  validated incumbent;
- the unchanged full model, higher-priority fulfillment values, and strict
  aggregate v2 substantive-improvement requirement remain in force; and
- every returned candidate must pass the existing full-model validation before
  it can replace the diagnostic incumbent.

The target list is a search restriction, not a counselor rule and not an
assignment recommendation. No adaptive controller, grade-bounded escape, or
global unrestricted operator is implemented by this module.

The ordinary two-stage path also retains a complete validated Stage 1 seed
when a later bounded optimization pass returns a weaker candidate or becomes
inconclusive. This protects completeness without claiming that the lower
objective tier was optimized.

## Soft sequence versus hard prerequisite

`CoursePrerequisite` remains a hard same-year sequencing rule. When it applies,
the prerequisite must be in Semester 1 and the dependent course in Semester 2;
soft importance cannot reverse it. `CourseSequencePreference` is separate
non-binding evidence: when both courses are applicable, Semester 1 for the
earlier course and Semester 2 for the later course is rewarded by the existing
v2 normalized objective, but a legal reverse order remains possible when the
other hard constraints and higher-priority objectives require it.

Applicability is based on both courses being present in the student's target-
year request/fixed context, not on the preferred orientation already being
available. Thus a student whose only legal order is the reverse still produces
one unsatisfied soft opportunity rather than silently removing the opportunity
from the denominator.

Sequence edges are part of the semantic student-assignment input fingerprint,
so changing the configured directed relation causes immutable-run drift
detection rather than silently changing the meaning of an existing run.

## Authority and future work

CP-SAT remains the sole assignment authority. Targeted guidance may choose
which diagnostic neighborhood to inspect, but it may never authorize a result.
The full-model validator and existing approval/snapshot rules remain the
durable boundaries.

The target-scale evidence obtained so far is diagnostic, not a production
policy. On the durable 1,400-student input, using the unchanged validated v1
source seed under v2 semantics, the highest-pressure S1 probe (one student,
radius 2) found a complete validated candidate at raw section-utilization
penalty `6,871`, down from `6,875`. A matched S2 probe (the two highest-pressure
students, radius 4) found a complete validated candidate at `6,869`. Both
retained `10,635` assignments, zero unmet requests, and all `310` special
commitments. The isolated calls took approximately `157.7` seconds for S1 and
`148.4` seconds for S2, including model construction, seed validation, CP-SAT,
and finalization. These are target-scale capability results, not evidence that
the ranking is globally optimal or that an adaptive controller should select
these students in production.

The next separately scoped research increment may study adaptive allocation
among targeted repair and retained local operators. Grade-bounded unrestricted
escape and full-school global escape remain deferred. The targeted diagnostic
does not change the production objective, counselor policy, approval behavior,
or operator allocation.
