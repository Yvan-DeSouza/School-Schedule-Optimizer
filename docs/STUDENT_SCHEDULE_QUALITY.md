# Student Schedule Quality Measurement

This document describes the measurement-only quality evaluator in
`scheduling_engine.student_assignment.quality`. It does not add constraints,
objectives, hints, or search behavior. CP-SAT remains the authority for the
recommendation.

The historical v1 payload version is `student_schedule_quality_v3`. Explicit
Objective Semantics v2 reports use `student_schedule_quality_v4` and add
canonical counselor scores plus input-derived normalized contributions. The
raw metrics remain present in both versions. The mathematical contract is
documented in [`STUDENT_ASSIGNMENT_OBJECTIVE_SEMANTICS.md`](STUDENT_ASSIGNMENT_OBJECTIVE_SEMANTICS.md).
Targeted diagnostic repair guidance and its authority boundaries are documented
in [`STUDENT_ASSIGNMENT_SEARCH_STRATEGY.md`](STUDENT_ASSIGNMENT_SEARCH_STRATEGY.md).

## Objective Semantics v2

Objective Semantics v2 is an explicit opt-in solver semantics version. It keeps
the existing fulfillment tiers, hard constraints, special commitments,
complete-incumbent behavior, approvals, and immutable snapshots unchanged.
After fulfillment, the five existing soft components are normalized
independently to a common bounded integer scale and multiplied by one canonical
counselor importance score from `0` through `10`. Existing labels are presets
over that same scale; they are not a second weighting system. Schedule
preservation remains a distinct rerun-only objective by explicit design.

The solver's raw and normalized component values, denominator facts, selected
semantics version, and importance scores are persisted in the existing result
and snapshot payloads. The quality evaluator remains diagnostic-only and keeps
CP-SAT authoritative. A v2 production-scale run establishes a new baseline;
the historical v1 values below are not silently reinterpreted under v2.

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

A subsequent clean full acceptance run completed the entire workflow, including
review, approval, and the controlled exact-lock, whole-schedule-lock, and
released-lock reruns. It used the same `1,400`-student final-staffing fixture,
completed student assignment in `1,855.224` seconds, completed review in
`208.943` seconds, and finished the full child process in `2,672.83` seconds.
The ordinary result was complete with `10,635` assignments, zero unmet
requests, all `310` special commitments fulfilled, and substantive tier
`65,173`; only the opaque tie-break improved. During this run, the student
assignment process reached a sampled peak of approximately `7.2 GB` working
set and later recycled down to approximately `3.6 GB`. The memory sample did
not show monotonic growth, but this remains an important laptop-capacity
constraint for multi-hour operation.

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

A later clean target-scale strict-improvement probe used the same
final-staffing input and asked CP-SAT to satisfy the existing higher-priority
values while requiring the substantive tier to be at most `65,172`. The probe
returned `OPTIMAL` in `113.780` seconds with a complete candidate at `65,171`,
after validating the Stage 1 seed. The candidate retained all `10,945`
required source decisions and changed `258` meaningful source decisions. Its
component values were section utilization `6,873`, semester balance `175`,
difficulty balance `35,973`, and category diversity `22,150`; therefore the
improvement was entirely a two-point section-utilization improvement. The
diagnostic model contained `110,917` variables and `175,894` constraints. This
is stronger evidence that a better substantive schedule exists, but it does
not authorize changing the ordinary Stage 2 search or objective semantics.

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

On the matched target-scale final-staffing input, four independent radius-two
replays using the same semantic Stage 1 seed all found and validated a
strictly better complete candidate within the existing `1,800`-second Stage 2
budget. The local candidate values were `65,165`, `65,165`, `65,163`, and
`65,165`; every replay returned `10,635` assignments, zero unmet required
requests, and all `310` special commitments fulfilled. The `65,163` replay
provides the clearest human-readable example: one student's course request
moved between two parallel sections, changing section loads by `-1` and `+1`
and improving only section utilization from `6,875` to `6,865`.

The ordinary optimizer did not consistently improve the local candidate's
substantive tier in the `1,800`-second replays. The final substantive values
therefore remained `65,165`, `65,165`, `65,163`, and `65,165` in those four
runs, while all retained candidates remained complete and hard-valid. A
separate `3,600`-second local-seeded replay began from `65,165` and ended at
`65,161` after Stage 2, showing that additional ordinary search can sometimes
improve a locally seeded candidate, but did not outperform the later adaptive
bootstrap result described below.

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
did not produce a validated seed in that isolated trial. The matched
post-hardening replay did produce and validate the seed. Three radius-two
iterations then returned `OPTIMAL` and were each validated and adopted,
progressing the substantive tier from `65,173` to `65,163`, then `65,143`,
then `65,135`. The bootstrap consumed approximately `365.7` seconds in
total, and the subsequent ordinary `1,800`-second Stage 2 run remained
complete at `65,135` with section utilization `6,857`, difficulty balance
`35,953`, semester balance `175`, and category diversity `22,150`.

This is strong target-scale evidence that a bounded local improvement policy
can expose better complete schedules that ordinary Stage 2 search did not
find from the original seed. It is still one adaptive target-scale trial, so
it does not by itself establish repeatability or authorize changing ordinary
production behavior.

The current conclusion is to preserve the adaptive and retention diagnostics,
keep ordinary Stage 2 unchanged, and require repeated target-scale adaptive
trials plus a documented operational policy before changing counselor-facing
scheduling behavior. The `7,200`-second horizon was not run: the regular
`3,600`-second local-seeded trial did not outperform the adaptive `65,135`
result, and the adaptive `1,800`-second run showed no need for more ordinary
search after reaching that value.

The engine now also exposes a separate diagnostic-only variable-neighborhood
entry point for bounded `R2/R4/R8` descent. It uses the same CP-SAT probe and
full-model validation as the adaptive bootstrap, returns to radius two after
each adopted improvement, records per-radius attempts and stopping reasons,
and distinguishes a proven infeasible neighborhood from an unresolved
`UNKNOWN` search.

The target-scale replay is now durable rather than dependent on disposable
temporary files. The authoritative synthetic benchmark is stored under
`scheduling_engine/benchmarks/student_assignment/production_scale_v1/` as a
manifest plus two deterministic gzip-compressed, versioned JSON artifacts:
`input.json.gz` contains the complete final-staffing
`StudentAssignmentInputDTO`, and `stage1_seed.json.gz` contains the semantic
Stage 1 source-decision seed. The manifest records the artifact hashes,
semantic fingerprints, counts, Stage 1 objective vector, substantive
components, and solver metadata. The artifacts are canonicalized by semantic
ordering and do not depend on database primary keys or Python pickle state.
Future replay work must verify the manifest and both artifact hashes before
solving; it must not regenerate the upstream placement/staffing workflow for
ordinary Stage 2 comparisons. If the benchmark must be regenerated in a
future deliberate refresh, the old directory must be retained as historical
evidence until the replacement has passed the clean-process replay gates.

