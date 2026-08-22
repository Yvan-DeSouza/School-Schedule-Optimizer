# Student Schedule Quality Measurement

This document describes the measurement-only quality evaluator in
`scheduling_engine.student_assignment.quality`. It does not add constraints,
objectives, hints, or search behavior. CP-SAT remains the authority for the
recommendation.

The current payload version is `student_schedule_quality_v3`.

## Stage comparison

When a complete CP-SAT Stage 1 seed is available, the engine evaluates both
the validated seed and the final Stage 2 candidate with the same evaluator.
The compact reports are stored in `StudentAssignmentResultDTO.optimization_facts`
under `quality`. The pure evaluator can also return per-entity detail for
benchmarks and tests. `stage_1_vs_stage_2` reports improved, unchanged, and
worsened entity counts plus aggregate/mean changes and mean improvement or
worsening magnitudes where defined; it is descriptive and does not alter
optimization.

The evaluator uses nearest-rank percentiles for p75, p90, and p95. Empty
populations report `null` statistics rather than fabricating zeros.

When evaluation runs inside the solver, the aggregate `solver_aligned_penalty`
for each objective is read from the actual CP-SAT candidate. The evaluator also
keeps its independent per-entity reconstruction as `reconstructed_penalty` and
records any `reconstruction_delta`. This makes CP-SAT the authority while
turning adapter/evaluator drift into visible measurement evidence rather than
silently reporting a reconstructed value as the solver's value.

## Solver-aligned metrics

### Request fulfillment

The report preserves the existing fulfillment tiers: mandatory requests,
nominated priority primary requests, other primary requests, and approved
backups. It also reports requested and fulfilled special commitments. These
are descriptive copies of the existing lexicographic fulfillment facts; the
evaluator does not add a new objective or decide whether a request is
acceptable.

### Section-utilization balance

For every pair of sections in the same delivery group, the evaluator sums the
absolute enrollment-count difference. This is the same pairwise penalty used
by the solver. Online supervision sessions retain their engine-only negative
section identities for this calculation; they are not persisted instructional
sections.

The report also includes per-delivery-group section counts, pairwise penalty,
the distribution of fullest-minus-emptiest section ranges, average absolute
deviation from each group's mean enrollment, perfectly balanced-group counts,
and descriptive groups within one enrollment of balance. Those thresholded
counts are reporting facts only; they are not additional solver objectives.

### Student semester-load balance

For each non-Focus student, course credits are represented as
`round(credit_value * 2)` and the absolute Semester 1 minus Semester 2 total is
summed. Active fixed enrollments and two-credit Co-op commitments are included.
Study contributes no academic credit, and Focus students are excluded.

The report includes each applicable student's two loads and difference, plus
mean, median, p90, p95, maximum, and perfectly balanced counts.

### Soft course-sequence preferences

An opportunity is the same student/preference pair for which the solver
constructed a sequence variable. Satisfaction means the earlier course is in
Semester 1 and the later course is in Semester 2. The solver-aligned penalty
is the negative number of satisfied opportunities. Applicability and
unsatisfaction are reported separately.

When evaluating inside the engine, the exact constructed opportunity set is
passed to the evaluator. Standalone callers may omit it and receive a
candidate-derived applicability estimate. The report also separately counts
students with applicable and satisfied opportunities.

### Difficulty balance

For each non-Focus student, the evaluator sums
`round(effective_difficulty * credit_value)` by semester and minimizes the
absolute difference. This includes normal and online academic courses,
fixed enrollments, and the existing Study/Co-op contribution rules. A Study
or Co-op candidate's semester is derived from its occupied target-year
timeslot(s), never from a timeslot identifier. Focus is excluded.
Half-semester courses use the same effective contribution as the current
solver; the evaluator does not invent proportional weighting.

The report includes per-student semester difficulty loads and the full
distribution of absolute differences. Stage comparisons also include entity
counts and change magnitudes, including mean improvement and mean worsening
among affected students where those populations are non-empty.

During a rerun, the engine passes the resolved fixed enrollment and special
commitment context to the evaluator. Movable or replaced active facts are
therefore counted through the returned candidate rather than counted again as
fixed context. Schedule-preservation facts still use the original snapshot so
the legal move opportunity remains visible.

### Course-category diversity

The evaluator uses the catalog category relationship similarity score. Equal
categories have similarity 100; explicit relationship rows provide
cross-category similarity; missing categories and unspecified relationships
are neutral. For each applicable student/course pair and semester, shared
occupied halves contribute the same integer quantity as the solver:

```text
floor(similarity_score * shared_half_count / 2)
```

Focus students and category-neutral commitments such as Study and Co-op are
excluded. Online supervision is not an academic category. Online course
presence follows the current solver's physical supervision occupancy rule,
including online half-semester courses.

The report includes aggregate penalty, per-student penalty, pair contributions,
and distribution statistics. It must not be simplified to a count of distinct
category strings.

