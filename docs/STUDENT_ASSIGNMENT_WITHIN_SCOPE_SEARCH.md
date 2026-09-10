# Student Assignment Within-Scope Search

This document owns the research question that begins **after** an operator and
its exact student scope have been chosen. It does not define schedule quality,
choose students, change solver hints, or authorize candidates.

The distinction is:

- target selection asks, “Which four students should R16 inspect?”;
- within-scope search asks, “Once those students are fixed, what should CP-SAT
  do inside that neighborhood?”

The frozen study described below is prepared but has not been executed. None
of these new modes is wired into production.

## Why this question is worth isolating

In the 63 complete, full-model-validated transitions from the interrupted IA
trajectory, nine three-to-four-student moves produced 426 of 1,098 observed
prefix points. Eight moves changing at least ten source decisions produced 414
points, and every observed gain of at least 60 points met both breadth
thresholds used below. That is useful evidence that coordinated rearrangements
can matter, not proof that breadth causes quality: broad IA attempts 32 and 63
each gained only six points.

The broad high-gain IA moves improved utilization by about 438 points while
losing about 18 each in category and difficulty; broad low-gain moves improved
utilization by about 48 while losing about 24 in category. At the matched
165-minute prefix, IA had roughly 66 more utilization improvement than TOP,
while TOP had roughly 144 more category-plus-difficulty improvement and a
78-point total advantage. These component tradeoffs motivate testing the exact
full-v2 objective inside a fixed scope instead of optimizing utilization or
assuming broad movement is sufficient.

The earlier IA run also exposed an operational constraint: duplicated attempt
and phase-event payloads were about 1.52 and 1.54 GiB, process-tree USS grew by
about 48.6 MiB per attempt, and peak memory approached 4 GiB. The new grid
therefore has a compact, externally streamed telemetry boundary.

## Shared R16/S4 contract

All four research modes start from one immutable, fully validated source
schedule and one exact four-student scope. Source decisions outside that scope
are fixed. Within the scope, no more than four students and no more than 16
source decisions may change. Completion, mandatory requests, primary/backup
tiers, all ordinary hard constraints, and all higher objective tiers retain
their existing model semantics.

Every mode uses seed 101, eight search workers, the current authoritative
incumbent-derived hints, and one cumulative 300-second CP-SAT search budget.
The final reportable candidate—if any—then receives an independent 180-second,
one-worker full-model validation. It is authoritative only when validation is
complete, required requests remain fully met, the validated semantic
fingerprint is the candidate fingerprint, and its Objective Semantics v2 value
is strictly lower than the source value.

The exact definitions of better schedule quality are owned by
[Objective Semantics](STUDENT_ASSIGNMENT_OBJECTIVE_SEMANTICS.md). Target choice
is owned by [Target Selection](STUDENT_ASSIGNMENT_TARGET_SELECTION.md), hint
construction by [Hint Strategy](STUDENT_ASSIGNMENT_HINT_STRATEGY.md), and
candidate authority by [Validation](STUDENT_ASSIGNMENT_VALIDATION.md). The
adaptive allocator remains separately documented in
[Adaptive Search](STUDENT_ASSIGNMENT_ADAPTIVE_SEARCH.md).

## 1. Current first-qualifying control

The current R16/S4 probe is a strict-threshold satisfiability query. It asks
CP-SAT for any complete schedule inside the fixed neighborhood whose exact v2
substantive value is at least one integer point lower than the source. It does
not give CP-SAT a substantive minimization objective. Search stops when the
single `Solve()` call returns its qualifying candidate, or reports the native
non-candidate status.

In counselor language, this is “find me a legal improvement,” not “use the
remaining time to find the best improvement you can.” It is the historical
control and its default behavior is unchanged.

## 2. Minimum-coordination search

This research-only satisfiability mode keeps the control’s strict-improvement
query and adds two lower bounds:

```text
3 <= changed students <= 4
10 <= changed source decisions <= 16
```

It removes the easy exit through a one-student or few-decision repair. It does
not maximize breadth and breadth is not added to schedule quality. A broad
move can still be poor: historical IA attempts 32 and 63 met broad movement
conditions but gained only six v2 points. This treatment tests whether forcing
coordination changes the candidates the solver finds; it does not assume that
coordination is beneficial.

## 3. Iterative strict-bound refinement

This mode uses an outer research loop rather than a CP-SAT objective:

1. Clone the unchanged full model and add the same fixed scope, upper bounds,
   and strict source-improvement bound as the control.
2. Invoke a fresh `CpSolver` using the remaining shared search budget.
3. If it returns a complete candidate with value `V`, retain that candidate
   and add `target_expression <= V - 1` to the same probe model.
4. Invoke another fresh solver with only the budget that remains.
5. Repeat, retaining the lowest complete candidate seen.
6. Validate only that final retained candidate.