The lost detached benchmark with input fingerprint
`14cfe8f...` and historical trajectory `65,173 -> 65,135` remains historical
evidence only; its raw objective values must not be compared with a different
input as though they were the same optimization problem. The current durable
benchmark has input fingerprint
`1c4843ac33fccabd76218c63d8818c94a0a8dd8ab2886e3f5718ca1cd9576a11`, Stage 1
seed fingerprint
`00889b7f4110dc19c6cdcb413b44fe77ab9598cb0fde25de6ea618ddb27325e7`, and
the same-scale counts of `1,400` students, `10,760` requests, `10,945`
required source-decision groups, `304` normal sections, `13` online
supervision sessions, and `310` fulfilled special commitments. Its Stage 1
baseline is substantive `65,173` with components `6,875` section
utilization, `175` semester balance, `35,973` difficulty, and `22,150`
category diversity.

Two independent clean-process replays loaded the exact artifacts, verified
their hashes and semantic fingerprints, validated the semantic Stage 1 seed
against the unchanged full model, and reproduced the exact Stage 1 objective
vector. The validation CP-SAT query itself is reported separately as
`UNKNOWN` because replay uses a short post-validation query; that status is
not used to reject the already validated seed.

The first full target-scale variable-neighborhood run used radii `2, 4, 8`,
the frozen Stage 1 seed, eight workers, one `1,800`-second bounded Stage 2
horizon, and bounded per-radius attempts. It completed with `10,635`
assignments, zero unmet requests, and all `310` special commitments fulfilled,
but did not adopt a substantive improvement: the final substantive tier
remained `65,173`. A compact follow-up using one attempt at each radius and a
`300`-second total horizon produced `UNKNOWN` for `R2`, `R4`, and `R8`; no
radius was proven infeasible and no candidate was validated or adopted. The
follow-up therefore provides no evidence that the new benchmark is locally
optimal; it establishes that the tested neighborhoods remain search-inconclusive
under those bounded probes. R8 is not promoted, and no production solver
behavior has changed.

The production-shaped 80-student medium fixture now has a stable semantic
fingerprint (`4ac904a01a2c43dcf00505969c539dc8bacf9606e01aad67da5ab171b373f66b`)
and is used for lower-cost curve experiments. With four CP-SAT workers and
Stage 1 limits of 10 seconds plus 5 seconds for validation, ordinary Stage 2
trials produced substantive values of `25,143`, `25,139`, `24,964`, and
`24,475` at 5-, 10-, 20-, and 40-second Stage 2 horizons respectively. Each
trial was complete with 510 assignments and zero unmet requests. This supports
the conclusion that additional search time can matter on a production-shaped
problem, while the values remain subject to parallel CP-SAT variation.

At a matched 20-second horizon, a same-Stage-1-seed ordinary/retention pair
also completed with zero unmet requests. The pair returned `25,072` and
`25,109` respectively; neither retention pass had to reject a candidate in
that trial. The difference is not evidence that retention improves quality,
because the subsequent CP-SAT searches are parallel and nondeterministic. The
full-vector retention rule remains mathematically safe and diagnostic-only;
additional repeated paired trials are required before any production decision.

## Current frozen-benchmark search-landscape study

The durable `production_scale_v1` benchmark was subsequently tested without
rerunning placement or named-teacher assignment. Every probe loaded the same
verified input fingerprint
`1c4843ac33fccabd76218c63d8818c94a0a8dd8ab2886e3f5718ca1cd9576a11` and the
same validated Stage 1 source-decision seed
`00889b7f4110dc19c6cdcb413b44fe77ab9598cb0fde25de6ea618ddb27325e7`.
The Stage 1 substantive baseline was `65,173`.

The first unrestricted strict-improvement probe asked whether any complete
schedule could satisfy `substantive_soft_tier <= 65,172`. With eight workers
and a 600-second diagnostic limit, CP-SAT returned a complete candidate at
`65,171`. The candidate changed `258` meaningful source decisions, affected
`191` students and `46` sections, and improved only section utilization from
`6,875` to `6,873`. The solver-reported search time was `301.08` seconds and
the probe operation time was `330.47` seconds. The clone contained `110,917`
variables and `175,894` constraints.

The probe is a satisfiability query, not a minimization query. CP-SAT reports
`OPTIMAL` when it proves the threshold-constrained satisfiability model has a
solution; that status does not prove that `65,171` is the global minimum.

Matched radius experiments then used the same Stage 1 seed and eight workers:

| Radius | Limit | Trials | Result | Best candidate | Distance |
| --- | ---: | ---: | --- | ---: | ---: |
| R2 | 240 s | 3 | `UNKNOWN` in every trial | none | unresolved |
| R2 | 600 s | 1 plus quality replay | complete candidates | `65,165` | 2 |
| R4 | 240 s | 3 | `UNKNOWN` in every trial | none | unresolved |
| R4 | 600 s | 2 successful trials | complete candidates | `65,171` | 3--4 |
| R8 | 240 s | 3 | `UNKNOWN` in every trial | none | unresolved |
| R8 | 600 s | 2 successful trials | complete candidates | `65,171` | 7 |

Every successful candidate retained all `10,760` mandatory requests and all
`310` special commitments. No candidate was adopted into the production
result. The short-horizon `UNKNOWN` results do not prove neighborhood
infeasibility: a valid R2 witness was found when the same radius received a
600-second horizon. The known distance-two witness is also mathematically
inside R4 and R8, so their short-horizon `UNKNOWN` results represent search
difficulty rather than a lack of an improving schedule.

The existing quality evaluator was invoked in-process for representative
successful candidates and its per-entity detail was reduced to bounded
diagnostic facts. The strongest R2 candidate had utilization `6,867`, one
delivery group improved, `54` were unchanged, and none worsened. Semester
balance, difficulty, category diversity, fulfillment, special commitments,
and preservation were unchanged. A representative R4 candidate at `6,873`
showed one utilization entity worsen and `54` unchanged despite the
solver-authoritative aggregate improving by two points; this is reported as a
trade-off rather than hidden. A representative R8 candidate had no evaluator
entity changes while the solver-authoritative utilization aggregate improved
by two points. The solver aggregate remains authoritative, while the
per-entity evaluator facts explain the practical distribution.

The probe records are compact JSONL diagnostics in the temporary experiment
results location. They include fingerprints, radius, configured limits,
status, candidate values, source-decision distance, affected entities,
solver branches/conflicts, model size, timings, and bounded quality facts.
Peak memory was not available for this study because the environment did not
have `psutil`; the prior accepted full-pipeline replay remains the source of
the available laptop-memory observation.