### Schedule preservation

This metric is applicable only when the input's schedule-preservation level is
not `none`. It counts legally movable active enrollments whose selected section
differs from the prior section. Locked, out-of-scope, and unmatched historical
context is not treated as an optional preservation opportunity.

The report includes the exact move penalty, movable/preserved/moved counts,
preservation rate, affected students, and moves per affected student.

## Interpreting distributions

Median, p90, p95, maximum, and improved/worsened counts are descriptive
outcome evidence. They are not additional CP-SAT objectives. A combined
same-priority solver tier may improve one individual objective while worsening
another; the compact report exposes that trade-off instead of hiding it in a
single aggregate vector.

The evaluator does not assign a universal quality score. A lower aggregate
penalty is mathematical evidence of improvement under the existing contract,
while the distribution facts explain which students, sections, or preference
opportunities changed.

## Benchmark methodology

Use the deterministic engine fixtures first, then the realistic validation
fixture, and finally the isolated production-scale scenario when a full run is
justified. Record Stage 1 and Stage 2 vectors, individual metrics, pass facts,
completeness, unmet requests, runtime, and process-isolation conditions.
Parallel CP-SAT runs need not produce identical assignments. Comparisons must
use hard validity, completeness, objective vectors, and quality distributions.

The pre-semantic-correction production-scale observations (which used the
older `student_schedule_quality_v2` difficulty interpretation) are historical
only and must not be used as the current objective baseline. In particular,
the earlier difficulty and substantive-tier values were `36,073` and `65,273`.

The first post-correction isolated scale run produced this authoritative
comparison:

```text
Stage 1: [-10945, 0, 0, 0, 65173, 5669086063387]
Stage 2: [-10945, 0, 0, 0, 65173, 5669086063311]
```

The run produced a complete validated seed, completed student assignment
with 10,635 assignments and zero unmet requests, and fulfilled all 310
special commitments. Section utilization, semester balance, difficulty, and
category-diversity metrics were unchanged between stages at `6,875`, `175`,
`35,973`, and `22,150`; only the opaque final tie-break improved. All
reconstruction deltas were zero. The student-assignment service time was
`1,863.978` seconds and the review layer took `352.800` seconds. These are
environment-specific observations, not contractual performance limits. The
post-approval controlled-rerun portion of that release-validation child was
interrupted after the initial schedule completed, so this is not yet evidence
of a complete repeated rerun validation.

## Diagnostic substantive-tier discovery

The engine also contains a diagnostic-only probe for the existing substantive
same-priority tier. It reuses the full production model and the validated
Stage 1 source assignment, preserves every higher-priority objective value,
and asks a bounded satisfiability question. It is not part of the ordinary
student-assignment workflow and does not change counselor importance labels,
objective definitions, or the Stage 2 result.

On the representative 1,400-student input, the Stage 1 substantive value was
`65,173`. A probe requiring a value at most `65,172` found complete,
hard-valid candidates below that bound. A source-decision neighborhood probe
restricted to at most two changed decisions repeatedly found a candidate at
`65,165` on the current environment (earlier parallel trials found values in
the `65,163`--`65,165` range). The current representative candidate changed
two ordinary course source decisions for one student: two same-semester
courses exchanged their two available blocks, moving sections `75`/`91` to
`73`/`93`. The affected section loads changed by `+1`, `-1`, `-1`, and `+1`
respectively. No auxiliary-variable identity is used as the candidate's
meaningful identity.

The candidate changed only the section-utilization component (`6,875` to
`6,867`); semester load, difficulty, and category diversity were unchanged.
It was complete, fulfilled all required source groups, and passed validation
against the unchanged full model. This is evidence that the current Stage 1
value is not established as globally optimal and that a very local better
schedule exists. It remains diagnostic evidence only: no production Stage 2
objective or search rule was changed as a result.

The diagnostic path can replay that validated candidate through the existing
lexicographic Stage 2 process. On the target-scale replay, the alternate
candidate was validated and entered Stage 2 with substantive value `65,165`.
When the bounded replay exhausted its short shared budget before obtaining a
new solver candidate, the complete `65,165` incumbent was returned unchanged;
it was not downgraded to the original `65,173` seed. Medium-fixture traces
also show the same protection when an equal-current-tier solver result has
worse future quality. These traces diagnose discovery and incumbent handling;
they do not by themselves justify changing the production Stage 2 search.

The same-priority component values in that run were:

| Component | Stage 1 value | Share of aggregate |
| --- | ---: | ---: |
| Section utilization | 6,875 | 10.5% |
| Semester-load balance | 175 | 0.3% |
| Difficulty balance | 35,973 | 55.2% |
| Category diversity | 22,150 | 34.0% |