All native/external `Solve()` walls draw from one 300-second allowance; the
treatment never receives 300 seconds per round. Solver-construction and model
setup are reported separately. Each round uses the original validated
incumbent-derived hints. Candidate-derived hints are deliberately forbidden,
because introducing them would combine refinement with a hint-policy change.

If a tighter round is `UNKNOWN`, the best earlier complete candidate remains
reportable; `UNKNOWN` is not a proof that the tighter bound is infeasible. If
the tighter bound is proven `INFEASIBLE`, the retained candidate is optimal
for the bounded probe model. Intermediate candidates are diagnostic only and
are never adopted.

## 4. Direct exact-v2 optimization

This mode gives CP-SAT the exact substantive expression already represented by
the v2 model:

```python
probe_model.Minimize(target_expression)
```

The expression is the sum of the existing five integer-normalized,
counselor-weighted component terms after higher lexicographic tiers are fixed:

- course-category diversity;
- course-sequence preferences;
- difficulty balance;
- section-utilization balance; and
- student semester-load balance.

The model and evaluator both use the same input-derived denominators, integer
floor normalization, and counselor importance scores. The strict source-value
bound is retained, so direct optimization cannot report a non-improvement.
At timeout, the best complete incumbent is retained and its objective, best
bound, and meaningful absolute/relative gaps are recorded. A feasible status
does not mean optimal. With no solution callback in the frozen study, the time
of the solver’s first internal incumbent and final internal update are
explicitly unavailable; callback overhead is not introduced merely for
telemetry.

Direct optimization is eligible because deterministic parity tests prove that
the model expression equals `evaluate_student_assignment_quality(...)
.weighted_substantive_value` for both seed and candidate schedules, including
all five components, denominators, floor normalization, and weights. Any future
semantic change must re-pass that gate. A surrogate such as raw utilization is
not permitted.

## Hybrid assessment

A short first-qualifying phase followed by direct exact-v2 minimization is
mathematically distinct only as a search-allocation or warm-start strategy. To
be useful, it would need to transfer a candidate bound and perhaps a candidate
hint into the second phase under one shared budget. Candidate-derived hints
would be a new hint treatment; without them or persistent native solver state,
the warmup may merely spend time that direct branch-and-bound could use.

Direct optimization followed by iterative bounds, or progressive bounds while
also minimizing the same expression, is redundant with CP-SAT’s own
branch-and-bound and obscures causal interpretation. Consequently no hybrid is
included in the initial four-arm screen. If direct/iterative evidence later
supports it, a separately preregistered later-stage hybrid may use a short
feasibility warmup followed by direct optimization, with the transfer and
shared-budget rules tested as their own factor.

## Budget, retention, and status semantics

The 300 seconds are cumulative CP-SAT `Solve()` time for a cell. Model build,
scope construction, extraction, compact telemetry, and the independent
validation allowance are separately measured and are contained by a
supervisor hard wall. Iterative rounds share that allowance. Control,
minimum-coordination, and direct optimization each make one native call.

`FEASIBLE` means a complete candidate was returned without an optimality proof.
`OPTIMAL` means the active query was solved to proof. `INFEASIBLE` is a proof
for that exact source/scope/query. `UNKNOWN` means neither a reportable
candidate nor a proof was returned before search ended. It must never be
relabeled as infeasibility.

Candidate retention never grants authority. The incumbent remains unchanged
unless the final candidate crosses the unchanged full-model validation and
strict-v2-improvement boundary.

## Frozen study and promotion gate

The machine contract is
[`research/contracts/r16_fixed_scope_search_semantics_v1.json`](../research/contracts/r16_fixed_scope_search_semantics_v1.json).
It fixes six source/scope states, four treatments, three independent
eight-worker repeats, alternating deterministic order, telemetry, resource
guards, and analysis rules: 72 cells total. Each cell resets to its source;
there is no dynamic adoption across cells.

A treatment advances only when its paired three-repeat median gain exceeds
control by at least 12 points on at least two scopes, loses by at least 12 on
no more than one scope, and has at least two validated repeats on at least five
scopes. Minimum coordination also fails if material non-utilization losses or
broad-low failures cross the frozen limits. Direct and iterative use the same
paired criterion and the winner must have no worse validation reliability.

Advancement authorizes only another research decision. It does not alter
production, target selection, hints, Objective Semantics, or validation
authority.

## Compact telemetry boundary

Each cell stores source/scope identity, treatment, solver and candidate facts,
exact changed semantic requests/destinations, five component deltas,
validation, and a resource summary. Full 1,400-student targeting populations
are forbidden by the initial contract. Five-second process-tree resource
samples are streamed once by the external supervisor and are not duplicated
inside cell results. A valid promoted result must remain below 1 MiB; candidate
source decisions are stored once as a compressed side artifact for validation
auditability.