After the radius study, the existing adaptive diagnostic VNS entry point was
run once with radii `(2, 4, 8)`, a 600-second per-probe allowance, an
1,800-second shared diagnostic budget, and the same frozen seed. Each of its
three iterations improved R2 and therefore restarted at R2 before reaching
R4 or R8:

```text
65,173 -> 65,165 -> 65,159 -> 65,153
```

All three candidates were CP-SAT-generated, full-model-validated, and
adopted only inside the diagnostic run. The final diagnostic result remained
complete with `10,635` assignments, zero unmet requests, and `310` special
commitments. The diagnostic wrapper reported `UNKNOWN` for the later ordinary
optimization portion after its bounded budget, but it retained the complete
`65,153` candidate. No production Stage 2 result or persisted schedule was
changed.

The current frozen-benchmark classification is therefore: **nearby
substantive improvements exist and local CP-SAT search can find them when
given a sufficiently long bounded horizon; the strongest observed path is
R2, while global substantive optimality remains unproven**. The 240-second
radius probes are too short to establish local optimality. Iterative VNS is
justified as a diagnostic investigation, but it is not promoted into ordinary
production scheduling until repeated runs establish a stable policy, resource
envelope, and counselor-relevant benefit. No objective definition, counselor
priority, hard constraint, or ordinary Stage 2 behavior was changed.

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

Diagnostic optimization facts also expose bounded incumbent timelines,
objective metadata grouped by counselor importance level, and observational
model-family variable counts. These fields support later search and
formulation experiments without exposing auxiliary CP-SAT identities as
schedule meaning and without changing the ordinary production solve.

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

## Target-scale ordinary-horizon comparison

The target-scale diagnostic harness also ran a clean ordinary Stage 2 replay
with a `3,600`-second Stage 2 horizon. The run used the same final-staffing
fixture shape and the explicit Stage 1 configuration needed to obtain a
validated seed. It completed with `10,635` assignments, zero unmet required
requests, and all `310` special commitments fulfilled.

The one-hour ordinary result remained at substantive tier `65,173`:

```text
Stage 1: [-10945, 0, 0, 0, 65173, ...]
Stage 2: [-10945, 0, 0, 0, 65173, 5669086063307]
```

Section utilization, semester balance, difficulty, and category diversity
remained `6,875`, `175`, `35,973`, and `22,150`. The additional ordinary
search improved only the opaque final tie-break. The complete child process
took `3,988.97` seconds, including upstream preparation; this is an
experimental observation, not a new production timeout.

The target-scale diagnostic comparison currently supports these frontier
observations:

| Strategy | Stage 2 horizon | Substantive tier | Result |
| --- | ---: | ---: | --- |
| Ordinary | 1,800 s | 65,173 | Complete; substantive metrics unchanged |
| Ordinary | 3,600 s | 65,173 | Complete; only the opaque tie-break improved |
| Radius-two local bootstrap + ordinary | 1,800 s | 65,163--65,165 | Complete across four matched trials; diagnostic-only |
| Adaptive radius-two bootstrap + ordinary | 1,800 s | 65,135 | Complete; one matched target trial; diagnostic-only |
| Radius-two local bootstrap + ordinary | 3,600 s | 65,161 | Complete; one matched trial; diagnostic-only |
| Full-vector retention | 1,800 s | 65,173 | Complete; no substantive improvement in this trial |
| Strict substantive probe | bounded diagnostic | 65,171 candidate | Feasibility evidence, not ordinary production behavior |

The local-bootstrap results are promising because they found materially better
validated schedules within the existing total budget, and the adaptive trial
reached `65,135`, a `38`-point improvement over the Stage 1 substantive value.
The result has not yet passed a repeated adaptive target-scale promotion gate,
so the bootstrap remains diagnostic-only. The ordinary one-hour replay from
the original seed did not improve the substantive tier, while the regular
local-seeded one-hour replay reached `65,161`; neither result justifies
changing the ordinary production horizon or objective semantics. No ordinary
solver objective, production horizon, or counselor-facing behavior was
changed by these experiments.

## Repeated radius-two descent hardening study

The later frozen-benchmark validation repeated the adaptive local search as a
radius-two-only descent from the exact stored Stage 1 source seed. Every probe
used eight workers, a maximum of `600` seconds, and the same shared
`1,800`-second local-search horizon. A candidate was adopted only after it was
generated by CP-SAT, was complete, passed the unchanged full-model validator,
and was strictly better in the existing summed substantive tier.

All target runs used input fingerprint
`1c4843ac33fccabd76218c63d8818c94a0a8dd8ab2886e3f5718ca1cd9576a11` and Stage
1 seed fingerprint
`00889b7f4110dc19c6cdcb413b44fe77ab9598cb0fde25de6ea618ddb27325e7`.

| Run | Adopted R2 trajectory | Final substantive | Utilization | Local result | Assignments / unmet / special |
| --- | --- | ---: | ---: | --- | --- |
| 1 | `65173 -> 65163 -> 65155 -> 65147` | `65147` | `6849` | complete | `10635 / 0 / 310` |
| 2 | `65173 -> 65165 -> 65157 -> 65153` | `65153` | `6855` | complete | `10635 / 0 / 310` |
| 3 | `65173 -> 65165 -> 65157 -> 65151` | `65151` | `6853` | complete | `10635 / 0 / 310` |

All three matched repetitions improved below the `65173` Stage 1 baseline and
adopted three validated R2 candidates, but their final values varied by six
points. The descriptive classification is **partially reproducible**, not
strongly reproducible: the descent is consistently useful, but the exact
endpoint is search-sensitive. The final semester, difficulty, category,
fulfillment, and special-commitment values remained unchanged; the measured
substantive improvements were section-utilization improvements.

A fourth repetition was run after the quality-payload hardening so each
adopted candidate retained bounded counselor-readable impact facts. It adopted
`65165` and `65157`, then reached an unresolved third probe and retained the
complete `65157` candidate. Its process timing was anomalous and is not used
as a normal runtime representative. Its quality payload showed pairwise
section-utilization changes of `-8` and `-8`; the corresponding range-based
descriptive changes were `-1` and `-1`. This distinction is intentional:
pairwise contribution is the solver-aligned metric, while range is only a
shape summary.

The two post-hardening repetitions with native Windows telemetry reported
local-search peak working sets of approximately `3.10 GiB` and `3.12 GiB`,
with peak pagefile usage of approximately `3.63 GiB` and `3.64 GiB`. Spot
checks during finalization showed larger transient process working sets, so
the local-search telemetry must not be interpreted as a whole-process upper
bound. The first repetition predates the telemetry fix and has no native
memory record. The fourth run is excluded from normal resource statistics
because of its anomalous wall-clock measurement.

