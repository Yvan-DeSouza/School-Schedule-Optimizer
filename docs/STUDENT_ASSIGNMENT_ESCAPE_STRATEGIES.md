# Student Assignment Escape Strategies

This document owns the conceptual taxonomy and authority boundaries for
diagnostic escape operators. Empirical results belong in the operator
characterization document. It defines the diagnostic search vocabulary for leaving a mature
student-assignment basin. It does not enable a production optimizer.

## Strategy boundaries

- **Local descent (`r2`)** searches a small source-decision neighborhood.
- **Targeted student repair (`R4/S1`, `R8/S1`, `R4/S2`, `R8/S2`)** searches a
  bounded scope selected from student-local pressure.
- **Utilization-cluster repair (`R16/R32/R64` families)** searches a bounded
  multi-student scope selected from global section-utilization pressure.
- **Grade-bounded escape** leaves students in one actual grade unrestricted
  while freezing source decisions owned by all other grades.
- **Full-school unrestricted escape** remains deferred.

An escape is a diagnostic search scope, not permission to move records. CP-SAT
produces the candidate, and the unchanged full-model validator decides whether
it is complete, hard-valid, and eligible for adoption.

## Status semantics

`UNKNOWN` means the bounded search did not establish a result. It is not a
local-optimum proof. `INFEASIBLE` proves only that the exact modeled scope and
constraints have no solution under the query. Empirical stagnation means that
the measured attempts produced no adopted improvement; it must not be written
as mathematical exhaustion.

After a validated escape, the preferred diagnostic sequence is to return to a
faster local operator and measure the downstream basin. Direct escape gain and
downstream local gain must be reported separately and never double-counted.

## Authority and scope

Every operator uses the full hard model, including capacities, conflicts,
prerequisites, locks, special commitments, fixed context, and fulfillment.
Grade-bounded scope uses immutable actual student-grade facts. Students outside
the selected grade remain frozen, but their assignments continue to occupy
capacity and participate in every full-model constraint. A grade operator does
not filter the model to courses labeled with that grade.

The operators, session records, opportunity facts, and characterization records
are pure-engine diagnostics. They do not persist operational schedule changes,
change Objective Semantics v2, or bypass approval.

## Evidence requirements

Before production adaptive integration, each retained family needs matched records for
success rate, first-improvement time, total and role-specific gain/minute,
attempt-level marginal gains, stagnation/unknown behavior, validation cost,
memory, and downstream effect. Input and source-seed fingerprints must match
for direct comparisons. v1 and v2 records must remain separate.

The current evidence catalog and explicit NO-GO decision are maintained in
[`STUDENT_ASSIGNMENT_OPERATOR_CHARACTERIZATION.md`](STUDENT_ASSIGNMENT_OPERATOR_CHARACTERIZATION.md).
