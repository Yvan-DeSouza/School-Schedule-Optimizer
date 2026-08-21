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
`65,173`. A probe requiring a value at most `65,172` found a complete,
hard-valid candidate at `65,171`; a second probe restricted to at most two
changed source decisions found `65,163`. The latter changed only the section
utilization component (`6,875` to `6,865`); semester load, difficulty, and
category diversity were unchanged. This is evidence that the current Stage 1
value is not globally established as optimal and that a very local better
schedule exists. It is diagnostic evidence only: no production Stage 2
objective or search rule was changed as a result.

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