The parity audit corrected diagnostic comparisons to use each delivery
group's `pairwise_absolute_difference`, exactly matching the solver's
section-utilization objective. Range-based comparisons remain available under
`range_comparison`; they are not presented as aggregate objective evidence.
Focused tests now require the sum of per-group pairwise contributions to equal
the reconstructed aggregate, subject to the existing integer semantics.

The repeated descent did not pass a production-promotion gate. A longer
`3,600`-second R2-only frontier was run because all three clean shorter runs
were useful. It adopted six validated improvements:

```text
65173 -> 65165 -> 65161 -> 65159 -> 65155 -> 65145 -> 65143
```

It remained complete with `10,635` assignments, zero unmet requests, and all
`310` special commitments fulfilled. The final utilization penalty was
`6,845`; semester, difficulty, and category remained `175`, `35,973`, and
`22,150`. The sixth and final probe still improved the tier, so this run did
not establish an R2 plateau. Its local-search peak working set was about
`3.06 GiB` and peak pagefile usage about `3.59 GiB`; total child-process time
was about `3,710` seconds including finalization.

At that earlier frontier endpoint, no mature-R2 R4 escape or R8 escalation was
run because R2 was still finding validated improvements. VNS and R2 descent
remain diagnostic-only: endpoint variation, continued late-horizon
improvements, and the need for an operational safety study mean that no
bounded production policy can yet be selected responsibly.
Ordinary Stage 2, objective definitions, hard constraints, approval behavior,
and fixture data remain unchanged.

## Mature-R2 checkpoint and student-bounded neighborhood diagnostics

Continuation work uses a transparent, versioned
`student_assignment_mature_r2_checkpoint_v1` JSON/JSON-Gzip artifact. A
checkpoint stores the parent input fingerprint, semantic source decisions,
source fingerprint, objective/component facts, completeness, and validation
facts. It never stores a solver object or pickle state. Replay must
re-materialize the source decisions against the current DTO and validate them
against the unchanged full model before using them as a diagnostic seed.

The bounded-neighborhood diagnostic may additionally limit the number of
students whose meaningful source decisions differ from the incumbent. This is
an experimental constraint layered on the existing source-decision Hamming
radius; it is not a production counselor control and does not change ordinary
Stage 2. A student counts once even when several of that student's courses or
special commitments move. `None` preserves source-radius-only behavior.

Long-running continuation and neighborhood experiments record compact process
and system resource facts under the canonical contract in
`docs/OBSERVABILITY_AND_MONITORING.md`. Resource telemetry is diagnostic and
cannot establish feasibility, improve an objective, or authorize a candidate.

The first durable continuation checkpoint was created from a clean target-scale
R2 descent with eight workers and a 600-second neighborhood allowance. It
reached `65,165` from the frozen Stage 1 value of `65,173`, remained complete
with `10,635` assignments and zero unmet requests, and stored all `10,945`
semantic source decisions. Its source fingerprint is
`2a038e67e9047af3541dacc413d68506deccd2e379fd050bc8113f94b3f083a9`.
Two independent clean-process replays materialized and validated that
checkpoint against the unchanged full model and returned the same complete
`65,165` objective vector. This is checkpoint/replay evidence, not proof that
the R2 neighborhood is globally exhausted; continuation beyond this incumbent
still requires an explicit stop classification based on additional bounded
descent results.

The mature checkpoint was subsequently continued from the incumbent rather
than restarting from Stage 1. Five clean bounded R2 continuations produced
the following validated descent:

```text
65173 -> 65165 -> 65159 -> 65155 -> 65153 -> 65151 -> 65149
```

Each adoption changed two semantic source decisions, remained complete and
full-model-valid, retained `10,635` assignments, fulfilled all `10,945`
required decision groups, and left the `310` special commitments fulfilled.
The measured continuation elapsed times were approximately `674`, `484`,
`794`, `728`, and `651` seconds; CP-SAT solver wall times were approximately
`146`, `121`, `212`, `119`, and `124` seconds respectively. The observed
substantive improvement came from section-utilization balance, which moved
from `6,875` at Stage 1 to `6,851` at the current `65,149` checkpoint; the
semester, difficulty, and category components remained `175`, `35,973`, and
`22,150`.

The five mature continuations consumed approximately `3,331` seconds of
bounded diagnostic descent, close to the planned longer-frontier horizon.
Because the final bounded probe still improved the incumbent, this is an
explicit experiment-horizon stop, not evidence of an R2 plateau.

This continuation is evidence that the mature R2 incumbent was still locally
improvable under the tested bounded searches. It is not a proof of global
optimality or of a stable production policy. The current checkpoint and
compact frontier records are diagnostic artifacts, not ordinary scheduling
inputs or production decisions.

### Mature-local session profiling

The diagnostic path now has a mature-local-only mode. It validates the supplied
checkpoint once, performs the R2 probe, validates the candidate against the
unchanged full model, and returns without launching ordinary lexicographic
Stage 2. Ordinary scheduling and ordinary Stage 2 behavior are unchanged.

One target-scale profile started from `65,149` and produced a complete,
full-model-valid `65,143` candidate in `314.541` seconds. It retained
`10,635` assignments, zero unmet requests, and all `310` special commitments.
The R2 solve consumed `211.054` seconds externally (`210.902` seconds as
reported by CP-SAT); candidate validation consumed `11.025` seconds. The
profile also measured approximately `25.118` seconds of model construction,
`7.633` seconds of mature-checkpoint validation, and `15.191` seconds of
review/diagnostic construction. The local probe's setup timings were:
`0.443` seconds model clone, `2.545` completion constraints, `1.999`
neighborhood constraints, `5.037` objective/bound setup, and `6.953` hint
application.

The local-only path recorded no ordinary lexicographic optimization passes.
Compared with the historical `651.042`-second continuation that started from
`65,151`, this profile completed in roughly half the wall time while also
finding a stronger incumbent. That comparison includes different CP-SAT
search outcomes and is not a controlled same-incumbent A/B test; it is
profiling evidence, not a universal speedup claim. The checkpoint now stores
the `65,143` source decisions with fingerprint
`5e58aecf29b0c269ddab60b61a32cf493e7b9e02562fed6dea7696c3f7fb46c2`.

That was the checkpoint immediately after the profile; it was subsequently
continued through the durable `65,133` checkpoint and the two multi-iteration
sessions described below. The current durable checkpoint is now `65,025`.

Checkpoint serialization is not a material contributor at this scale: encoding
and compressing all `10,945` source decisions took `0.371` seconds and
produced an `88,304`-byte gzip artifact in an isolated measurement. This is a
file-only measurement and does not include filesystem contention or a full
checkpoint-save transaction.

