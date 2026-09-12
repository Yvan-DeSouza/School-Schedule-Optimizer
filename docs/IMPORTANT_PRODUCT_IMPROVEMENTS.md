# Important Product Improvements

## Status

This document records important product capabilities that are not necessarily
the next implementation tasks, but must not be forgotten before the scheduling
system is treated as a mature real-school product.

These are not implemented features unless another canonical document explicitly
says otherwise.

The common theme is that a counselor must never be left with either:

- false confidence that a section placement will work; or
- a bare `INFEASIBLE` result with no practical explanation or path forward.

The system already has important foundations for these problems, but the full
product workflows remain future work.

---

## 1. Whole-Cohort Student Feasibility Certification

### The problem

Section placement already performs structural feasibility checks.

These checks are valuable. They can catch problems such as insufficient
aggregate timing capacity and can prove anonymous staffing feasibility.

However, they do not by themselves provide a complete constructive proof that
every student in the school can be assigned simultaneously to sections.

Likewise, checking every student individually proves only that each student has
some valid schedule when considered alone.

It does not prove that all of those schedules can coexist while sharing section
capacity and other global resources.

### Required future capability

The product should support a whole-cohort hard-feasibility certification after
candidate section placement has been constructed.

This should reuse the actual Student Assignment Stage 1 hard model.

If Stage 1 produces a complete assignment and the independent validator accepts
it, the system can truthfully say:

> A complete assignment exists for every included student under this exact
> section placement and the currently modeled hard constraints.

That is a constructive certificate, not merely a structural heuristic.

### Counselor experience

The product should clearly distinguish states such as:

- structural feasibility checks passed;
- individual student preflight passed;
- whole-cohort feasibility certified;
- whole-cohort infeasibility proven;
- whole-cohort certification unresolved within the configured runtime.

`UNKNOWN` must never be presented as infeasible.

Whether this whole-cohort certificate is optional, recommended, or required
before section-placement approval is a later product/governance decision.

A certification witness is evidence that the placement can work. It does not
need to become the final operational student allocation.

### Why this matters

A counselor should be able to spend additional compute time before approving a
placement if they want stronger assurance that they are not approving a section
layout that will fail later during student assignment.

The project already has the Stage 1 solver capability. The missing work is the
formal product workflow, persistence, review state, and counselor-facing
meaning of the certificate.

---

## 2. Individually Impossible Students Must Not Block Everybody Else

### The problem

The complete student-assignment problem may contain a student whose requested
program is mathematically impossible under the accepted section topology.

For example, imagine:

- 1,399 students have valid programs;
- 1 student has no possible assignment of all mandatory requests.

The current whole-cohort hard model can then become infeasible even though the
vast majority of the school is schedulable.

A real scheduling product must not respond by effectively making all 1,400
students unavailable to the counselor simply because one program requires
human intervention.

### Required future capability

The system needs a deliberate infeasibility-triage workflow.

When appropriate, it should be able to run isolated student feasibility checks
against the accepted candidate domain and distinguish:

- an individually impossible student program;
- all students individually feasible but the cohort globally impossible;
- an unresolved case where the configured diagnostic work cannot yet prove the
  cause.

If one or a small number of students are individually impossible, they must be
surfaced prominently to the counselor.

They must never be silently deleted, ignored, or have mandatory requests
changed automatically.

### Counselor experience

The system should help the counselor understand that a particular student
requires intervention.

Possible counselor decisions may eventually include:

- discussing a request change with the student;
- using an approved alternate;
- investigating a section-placement change;
- temporarily leaving the student unresolved;
- reviewing solver-generated repair options.

The solver may recommend options.

The solver must not make important academic/program decisions on the
counselor's behalf.

The product may eventually allow scheduling to continue for the feasible
population while explicitly carrying unresolved students forward.

If that is implemented, a partial result must NEVER be presented as a complete
school schedule.

The number and identities of unresolved students must remain obvious in review,
approval, audit, and downstream workflows.

### Required architecture work

This future capability requires deliberate design of:

- run completeness semantics;
- statuses;
- approval rules;
- unresolved-student persistence;
- backup handling;
- audit provenance;
- counselor intervention workflows;
- interaction with placement repair.

This must not be added as an informal exception inside the solver.

---

## 3. Programmatic Explanation of Whole-Cohort Infeasibility

### The problem

A production scheduling system cannot stop at:

> CP-SAT returned INFEASIBLE.

That answer may be mathematically correct, but it is not operationally useful to
a counselor.

The current v2.2 stress-benchmark investigation demonstrates why this matters.

For that exact detached benchmark:

- all 1,400 students pass isolated feasibility;
- zero students are individually infeasible;
- zero students are unresolved by the isolated preflight;
- the complete Stage 1 hard model nevertheless proves the cohort infeasible.

Therefore an important class of scheduling failures exists only because students
interact through shared capacities, timing choices, and other global
constraints.

Preserved research incidents, their witnesses, and exact reproduction commands
are maintained in [`IMPOSSIBLE_SCHEDULE_DIAGNOSIS.md`](IMPOSSIBLE_SCHEDULE_DIAGNOSIS.md).
This document continues to own the future product requirement for counselor-
facing diagnosis and resolution workflows.

Without a diagnostic system, a counselor would know that the schedule is
impossible but would have no practical explanation of what must change.

### Required future capability

The system needs a programmatic infeasibility-diagnosis pipeline.

It should progressively analyze the hard model using increasingly detailed
mathematical checks and determine the earliest level at which feasibility
fails.

A likely diagnostic sequence is:

1. verify the authoritative solver status;
2. identify individually impossible students;
3. audit total demand against total eligible seats by course;
4. audit capacity by semester/block timing cell;
5. solve a capacitated request-to-section matching/flow problem without
   same-student collisions;
6. add student collision constraints;
7. add paired-half semantics;
8. add online supervision;
9. add Study, Co-op, and FOCUS;
10. add fixed context and locks;
11. add prerequisite/order constraints;
12. inspect any remaining hard-constraint families.

Where possible, the system should produce mathematical witnesses such as:

- a course with more demand than eligible seats;
- a set of requests whose reachable sections contain insufficient capacity;
- a timing-cell subset with a Hall-style deficiency;
- a specific special-program capacity bottleneck;
- a small set of locks or fixed commitments whose combination creates the
  contradiction.

The eventual explanation should be domain language, not CP-SAT internals.

For example:

> 87 students need one of these three timing cells for this group of required
> courses, but the sections in those cells provide only 79 compatible seats.

is useful.

> MODEL_INFEASIBLE

is not.

### Counselor experience

The product should explain:

- what resource or combination causes the problem;
- how many students are affected;
- which courses/sections/timing cells are involved;
- whether the problem is local to particular students or global;
- which upstream decisions could plausibly resolve it.

It may later offer reviewed what-if options such as:

- moving one section to another block;
- adding another section if staffing permits;
- changing the semester of a section;
- reviewing a particular lock;
- reviewing affected student requests.

These are recommendations only.

The solver must not automatically make counselor decisions in order to force
feasibility.

### Diagnostic evidence must be preserved

Negative scheduling cases are valuable test assets.

The current
`paul_desmarais_shaped_g9_12_stress_v2_2`
global-infeasibility case should not disappear when the main benchmark is later
repaired.

Its exact semantic input/fingerprint, or an equivalent frozen artifact, should
be preserved as a regression case because it demonstrates:

> every student is individually feasible, but the cohort is globally infeasible.

Where practical, also derive a smaller deterministic reproduction of the same
failure for fast diagnostic tests.

A separate future regression scenario should deliberately represent:

> one student is individually impossible while the remaining population is
> jointly schedulable.

These two cases test different failure modes and both are required.

---

## Relationship Between the Three Improvements

These capabilities answer different questions.

### Structural / placement checks

> Did the proposed placement violate a known structural feasibility condition?

These are fast safeguards and should remain.

### Individual student feasibility

> Can this student's complete program work if considered independently?

This is diagnostic evidence about individual programs.

### Whole-cohort certification

> Can every student be assigned simultaneously under all modeled hard
> constraints?

A complete validated Stage 1 seed is the constructive certificate.

### Infeasibility diagnosis

> If certification fails, why?

This is the missing explanation layer that turns mathematical infeasibility into
an actionable counselor workflow.

The long-term product should therefore behave conceptually as:

section placement
    -> structural feasibility safeguards
    -> isolated student feasibility preflight
    -> whole-cohort hard-feasibility certification
        -> certified feasible
        OR
        -> programmatic infeasibility diagnosis
            -> individually impossible student triage
            OR
            -> global contention explanation
            -> counselor-reviewed repair options

None of the diagnostic or repair workflows may silently weaken hard constraints,
drop mandatory requests, or make academic decisions for the counselor.

---

## Production-Readiness Principle

These capabilities do not all need to be implemented immediately.

They do need to be resolved before the product can reasonably claim to handle
real-school scheduling failures well.

A mature system must do more than generate schedules when everything goes well.

It must also explain, preserve, and help humans resolve the cases where a valid
complete schedule does not exist.