These raw values are reported to expose scale and trade-offs, not to imply
that the solver should normalize or reweight equal-priority counselor
objectives. Any such change would be a separate product/objective-design
decision. The probe also reports source-decision deltas and objective-pass
hint provenance so future search changes can be evaluated against meaningful
assignment decisions rather than opaque auxiliary variables.

## Diagnostic local-bootstrap evidence

The diagnostic harness can reserve part of the existing Stage 2 budget for a
radius-limited substantive probe, validate the resulting CP-SAT candidate
against the unchanged full model, and pass the candidate to the ordinary
lexicographic optimizer. Bootstrap and validation time are deducted from the
same shared Stage 2 budget; the budget is not extended. The ordinary
`solve_student_assignment` entry point does not enable this path.

On the target-scale final-staffing input, a radius-two bootstrap found the
`65,165` candidate in approximately `126.6` seconds using an `1,800`-second
total Stage 2 budget. The candidate validated successfully and the remaining
optimization returned a complete result with `10,635` assignments, zero unmet
required requests, and all `310` special commitments fulfilled. The final
substantive components were `6,867`, `175`, `35,973`, and `22,150`.

This is not yet enabled for production scheduling. Three medium-fixture
baseline trials produced substantive values `1,634`, `1,634`, and `1,618`; the
three short-bootstrap trials produced `1,658`, `1,622`, and `1,624`. That
variation does not establish a consistent medium-scale improvement, so the
bootstrap remains an offline diagnostic until a repeatable shared-budget
benefit is demonstrated. A target-scale success alone is not sufficient to
change ordinary counselor-facing scheduling behavior.

## Adaptive incumbent-safe bootstrap evidence

The diagnostic path also supports an adaptive policy. It starts with a strict
radius-two improvement query around the current validated incumbent, restarts
at the smallest radius after an adopted improvement, and may try one larger
radius only when the smaller neighborhood does not improve. Each probe and its
full-model validation consume the same Stage 2 budget; the policy never fixes
the candidate or bypasses CP-SAT.

The optional retention rule now compares the complete existing lexicographic
objective vector. It can retain a previously validated complete candidate when
a later FEASIBLE result is equal or worse under the already-defined ordering.
The ordinary solver entry point does not enable this diagnostic option.

On the reusable 120-student medium fixture, three ordinary diagnostic runs
returned substantive values `1,616`, `1,634`, and `1,700`. Three adaptive runs
with two- and four-decision neighborhoods and short five-second probe slices
returned `1,694`, `1,646`, and `1,650`; none adopted a bootstrap candidate.
The adaptive policy therefore did not outperform ordinary Stage 2 repeatably.
A longer medium diagnostic also failed to find a neighborhood candidate and
consumed substantially more wall time than its requested probe slice in one
parallel trial, so the adaptive path is not production-safe yet.

An earlier exact target-scale adaptive replay was inconclusive because Stage 1
did not produce a validated seed in that isolated trial. The post-hardening
replay below did produce and validate the seed, so the earlier result is now
classified as search variation or replay-state variation rather than evidence
of infeasibility. The adaptive probes still did not find a substantive
improvement and remain diagnostic-only because the medium repeatability gate
is unmet.

The current conclusion is to preserve the adaptive and retention diagnostics,
keep ordinary Stage 2 unchanged, and require a reliable bounded-time policy
before changing counselor-facing scheduling behavior.

## Diagnostic timing and replay facts

Student-assignment results now expose additive timing facts for the Stage 1
seed and validation solves, the full model construction, each Stage 2 pass,
and diagnostic local-bootstrap probes. Each CP-SAT solve reports both the
solver's `WallTime()` and externally measured `Solve()` wall time. Diagnostic
bootstrap and validation operations share one monotonic deadline, so a child
validation solve does not silently receive the complete experiment allowance
again. These facts are observational and do not change ordinary scheduling
semantics.

Results also expose a diagnostic semantic input fingerprint. It canonicalizes
the detached DTO's opaque database identifiers while retaining scheduling
facts such as requests, sections, timeslots, fixed context, special
commitments, locks, and objective settings. It is suitable for comparing
equivalent fixture builds; immutable run snapshots and persisted identifiers
remain authoritative for application behavior.

The post-hardening target replay used the unchanged mixed final-staffing input
(`1,400` students, `10,760` requests, `304` normal sections, and `13`
online-supervision sessions). Stage 1 again produced and validated a complete
seed. With a `300`-second Stage 2 horizon and two `60`-second local probes,
the diagnostic result remained complete with `10,635` assignments, zero unmet
requests, and all `310` special commitments fulfilled. Both probes returned
`UNKNOWN` without an adopted candidate. Stage 2 measured `306.09` seconds
externally; the probe operation times were `63.63` and `67.28` seconds, while
CP-SAT reported `55.95` and `60.82` seconds. This is why diagnostic reports
distinguish solver time from model preparation, hinting, extraction, and
native cleanup overhead.