A current checkpoint-write micro-measurement, including canonicalization,
fingerprinting, JSON encoding, gzip compression, and a temporary-file write,
took `0.791` seconds for the same `10,945` decisions. Its measured substeps
were `0.059` seconds for materialization, `0.195` seconds for fingerprinting,
`0.022` seconds for JSON encoding, and `0.061` seconds for compression. The
temporary artifact was removed after measurement.

The durable local-only continuation was then extended from `65,143` to
`65,133`. That historical session took `359.030` seconds total, including `229.177`
seconds of CP-SAT R2 solver wall time and `10.065` seconds of full-model
candidate validation. It retained `10,635` assignments, zero unmet requests,
and all `310` special commitments. The resulting checkpoint has source
fingerprint
`67131d41aeb39e53248fbc3d9f81c907d7e30d9d589d30dd8ff633cb0e1e0e84`; the
current checkpoint fingerprint after the later sessions is
`d5036a44e71d5a3b2a94eebe51d645bb4034179a0dd29537492ea81feda2e900`.

The mature-local wrapper now also supports an opt-in session persistence mode
for diagnostic continuations. When enabled, the strongest complete,
full-model-validated incumbent returned by the in-memory multi-iteration
session is written to a temporary checkpoint file, flushed, fsynced, and
atomically replaced into the configured checkpoint path. Only after that
replacement succeeds is the compact frontier row appended and fsynced. The
frontier row records both the parent checkpoint source fingerprint and the
resulting checkpoint source fingerprint, so a durable row cannot claim a
checkpoint that was not safely written first. Persistence is diagnostic-only;
it does not alter immutable scheduling runs, approvals, or ordinary Stage 2.

The session telemetry distinguishes the per-iteration R2 work from the
session-level wrapper work and records cumulative elapsed time after each
iteration. A later session can therefore resume from the last durable
incumbent without rerunning the previous session's accepted improvements.
An interrupted session leaves the previous checkpoint intact; an incomplete,
unvalidated, unknown, or weaker candidate is never persisted.

The first multi-iteration continuation from the durable `65,133` checkpoint
adopted twelve consecutive complete, full-model-validated R2 improvements in
one process, reaching `65,077` in `2,592.6` seconds. It retained `10,635`
assignments, zero unmet requests, and all `310` special commitments. Across
those iterations, CP-SAT accounted for approximately `2,095.6` seconds and
full candidate validation for `74.6` seconds; the remaining session time was
model/setup, extraction, quality, telemetry, and finalization work. It stopped
at the diagnostic iteration cap used for that run, so it was not evidence of a
plateau.

A second continuation from `65,077`, with the iteration cap raised and the
same `3,600`-second bounded R2 horizon, adopted fourteen further validated
improvements and reached `65,025`. Its fifteenth probe returned `UNKNOWN`
without a validated candidate after the shared horizon was exhausted; the
strongest prior incumbent was retained and persisted atomically. The session
finished in `3,633.6` seconds including finalization and checkpoint/frontier
writes. This is an unresolved-search/horizon stop, not a proven R2 local
optimum: only an `INFEASIBLE` strict-improvement probe can establish the latter.
These two sessions are diagnostic evidence that ordinary R2 remains productive
from the current target-scale frontier; they do not justify promoting R4/R8
while R2 continues to produce validated improvements.

After the second session ended with an unresolved `UNKNOWN` probe, matched
target-scale R4 screening was run from the same durable `65,025` checkpoint.
Ordinary R4, R4/S1 (`max_changed_students=1`), and R4/S2
(`max_changed_students=2`) each received one 180-second, eight-worker
diagnostic attempt. All three returned `UNKNOWN` without a candidate; all
retained the complete `65,025` incumbent, with no candidate validation or
adoption. These are inconclusive negative screens, not proofs that R4 or its
student bounds are infeasible. No R4 candidate was therefore persisted, no
R2-after-escape run was required, and R8 was not justified. A longer or better
evidence-gated R4 experiment would be a separate diagnostic study.

The later evidence-gated study corrected the diagnostic wrapper so that
`max_changed_students` is forwarded into the CP-SAT neighborhood model. The
earlier short R4/S1 and R4/S2 labels above therefore remain historical
screening records, but they must not be treated as valid student-bounded
experiments. From the frozen, full-model-valid `65,025` checkpoint, matched
600-second first-pass probes produced the following diagnostic-only results:

| Neighborhood | Result | Changed students / source decisions | Substantive | Component change | CP-SAT wall | Full validation |
|---|---|---:|---:|---|---:|---:|
| R4 | validated escape | 3 / 4 | 65,021 | section utilization -4 | 176.8 s | 4.5 s |
| R4/S1 | validated escape | 1 / 4 | 65,023 | section utilization -2 | 311.4 s | 18.0 s |
| R4/S2 | validated escape | 2 / 4 | 65,007 | section utilization -18 | 190.4 s | 10.1 s |
| R8 | validated escape | 6 / 8 | 65,005 | section utilization -20 | 202.2 s | 10.3 s |
| R8/S1 | validated escape | 1 / 3 | 65,021 | section utilization -4 | 196.6 s | 8.4 s |
| R8/S2 | validated escape | 2 / 8 | 65,005 | section utilization -20 | 187.0 s | 9.8 s |

All candidates remained complete with `10,635` assignments, zero unmet
requests, and unchanged semester-balance, difficulty, category-diversity, and
schedule-preservation components. The R4/S2 candidate was repeated in two
additional clean processes and reproduced `65,007`, the same two affected
students, and the same `-18` section-utilization delta in all three runs.
The R4/S2 branch was therefore used for one diagnostic continuous R2 descent
without changing the canonical `65,025` checkpoint. That R2 session adopted
twenty further validated improvements in twenty-one probes and reached
`64,929` in `3,609.95` seconds (`3,136.75` seconds CP-SAT and `106.03`
seconds full validation). Its final probe was `UNKNOWN`, so the result is a
complete unresolved bounded incumbent, not a proof of local optimality.

The R4/S2 escape and its follow-on R2 descent show that a small coordinated
student neighborhood can unlock substantial section-utilization descent.
They do not justify changing production Stage 2 objective semantics or
promoting R4/R8 into the ordinary optimizer. The R8 scores were single-run
diagnostic observations, while R4/S2 is the only escape strategy in this study
with three clean-run reproductions. Any promotion decision requires a separate
production-policy review and must preserve the existing hard constraints,
objective definitions, and canonical checkpoint lineage.

A bounded target-scale replay of the old post-local path, starting from the
same `65,143` checkpoint, measured `25.270` seconds for the local probe and
then `23.425` seconds of ordinary Stage 2 work after the local probe. That
ordinary work produced no substantive improvement; its first objective pass
used `13.543` seconds and returned `UNKNOWN`, while the already complete
incumbent remained valid. This confirms the orchestration cost observed in the
historical frontier rather than merely inferring it from total elapsed time.

On the medium 120-student fixture, the generic benchmark wrapper separately
spent `0.012` seconds reconstructing quality and `0.035` seconds constructing
the production-shaped summary after a `34.8`-second engine run. These are
small at medium scale, but the mature-local path avoids the duplicate quality
reconstruction because its engine result already contains the required compact
quality facts.

A bounded target replay with the normal Stage 1 bootstrap enabled measured
`27.693` seconds for model construction, `15.658` seconds for the CP-SAT seed
solve, `3.621` seconds for full-model seed validation, and `0.512` seconds for
Stage 1 quality extraction. This gives a direct representative bootstrap
measurement. The generic replay did not accept its supplied mature alternate
as the Stage 2 seed in that trial, so this result is used only to quantify
normal bootstrap cost, not as a matched old-versus-new quality comparison.

## Objective Semantics / Search Evidence v1 closeout

The current objective and search-mechanics study is now frozen as historical
v1 evidence, with the current-tree R8/S2 repeatability discrepancy explicitly
unresolved. The durable production-scale input remains the
`production_scale_v1` benchmark with input fingerprint
`1c4843ac33fccabd76218c63d8818c94a0a8dd8ab2886e3f5718ca1cd9576a11` and
`1,400` students, `10,760` requests, `10,945` required source groups,
`10,635` assignments, zero unmet required requests, and `310` special
commitments. The canonical comparison checkpoint was not changed: its source
fingerprint is
`d5036a44e71d5a3b2a94eebe51d645bb4034179a0dd29537492ea81feda2e900`, with
substantive value `65,025` and components `6,727` section utilization,
`175` semester balance, `35,973` difficulty, and `22,150` category diversity.

### R8/S2 repeatability

R8/S2 means radius `8`, `max_changed_students=2`, eight CP-SAT workers, a
bounded approximately `600`-second probe, strict substantive improvement, and
mandatory full-model validation. The older accepted first-pass record and the
two new clean-process replications started from the same canonical semantic
incumbent, but the current tree did not reproduce the older endpoint:

| Trial | Substantive | Direct gain | Changed decisions / students | Affected students | CP-SAT wall | Validation | Local operation |
|---|---:|---:|---:|---|---:|---:|---:|
| Historical accepted first run | `65,005` | `20` | `8 / 2` | `22, 478` | `187.0 s` | `9.8 s` | `229.6 s` |
| Current-tree clean repeat 1 | `65,021` | `4` | `1 / 1` | `1042` | `2.30 s` | `3.58 s` | `34.92 s` |
| Current-tree clean repeat 2 | `65,021` | `4` | `1 / 1` | `1042` | `2.39 s` | `3.71 s` | `35.71 s` |

Every candidate was complete, full-model valid, retained `10,635`
assignments, retained zero unmet requests, and fulfilled all `310` special
commitments. The two current-tree repeats selected student `1042` and moved
between sections `296` and `300`; each changed utilization by `-4` and left
the other substantive components unchanged. Both inner probes reported
`OPTIMAL`, and both validation classifications were `validated` with an
`OPTIMAL` validator result. The older `65,005` record remains valid historical
evidence, but it is not reproducible by the current clean-process path. The
three-record set is therefore classified as **inconsistent current-v1
repeatability**, not strong exact repeatability. This is an evidence mismatch,
not permission to rewrite the canonical checkpoint or infer that either
endpoint is globally optimal.

The fresh trials used the mature checkpoint directly and did not run Stage 1.
No canonical checkpoint or frontier was written by either process.

### Matched R4/S2 versus R8/S2 scorecard

The historical R4/S2 comparison consists of the original run plus two clean
repeats. One current-tree R4/S2 control was also run from the same canonical
checkpoint. It matched the current-tree R8/S2 result at `65,021`, with student
`1042` and sections `296`/`300`. The old and current records are not silently
pooled into one repeatability claim.

| Measure | R4/S2 | R8/S2 |
|---|---:|---:|
| Radius / student bound | `4 / 2` | `8 / 2` |
| Historical valid-candidate success | `3 / 3` | `1 / 1` |
| Current-tree valid-candidate success | `1 / 1` | `2 / 2` |
| Historical median substantive candidate | `65,007` | `65,005` |
| Current-tree substantive candidate | `65,021` | `65,021` |
| Historical best direct gain | `18` | `20` |
| Current-tree direct gain | `4` | `4` |
| Historical median CP-SAT wall | `186.3 s` | `134.7 s` |
| Current-tree CP-SAT wall | `2.40 s` | `2.35 s` |
| Current-tree full validation | `3.76 s` | `3.65 s` |
| Current-tree local operation | `35.96 s` | `35.31 s` median |
| Historical changed decisions / students | `4 / 2` | `8 / 2` |
| Current-tree changed decisions / students | `1 / 1` | `1 / 1` |
| Repeatability | historical exact; current control differs | historical endpoint not reproduced; current repeats exact |
| Branches / conflicts | not exposed historically | current: `676 / 8` |
| Comparable memory telemetry | not retained for the earlier R4/S2 set | peak process-tree working set approximately `4.04–4.11 GB` |

The older record suggested a two-point R8/S2 advantage, but the two current
clean repeats produced the same `65,021` result as the current R4/S2 control.
The current evidence therefore does not establish a stable direct R8 advantage.
The wider radius remains a diagnostic/reference operator, not a production
optimizer, pending investigation of the historical/current discrepancy.

### Temporary R8/S2 branch and matched R2 follow-on

One current-tree validated temporary branch was captured in memory without
modifying `mature_r2_checkpoint.json.gz`:

- parent source fingerprint: `d5036a44e71d5a3b2a94eebe51d645bb4034179a0dd29537492ea81feda2e900`;
- branch source fingerprint: `36adbb29d796d75497765d5a6b5b23c9ce4928a6db2075db180e5575da8a25b9`;
- objective vector: `[-10945, 0, 0, 0, 65021, 5669086063321]`;
- components: `6,723` utilization, `175` semester balance, `35,973`
  difficulty, `22,150` category diversity;
- validation: complete, seed validated, full-model validated;
- counts: `10,635` assignments, zero unmet, `310` special commitments.

The matched continuous R2 session from this temporary branch used the same
diagnostic policy as the earlier R4/S2 follow-on: radius `2`, no student bound,
eight workers, up to `600` seconds per probe, a `3,600`-second total horizon,
full validation after each candidate, and no ordinary Stage 2 between
iterations. It produced this compact trajectory:

```text
65,021 -> 65,017 -> 65,005 -> 65,003 -> 64,997 -> 64,995 -> 64,991
        -> 64,989 -> 64,987 -> 64,983 -> 64,975 -> 64,973 -> 64,971
        -> 64,969 -> 64,965 -> 64,961 -> 64,957 -> 64,955 -> 64,949
        -> 64,947 -> 64,943
```

Twenty improvements were adopted in twenty probes. Every adopted candidate was
complete and full-model validated; the final retained result had `10,635`
assignments, zero unmet requests, and all `310` special commitments. The
session used approximately `2,586.56` seconds of CP-SAT wall time and
`2,961.20` seconds in the engine local session. It reached the configured
attempt cap after twenty validated adoptions; this is not a proof of an R2
local optimum. The final components were `6,645` utilization, `175`
semester balance, `35,973` difficulty, and `22,150` category diversity.

### Complete escape-path comparison

| Path | Direct escape | R2 endpoint | Total gain vs `65,025` | R2 adopted improvements | Approx. diagnostic runtime |
|---|---:|---:|---:|---:|---:|
| R4/S2 -> R2 | `65,007` | `64,929` | `96` | `20` | escape plus `3,652.9 s` R2 session |
| R8/S2 -> R2 (historical branch) | `65,005` | `64,981` | `44` | `7` | escape plus `3,203.6 s` R2 session |
| R8/S2 -> R2 (current-tree branch) | `65,021` | `64,943` | `82` | `20` | escape plus `2,996.1 s` including branch setup |

The current-tree R8/S2 branch reached `64,943`, which is `14` points weaker
than the historical R4/S2 endpoint `64,929`. The historical R8/S2 branch
reached `64,981`; it is retained as historical evidence but cannot be treated
as current-tree repeatability. The measured complete-path evidence therefore
favors R4/S2, while the current fresh R8/S2 branch still demonstrates a
productive R2 descent. Neither path establishes a global optimum.

### v1 operator classification and closeout

The current evidence classifies the operators as follows:

For R8/S2 specifically, the classification is superseded by the current-tree
replication above: the two fresh trials both returned `65,021`, while the older
`65,005` trial was not reproduced. The current direct-repeatability result is
therefore inconsistent, and the current-tree R8/S2 -> R2 path reached `64,943`.

- **R2 — core retained operator:** repeatedly productive and the primary local
  descent mechanism;
- **R4 — useful escape candidate:** validated wider escape, but weaker than
  the bounded R4/S2 direct result in the matched matrix;
- **R4/S1 — diagnostic/reference only:** valid and informative, but weaker than
  R4/S2 and not supported as a retained default;
- **R4/S2 — core retained operator:** exact three-run repeatability and the
  strongest complete downstream path observed;
- **R8 — diagnostic/reference only:** valid direct escape evidence, but not
  repeated in the matched bound/student matrix;
- **R8/S1 — diagnostic/reference only:** valid but weaker direct result;
- **R8/S2 — unresolved diagnostic candidate:** the older record had the
  strongest direct escape, but two current-tree repeats produced `65,021`, the
  same endpoint as current R4/S2. Its current direct-repeatability
  classification is inconsistent, and downstream R2 from the current-tree
  branch was weaker than R4/S2.

The current v1 characterization is complete enough to stop open-ended endpoint
shaving. No canonical checkpoint was changed and no current objective or
importance semantics were altered. The next work should address the objective
contract rather than continue accumulating isolated v1 endpoint evidence.

### First Objective Semantics v2 target-scale baseline

The first isolated v2 replay used the unchanged durable production-scale input
fingerprint
`1c4843ac33fccabd76218c63d8818c94a0a8dd8ab2886e3f5718ca1cd9576a11` and the
unchanged validated v1 source-decision seed. It did not modify the v1
checkpoint or fixture. The v2 compatibility presets resolved the five
`important`/`not_important` labels to scores `5, 5, 0, 5, 5`.

The fresh v2 Stage 1 hard bootstrap was tested with 8 workers and 30 seconds;
it returned `UNKNOWN` after approximately `31.1` seconds and did not produce
an independent seed. The unchanged v1 source seed was then validated against
the v2 full model and accepted. This is valid evidence that the hard model and
v2 objective auxiliaries accept the existing complete source assignment, but it
is not evidence that a cold v2 Stage 1 bootstrap completes within 30 seconds.

Using that validated seed, v2 Stage 2 ran with 8 workers and the existing
1,800-second bound. It returned a complete feasible candidate in approximately
`1,822.3` Stage 2 seconds (`2,002.7` seconds including detached loading,
model construction, validation, and finalization). The result retained
`10,635` assignments, zero unmet requests, and all `310` special commitments.
Raw components remained `6,875` section utilization, `175` semester balance,
`35,973` difficulty, and `22,150` category diversity. The v2 normalized
components were `1,750`, `77`, `536`, and `3,941`, for a weighted substantive
value of `31,520`; no substantive improvement was found. The final opaque
tie-break improved, so the final objective vector was
`[-10945, 0, 0, 0, 31520, 5669086063269]`.

This earlier `31,520` record belongs to the first v2 compatibility-profile
replay and is not numerically comparable with the later mixed-grade-v2
characterization, whose common target-scale source-seed value is `37,596`.
Those profiles, objective facts, and search results must remain separate; the
later operator characterization is the authoritative source for the mixed-
grade-v2 diagnostic comparisons.

This is the post-implementation v2 baseline, not a claim that v2 is already
the default production policy. A future promotion decision must separately
address cold Stage 1 bootstrap reliability and compare v2 quality/search
behavior against the frozen v1 evidence without mixing objective values across
versions.

### Frozen roadmap after v1 and v2 implementation

The current v1 search-mechanics evidence is historical and closed. Objective
Semantics v2 now implements the previously deferred normalization and one
canonical `0`--`10` importance representation. The v2 baseline must be
measured separately from the v1 baseline and must not overwrite the v1
checkpoint or historical trajectories.

The next research order is explicitly staged. Objective Semantics v2 and the
first student-targeted diagnostic operators are implemented, but no adaptive
production policy has been selected:

1. Establish and document the post-v2 target-scale baseline.
2. Revalidate only the retained v1 search operators that are relevant under
   v2, without assuming their v1 ranking carries over.
3. Characterize the implemented targeted S1/S2 diagnostics across repeated
   medium and target-scale trials, including time-to-first-improvement,
   changed students, validation cost, and repeatability. Initial target-scale
   capability results are recorded in the search-strategy document and are not
   a promotion decision.
4. Investigate adaptive allocation across targeted repair, R2, R4/S1, R4/S2,
   R8/S1, R8/S2, and unrestricted variants where evidence justifies them.
5. Investigate grade-bounded unrestricted escape only after targeted/adaptive
   studies. A selected grade changes source decisions only for students in
   that grade; the full model still applies, including frozen students,
   mixed-grade sections, capacity, conflicts, prerequisites, locks, and
   special commitments.
6. Return to faster local operators after a successful grade-scoped escape;
   consider full-school unrestricted escape only as a later evidence-gated
   escalation.
7. Run a final production-policy and promotion study only after those
   prerequisites.

Objective v2 preserves the invariant that if multiple soft preferences have
the same counselor importance, their practical influence should be
approximately comparable rather than being dominated by raw metric magnitude.
The current v1 observed raw contribution shares were approximately `10%`
section utilization, `0.3%` semester balance, `55%` difficulty, and `34%`
category diversity. These values are evidence for the future design problem,
not normalization constants. The future adaptive-search policy must also keep
the distinction between **what** is better (the normalized counselor-weighted
objective) and **how** to search (operator selection); heuristics may choose a
student, pair, neighborhood, or grade to explore, but never authorize a
candidate.

### Objective Semantics v2 targeted-repair evidence gate

Utilization-cluster guidance does not introduce a new quality metric. The
section-utilization metric remains the global pairwise absolute-difference
penalty defined by the objective model. The diagnostic utilization-cluster
families may use current section counts and optimistic single-request deltas
to choose a bounded multi-student neighborhood, but those deltas are guidance
only and are not added to the quality report or attributed to students. The
unchanged full model and full-model validator determine actual candidate
quality. The target-scale study and its non-promotion boundary are recorded
in [`STUDENT_ASSIGNMENT_UTILIZATION_CLUSTER_SEARCH.md`](STUDENT_ASSIGNMENT_UTILIZATION_CLUSTER_SEARCH.md).

### Continuous operator-session implementation boundary

The mature-R2 continuation has now been generalized in the pure engine into
`run_student_assignment_operator_session_diagnostic`. The session contract
supports `r2`, `targeted_r4_s1`, `targeted_r8_s1`, `targeted_r4_s2`, and
`targeted_r8_s2`. This is a reusable diagnostic boundary, not a production
optimizer and not an adaptive-policy promotion.

One session validates its supplied complete semantic incumbent, builds the
production model and static probe scope once, and then runs bounded CP-SAT
probe attempts against fresh model clones. After a validated strict
improvement, only incumbent-dependent bounds, hints, target scope, and
validation state change. Dynamic targeted sessions recompute their student
scope from the current validated quality facts; fixed sessions reuse the
caller-supplied scope. All candidates still require CP-SAT plus full-model
validation before adoption.

The session owns one monotonic wall-clock budget covering setup, validation,
probe construction, CP-SAT, candidate extraction, repeated validation, and
finalization. Native solver overrun is recorded separately. `UNKNOWN` remains
unresolved and distinct from proven infeasibility or proven scope exhaustion;
the last complete validated incumbent is retained. Session telemetry records
target history, attempts, adopted improvements, CP-SAT time, validation time,
total elapsed time, overrun, memory facts, and stop reason.

The current implementation and medium-fixture regression coverage establish
reusable multi-attempt behavior, dynamic retargeting, fixed-target isolation,
and status-authority semantics. A target-scale repeated-family calibration
and adaptive-versus-static promotion study remain future work and must use
detached, fingerprinted input without mutating canonical checkpoints.

The canonical operator contract and the current matched v2 targeted-repair
study are maintained in
[`STUDENT_ASSIGNMENT_SEARCH_STRATEGY.md`](STUDENT_ASSIGNMENT_SEARCH_STRATEGY.md).
The study used one detached 1,400-student input and one validated source seed.
Weighted targeted R8/S1 and R8/S2 each produced three complete, full-model-
validated trials at stable endpoints (`6,871` and `6,869`, respectively).
Ordinary CP-SAT-selected R4/R8 S1/S2 controls found no improvement within the
matched bounded trials and retained `6,875`; a deterministic targeted control
reached `6,873`. The weighted and raw policies selected the same leading
students on this input, so weighted-ranking superiority over raw ranking is
not established by this fixture alone. This is diagnostic evidence for the
next adaptive-allocation study, not production promotion or adaptive policy.

Historical references in this document to an “adaptive bootstrap” or “adaptive
VNS” refer to bounded v1 diagnostics. They do not mean that the future v2
adaptive operator allocator has been implemented. The separate v2 adaptive
operator allocator is now implemented as an offline diagnostic policy in
`STUDENT_ASSIGNMENT_ADAPTIVE_SEARCH.md`; it is not production-wired and does
not alter objective semantics or candidate authority.

### Target-scale reusable-session qualification

The current v2 operator-session implementation has passed a detached
target-scale qualification gate using input fingerprint
`faa7a016b553d662821cb1247bb70fed8b9021dc289a6b406ff9f7c993b0d280` and
source-seed fingerprint
`d5036a44e71d5a3b2a94eebe51d645bb4034179a0dd29537492ea81feda2e900`. The
1,400-student source facts were preserved: 10,760 requests, 10,945 required
groups, 10,635 assignments, zero unmet required requests, and 310 special
commitments.

At eight workers with three 30-second attempts per session, dynamic sessions
started from v2 substantive value `37,596` and ended as follows:

| Session | Final substantive value | Validated adoptions | Wall time |
| --- | ---: | ---: | ---: |
| R2 | 37,596 | 0 | 57.0 s |
| targeted R4/S1 | 37,578 | 3 | 73.6 s |
| targeted R8/S1 | 37,572 | 3 | 70.3 s |
| targeted R4/S2 | 37,470 | 3 | 71.2 s |
| targeted R8/S2 | 37,470 | 3 | 74.6 s |

All adopted candidates were complete and passed the unchanged full-model
validator. No run changed fulfillment, assignment count, or special-
commitment completion. Section-utilization improvement appeared throughout
the targeted trajectories; the S2 trajectories also included a validated
difficulty/category component move while preserving semester balance. The S2
sessions retargeted after adoption and followed the same `(1052, 1068)` to
`(1052, 1072)` target transition. Fixed-target controls also completed; one
fixed R8/S2 control reached `37,464`, which is useful control evidence but not
enough to establish fixed-policy superiority.

A session-reuse A/B comparison reached the same fixed-R8/S2 endpoint in 73.2
seconds as one continuous session, compared with 163.7 seconds for three
independent wrapper calls. This supports the session-static reuse design. Peak
working-set telemetry remained below approximately 0.95 GB in the measured
process. These are diagnostic facts, not production-policy approval.

The next authorized research boundary is comprehensive diagnostic
characterization of the implemented operator portfolio. Adaptive allocation
remains a separate future study and is not production-wired; grade-bounded
search is implemented diagnostically and is covered by the operator
characterization evidence catalog. Full-school global search remains deferred.
