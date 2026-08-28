# Student Assignment Adaptive Search (Objective Semantics v2)

## Status

The v2 adaptive allocator is implemented as a diagnostic-only, offline
experiment. It is not called by ordinary student assignment, is not part of
approval, and does not select or authorize persisted schedule facts.

The policy answers only **where to search next**. CP-SAT remains responsible
for producing every candidate, and the existing full-model validator remains
responsible for deciding whether a candidate is safe to adopt. A candidate
that is partial, `UNKNOWN`, unvalidated, or not strictly better is never
allowed to replace the current complete incumbent.

This document is intentionally separate from the historical v1 adaptive
bootstrap/VNS experiments. Those older experiments used a bounded radius
sequence inside one solver call. The v2 allocator is a policy layer over
existing independent diagnostic operators and uses Objective Semantics v2
quality facts to allocate a shared offline budget.

## Scope and non-goals

The implemented diagnostic portfolio exposes the complete current set of
existing local, targeted, utilization-cluster, and grade-bounded operators.
The allocator can select among them for offline calibration. This does not
make the portfolio a production policy: portfolio classification and matched
adaptive-versus-static evidence are still required before any promotion.

| Operator | Search scope | Role |
| --- | --- | --- |
| `r2` | ordinary radius 2, no student bound | local descent |
| `targeted_r4_s1` | targeted radius 4, one student | targeted repair |
| `targeted_r8_s1` | targeted radius 8, one student | targeted repair |
| `targeted_r4_s2` | targeted radius 4, two students | targeted repair |
| `targeted_r8_s2` | targeted radius 8, two students | targeted repair |
| `targeted_utilization_r16_s2` through `targeted_utilization_r64_s10` | bounded utilization clusters | utilization repair |
| `grade_bounded_g9` through `grade_bounded_g12` | one actual grade unrestricted; other grades frozen | basin escape |

Full-school global escape, reinforcement learning/bandits, adaptive objective
weighting, and production operator selection are not implemented. In particular,
the allocator cannot change
the objective definition, importance scores, hard constraints, fulfillment
semantics, locks, or approval behavior.

## Policy state

`adaptive_search.py` owns explicit, JSON-safe policy state:

- the Objective Semantics v2 version and canonical counselor scores;
- current normalized components and weighted contributions;
- student-local pressure total, highest pressure, top-k concentration, and
  nonzero-pressure count;
- global section-utilization share;
- elapsed and remaining shared budget;
- immutable history for prior operator attempts.

Student-local pressure uses only the existing attributable facts for semester
load, difficulty, category diversity, and applicable sequence opportunities.
Section utilization remains a global pairwise metric and is not falsely
attributed to a student. Counselor scores describe intent; observed pressure
describes the current incumbent. They are kept as separate fields.

The initial policy score is deliberately transparent and deterministic. It
combines bounded signals for the operator's role, prior validated success,
observed gain per minute, unknown rate, and whether an operator is untried.
The signals are an experimental starting point, not calibrated production
weights. Ties use stable operator names and radius ordering.

## Session lifecycle

One diagnostic session uses one monotonic wall-clock budget:

```text
complete validated incumbent
    -> build current quality/pressure state
    -> select one existing operator
    -> CP-SAT diagnostic probe
    -> candidate extraction and existing full-model validation
    -> adopt only a strict validated improvement
    -> recompute quality and ranking
    -> repeat until budget or iteration stop
```

Every probe receives the current semantic source-decision incumbent as an
alternate seed. The source decisions are not converted into permanent hard
assignments by the policy. The selected operator's unchanged hard model and
its existing full validation are authoritative. A hard-proven `INFEASIBLE`
scope is recorded distinctly from an unresolved `UNKNOWN`; neither is treated
as proof of a global optimum.

After adoption, quality facts and student ranking are recomputed from the new
result. The policy therefore never continues to target students using stale
pressure from an older incumbent. The session record contains the selected
operator, reasons/signals, status, candidate/validation/adoption facts, gain,
timings, changed entities, and stopping reason.

## Continuous operator sessions

The reusable diagnostic session entry point is
`run_student_assignment_operator_session_diagnostic` in the pure engine. It
generalizes the mature-R2 in-memory continuation boundary to these explicit
operator families:

- `r2` — radius two with no student cap;
- `targeted_r4_s1` and `targeted_r8_s1` — radius four/eight with one student;
- `targeted_r4_s2` and `targeted_r8_s2` — radius four/eight with two students.

One session builds and validates its supplied complete semantic incumbent,
constructs the production model and static probe scope once, and then runs
bounded probe attempts against clones of that model. An adopted candidate
updates only the incumbent-dependent seed, strict improvement bound, hints,
target scope, and validation facts. It does not rebuild placement, staffing,
the immutable input, or the static decision-group ownership map for every
attempt. The model clone and CP-SAT solve remain per-attempt because a probe
must receive a fresh bounded satisfiability model.

Targeted sessions support two explicit policies. `dynamic` recomputes the
bounded student target from the current validated quality pressure after each
adopted improvement. `fixed` keeps the caller-supplied one- or two-student
scope for every attempt. These IDs restrict diagnostic search only; they never
authorize an assignment and an empty/invalid target scope stops the session
with an explicit diagnostic stop reason.

The session has one monotonic wall-clock deadline. It covers model setup,
incumbent validation, probe model cloning, CP-SAT, candidate extraction,
full-model validation, and final session facts. `UNKNOWN` remains unresolved
and is distinct from proven `INFEASIBLE`; a complete validated incumbent is
retained in either case. The record reports configured budget, attempt count,
CP-SAT and validation time, total elapsed time, external overrun, target
history, adopted gains, resource facts, and the stop reason. A native solver
call can exceed its requested slice before returning, so external overrun is
reported rather than hidden.

The adaptive allocator remains an offline policy diagnostic. Its session
request shape describes an operator family, allocated chunk, attempt cap,
per-attempt CP-SAT ceiling, worker count, target policy, selected targets, or
selected grade. The allocator does not execute as ordinary scheduling
behavior. Static R2, specialized, utilization, fixed-cycle, stateless-role,
and adaptive comparisons are research controls; this interface does not imply
production promotion.

Two solver-free controls are available for calibration. The stateless-role
selector uses the same current-state role signals with an empty operator
history, while the fixed-cycle selector follows an explicitly supplied
operator sequence. Neither control runs a solver or authorizes a schedule;
they exist so matched comparisons can separate value from policy complexity.

## Offline replay and comparison

`AdaptiveSessionRecord` is a structured diagnostic artifact. It includes the
input fingerprint, source-seed fingerprint, objective-semantics version,
score mapping, objective facts, attempts, decisions, and resource facts. The
policy replay helper consumes records without CP-SAT, so selection logic can
be inspected or regression-tested independently of solver execution.

The minimum comparison set is:

1. R2-only;
2. targeted R8/S1-only;
3. targeted R8/S2-only;
4. a fixed cycle over the retained portfolio;
5. the adaptive allocator.

All controls must use the same detached input, complete source-decision seed,
Objective Semantics v2 profile, worker configuration, and shared budget. A
target-scale experiment must not mutate a canonical checkpoint or rerun
upstream placement/staffing unless the fixture does not provide a valid
detached input. Any reusable detached representation must be versioned,
fingerprinted, and schema-validated; an unsafe pickle is not benchmark
authority.

## Promotion gate

The allocator remains diagnostic until repeated medium- and target-scale
evidence shows a reproducible operational benefit over static policies. A
promotion study must compare substantive quality, time to first improvement,
total runtime, full-validation cost, memory, completeness, hard validity,
repeatability, unknown/infeasible rates, and downstream workflow behavior.
An opaque tie-break improvement alone is not sufficient. Normal run-to-run
variation must be measured before declaring an improvement meaningful, and an
absolute machine-safety memory ceiling applies in addition to relative
regression checks.

The policy must never be promoted by changing the objective definition. If
same-priority components require product-level rescaling or a new counselor
contract, that is a separate Objective Semantics decision.

## Current offline policy implementation

The current implementation is versioned as
`v2-local-allocator-diagnostic-2`. It is a deterministic, JSON-safe policy
layer over the existing diagnostic operator sessions. It is not called by
ordinary student assignment, backend scheduling services, Celery execution,
approval, or persistence workflows.

The policy portfolio exposes the complete current diagnostic family:

- local descent: `r2`;
- student-pressure repair: `targeted_r4_s1`, `targeted_r8_s1`,
  `targeted_r4_s2`, and `targeted_r8_s2`;
- utilization repair: `targeted_utilization_r16_s2`,
  `targeted_utilization_r16_s4`, `targeted_utilization_r32_s4`,
  `targeted_utilization_r32_s6`, `targeted_utilization_r64_s6`,
  `targeted_utilization_r64_s8`, and `targeted_utilization_r64_s10`;
- basin escape: `grade_bounded_g9`, `grade_bounded_g10`,
  `grade_bounded_g11`, and `grade_bounded_g12`.

Role selection is based on current state signals. Student repair uses the
current student-local weighted pressure and its concentration; utilization
repair uses the global utilization share, pressured-group concentration, and
optimistic movable leverage; grade escape is eligible only after repeated
non-improvement and selects the grade with the strongest current actionable
opportunity. The policy does not contain a fixed preference for Grade 10,
R64/S8, R4/S2, or any other benchmark winner.

Within a selected role, history contributes observed adoption rate,
gain-per-minute, unknown rate, exact-scope exhaustion, and remaining-budget
signals. `UNKNOWN`, `validation_unknown`, `hard_invalid`, and
`validation_error` remain distinct. A proven exact scope may be skipped for
the current policy state, but no operator is globally blacklisted because of
one failed state. Every decision records the selected role, state signals,
history effects, budget estimate, target scope or grade, and deterministic
tie-break reason.

The adaptive runner executes the selected family through
`run_student_assignment_operator_session_diagnostic`, with one bounded
operation inside a single monotonic session budget. After an adopted result it
recomputes quality, pressure, utilization guidance, grade opportunity, and
history before selecting again. CP-SAT produces every candidate and the
unchanged full-model validator is the only adoption authority. A failed,
partial, unknown, unvalidated, or non-improving result leaves the complete
incumbent unchanged.

The current implementation is an offline calibration capability, not a
production policy. Static R2, specialized, utilization, fixed-cycle, and
stateless-role comparisons remain required before any promotion decision. The
available target-shaped evidence shows useful bounded operator behavior, but
does not yet prove that adaptive allocation outperforms a strong fixed policy
across multiple states, counselor profiles, or total budgets. Real mixed-grade
school data remains a later production-promotion gate.

## Matched calibration protocol

The offline calibration harness is implemented in
`scheduling_engine/student_assignment/adaptive_calibration.py`, with the
clean-process command surface in
`scheduling_engine/benchmark_adaptive_calibration.py`. It is versioned as
`adaptive-calibration-v1`. A trial applies one explicit v2 counselor-score
profile, consumes one detached input and one complete semantic source-
decision incumbent, and runs one selected policy under one shared outer
wall-clock budget. The controls are `r2_only`, `student_repair_only`,
`utilization_only`, `fixed_cycle`, `stateless_role`, and `adaptive`; they all
use the same existing operator-session boundary, CP-SAT workers, full-model
validation, strict-improvement adoption, and complete-incumbent retention.

The harness writes only a temporary, transparent
`student_assignment_diagnostic_branch_v1` checkpoint when a branch is needed
for comparison. The checkpoint records semantic source decisions, input and
source fingerprints, objective facts, completeness, and full-model validation
provenance. Reading the stored validation flag is never treated as authority:
the current DTO is materialized and the current full-model validator is run
before the trial. Branches are detached from the canonical benchmark and are
deleted after a trial unless an explicit output path is supplied. The harness
does not call ordinary production assignment, approval, persistence, or
upstream placement/staffing.

Session overrides in the calibration protocol control only diagnostic session
granularity (attempt caps and per-attempt/session slices). They do not change
hard constraints, objective definitions, counselor semantics, or candidate
authority. Session records separate policy-selection time, operator execution
time, finalization time, CP-SAT/validation facts, and external overrun so a
nominal solver slice is not misreported as total operational cost. A
calibration result is evidence about a search policy, not a production
recommendation.

The first post-implementation calibration screens used the production-shaped
detached input with fingerprint
`c07c77d0aa077a3e72240f27644d86b8a1a4faecb2f72a900aacc3fcb792d28a`, the
semantic seed fingerprint
`d5036a44e71d5a3b2a94eebe51d645bb4034179a0dd29537492ea81feda2e900`, eight
workers, a 60-second shared trial budget, and no canonical checkpoint writes.
The R2-only control retained the complete incumbent (`10,635` assignments,
zero unmet requests, `310` special commitments); its attempt returned
unresolved `UNKNOWN` without an adopted candidate. Its operator execution was
`93.806s` (`26.283s` CP-SAT), with `137.824s` spent in preparation and branch
validation before the trial. The fixed-cycle control also retained the same
complete incumbent and returned unresolved validation evidence without an
adoption. Its operator execution was `79.688s` (`4.466s` CP-SAT and
`11.094s` validation), with `211.690s` spent in preparation and branch
validation. These results demonstrate why calibration records separate
solver, validation, setup, and external-overrun time; they are not evidence
that either control is a production winner.

The final medium production-shaped screen used 80 generated students, the
same bounded 30-second shared trial configuration, and two workers. The
fixed-cycle control started from a complete `510`-assignment, zero-unmet
incumbent, adopted two validated improvements, and ended complete at
`35,262` versus `35,856` initially. This confirms multi-attempt execution,
branch revalidation, and complete-incumbent retention on a practical screen;
target-scale policy promotion still requires repeated matched trials and
resource-aware comparison.

## Supervised v2 adaptive-promotion study (2026-08-27)

Study `adaptive-promotion-v2-supervised-20260827` was opened to characterize
the current diagnostic policies under a true parent-side process boundary.
It used only the detached mixed-grade v2 benchmark with input fingerprint
`c07c77d0aa077a3e72240f27644d86b8a1a4faecb2f72a900aacc3fcb792d28a` and the
validated baseline source fingerprint
`d5036a44e71d5a3b2a94eebe51d645bb4034179a0dd29537492ea81feda2e900`.
The source facts remained 1,400 students, 10,760 requests, 10,945 required
groups, 10,635 assignments, zero unmet required requests, and 310 special
commitments. The baseline v2 substantive value was `37,596`.

The historical student-pressure branch with fingerprint
`e147beadd23c31a068acaa928cae3fb2fe5262ad6af92e695f5fa2ccbbe8e386` was
formally treated as unavailable and comparison-unrecoverable. It was not
reconstructed or replaced with another branch. New student-pressure branch
generation attempts were bounded at 300 seconds and 1,800 seconds after
resource-guard qualification. They produced no complete validated strict
improvement: the first 300-second attempt reached candidate extraction before the
hard wall, while the longer attempts were terminated during preparation or
before a worker payload was returned. The optional utilization-only branch
attempt likewise produced no complete validated branch. Two additional
300-second qualification runs retained bounded worker phase history and
observed repeated CP-SAT, extraction, full-model validation, and candidate-
processing phases, but no strict substantive improvement was adopted. A final
qualification run reached candidate validation before its hard wall; no
immediate branch was written because no adopted candidate was observed.
An independent `student_repair_r8_only` control then ran the live
`targeted_r8_s2` family for the full 1,800-second supervised wall. It observed
46 bounded attempts and repeated model construction, CP-SAT, and validation;
the final observed neighborhood probes were `infeasible`, but the outer policy
was hard-wall terminated before returning a complete policy payload. It
produced no strict adopted improvement or branch. That local infeasibility
evidence is not a proof of global infeasibility.

Three matched long baseline cells were then run from the immutable baseline
state, each with eight workers, the balanced profile, and an 1,800-second
parent-side hard wall. Adaptive terminated at 1,800.207 seconds with the
worker last observed in CP-SAT; stateless-role terminated at 1,800.113 seconds
with the worker last observed in model construction; fixed-cycle terminated
at 1,800.198 seconds with the worker last observed in CP-SAT. None returned a
worker payload or adopted an improvement. Every cell retained the validated
`37,596` incumbent with 10,635 assignments and zero unmet requests, and every
worker tree was descendant-clean after supervision. These are unresolved
operational observations, not evidence that the baseline is infeasible or
that any policy is superior.

An adaptive baseline repeat with immediate branch persistence did produce a
complete, full-model-validated strict improvement before parent termination.
The detached branch is `adaptive-derived.json.gz`, with branch id
`supervised-adaptive-balanced-derived-iteration-12`, source fingerprint
`f2e945f268314542f37667775a15be46d3db2a6aaa75f47142ac7ca5d27b7631`, and
substantive value `37,128` (from `37,596`). It retained `10,635` assignments,
zero unmet required requests, and all `310` special commitments. The parent
revalidated the branch against the current DTO and full model before exposing
it. The canonical benchmark and historical lineages remained unchanged.

Matched derived-state adaptive, stateless-role, and fixed-cycle cells were
then launched from that exact branch. All three retained the complete,
full-model-validated `37,128` incumbent and returned no authoritative adopted
candidate before their bounded wall. The adaptive and stateless cells ended at
approximately `1,800` seconds. The fixed-cycle cell reached the same bounded
search boundary; its parent reported `7,609.98` seconds only because the host
entered sleep during the run, as confirmed by Windows power events. This is
not solver-quality evidence and does not indicate a supervision defect. A
matched R2 follow-on was then run from the derived branch: the first attempt
was resource-guard terminated at `140.02` seconds during a transient memory
dip, and a clean retry ran six observed R2 attempts for `3,600.27` seconds.
It found no validated improvement and retained `37,128`.

The derived adaptive worker's bounded phase history did observe three
complete, full-model-validated/adopted candidates at `37,002`, `36,990`, and
`36,984`, selected respectively through targeted utilization `R32/S6`, grade
`G12`, and targeted utilization `R64/S6`. The parent process did not return an
authoritative policy payload and no durable branch was emitted for those
candidates, so they are retained as non-authoritative worker observations only;
the externally retained derived incumbent remains `37,128`.

The observed adaptive quality curve for that worker was:

| Cumulative worker time | Operator | Best observed substantive |
| ---: | --- | ---: |
| start | derived incumbent | 37,128 |
| 1,297.83 s | targeted utilization R32/S6 | 37,002 |
| 1,487.45 s | grade-bounded G12 | 36,990 |
| 1,748.68 s | targeted utilization R64/S6 | 36,984 |

These values are worker-phase observations only; they are not promoted
branches because the parent payload was unavailable.

A separate R2 follow-on from the stronger immediately persisted stateless branch
at `37,098` was also run. It was resource-guard terminated at `1,905.04`
seconds during CP-SAT when available memory fell to `809,594,880` bytes below
the 1 GiB study floor. It found no candidate and retained the complete
`37,098` incumbent; this is bounded resource evidence, not an R2 infeasibility
proof.

An additional fixed-cycle follow-up from that same `37,098` branch kept the
1,800-second policy budget but allowed a separate 2,100-second parent wall for
finalization. It was stopped during preflight because available memory was
`983,846,912` bytes, below the 1 GiB safety floor; no model or solver phase ran.
This is a resource-availability observation only.

The repeat cells also used immediate worker persistence. The adaptive repeat
produced a complete, full-model-validated detached branch at `37,116`, and
the stateless-role repeat produced one at `37,098`, although neither parent
returned an authoritative policy payload before termination. The fixed-cycle
repeat was resource-guard terminated at `1,610.97` seconds when available
memory fell to `1,049,546,752` bytes against the 1 GiB study floor. The
stateless repeat was resource-guard terminated at `1,133.89` seconds during
candidate validation when available memory measured `1,048,657,920` bytes.
All worker trees were cleaned up. These branch artifacts strengthen the
observed ability of adaptive and stateless policies to find improvements, but
the incomplete fixed/stateless parent runs and lack of matched R2 follow-ons
from the new branches mean that policy ranking and downstream-basin behavior
remain unresolved.

The study therefore establishes a valid new derived state and exercises the
derived controls, but it does not establish adaptive-promotion readiness,
policy ranking, or downstream-basin behavior. Its durable manifest and result
artifacts are retained in
`benchmarks/student_assignment/adaptive-promotion-v2-supervised-20260827/`;
the next valid study must obtain clean, comparable derived-state policy-cell
outcomes and matched downstream behavior before stronger policy ranking; an
immediately persisted branch is useful evidence but does not replace those
comparisons. The supervisor
retains bounded phase history and immediate diagnostic branch persistence;
neither mechanism changes ordinary production scheduling.

## Initial calibration evidence

The matched diagnostic controls are defined by policy selection, not by a
second solver implementation: R2-only is a one-element fixed cycle containing
`r2`; student-repair controls use a caller-selected targeted family such as
`targeted_r8_s2`; utilization-only controls use a caller-selected utilization
family; fixed-cycle controls use an explicit ordered tuple of existing
operators; and stateless-role controls use the current role signals with all
history-derived learning cleared. Every control shares the adaptive runner's
CP-SAT execution, full-model validation, strict-improvement adoption, and
complete-incumbent retention.

The first clean target-scale comparison used the detached v2 artifact with
input fingerprint `c07c77d0aa077a3e72240f27644d86b8a1a4faecb2f72a900aacc3fcb792d28a`
and the validated semantic source seed containing `10,945` required groups.
The initial incumbent was materialized through the existing operator-session
boundary and validated with the unchanged full model: `10,635` assignments,
zero unmet requests, and complete status.

With one bounded attempt, eight workers, and the same 30-second session budget,
the adaptive policy selected `targeted_utilization_r16_s4` from current
utilization pressure. Its external operation took `46.635` seconds, returned
`UNKNOWN` without a candidate, and retained the complete incumbent. A matched
R2-only control took `68.721` seconds externally and likewise returned
`UNKNOWN` without a candidate. Both controls preserved the same assignments,
zero unmet requests, and the same objective components. The external time is
larger than the nominal CP-SAT slice because model construction and cleanup are
inside the diagnostic operation boundary.

This is capability and resource evidence, not a promotion result: it is one
target state, one attempt, and no strict improvement. A separate specialized
control was not counted because its clean-process seed validation failed before
the operator probe completed. More repeated target states and matched static
controls are required before concluding that adaptive allocation is better than
a fixed policy.

The matched-control execution seam was then verified with fresh clean-process
target-scale controls using the same detached input and validated source seed.
The R2 fixed-cycle control took `34.422` seconds externally for one bounded
attempt and returned `UNKNOWN` without a candidate; the stateless-role control
selected `targeted_utilization_r16_s4`, took `34.135` seconds, and likewise
returned `UNKNOWN` without a candidate. Both retained the complete incumbent
with `10,635` assignments, zero unmet requests, and weighted v2 components of
`10,272` section utilization, `462` semester balance, `3,216` difficulty, and
`23,646` category diversity. These controls establish that static policies now
use the same execution and validation path, but they are still single-attempt
observations and do not establish a promotion winner.

A subsequent fixed student-repair control using `targeted_r8_s2` also passed
seed validation and used the shared execution path. Its one bounded attempt
took `54.137` seconds externally, returned `UNKNOWN` without a strict
improvement, and retained the same complete incumbent and component values.
The earlier seed-validation failure remains historical excluded evidence; it is
not conflated with this later valid no-improvement control.

## Initial evidence gate

The first medium screening run used the v2 realistic quality-tradeoff
fixture. It produced a complete two-assignment incumbent and a complete final
result under a bounded shared diagnostic budget; a targeted S1 scope was
proven locally infeasible while preserving the incumbent. The policy and
session record remained deterministic and JSON-safe.

As a small run-to-run variation check, three repeated one-attempt medium
trials were run for each of adaptive, R2-only, and stateless-role controls.
All nine trials preserved the complete two-assignment incumbent and zero unmet
requests. Adaptive selected `targeted_r4_s1` in all three trials and returned
`INFEASIBLE`; R2-only selected `r2` in all three and returned `INFEASIBLE`; and
stateless-role selected `targeted_r4_s1` in all three and returned
`INFEASIBLE`. External trial times ranged from `0.2045` to `0.2290` seconds
for adaptive, `0.2153` to `0.2290` seconds for R2-only, and `0.1188` to
`0.2183` seconds for stateless-role. This is a repeatability sample, not a
quality-ranking claim: the fixture has too little student scope to exercise
the larger S2 and utilization controls meaningfully.

A separate solver-backed medium profile screen used the same mixed-grade
fixture with five counselor profiles. All five produced complete results. The
student-quality-heavy, difficulty/category-heavy, and sequence-heavy profiles
selected the student-pressure role; the utilization-heavy profile selected
the utilization role; and the balanced profile selected utilization because
the fixture's observed utilization signal exceeded its student-local signal.
The role signals were bounded to policy-only `[0, 1]` values, including when
rounded per-student pressure made the raw local share exceed one. Sequence
responsiveness is recorded as profile behavior only; applicable sequence
opportunities were present in this fixture, but no production conclusion is
drawn from this small screen.

A bounded budget screen on the same medium input used one, three, and six
second total windows for adaptive, R2-only, and stateless-role controls. Every
trial retained the complete `510`-assignment, zero-unmet incumbent. Short
windows ended as unresolved `UNKNOWN` attempts before validation; a longer
adaptive attempt reached the validation boundary but recorded
`validation_unknown` rather than adopting a candidate. This demonstrates that
the shared budget is an actual operational constraint and that unresolved
validation is preserved as evidence, not converted into infeasibility or a
quality result. These windows are calibration observations, not product
presets.

An additional matched control screen exercised two detached medium states with
the same one-attempt, one-worker, approximately two-second policy budget. The
small quality-tradeoff state retained its complete two-assignment incumbent:
adaptive and stateless-role selected `targeted_r4_s1`, fixed R2 selected `r2`,
and the fixed targeted control reported no eligible target for its requested
scope rather than constructing a candidate. The production-shaped
80-student state retained its complete `510`-assignment incumbent with zero
unmet requests under every
control. Adaptive and stateless-role selected `targeted_utilization_r16_s4`;
fixed targeted repair selected `targeted_r4_s1`, and fixed local descent
selected `r2`. All four attempts were unresolved `UNKNOWN` within the short
window and none was adopted. External operation times were approximately
`1.80`--`1.91` seconds on the 80-student state. This is evidence that the
policies share the same execution boundary and respond to state, not evidence
that adaptive allocation wins on quality.

One target-scale smoke run used the durable detached input with the canonical
source seed materialized under the v2 profile. It retained all `10,635`
assignments, zero unmet requests, and all `310` special commitments. The
corrected policy state selected ordinary `r2` because the observed global
utilization share (`0.2732`) exceeded the student-local share (`0.0078`). The
bounded R2 attempt returned `UNKNOWN` without a candidate and the complete
incumbent was retained. The reported CP-SAT wall time was approximately
`20.75s`, while external operation time was approximately `112.85s`; this
demonstrates that the current operator wrapper can exceed a nominal slice
through model construction and cleanup. That is an operational limitation to
measure before any production promotion, not a reason to weaken constraints
or treat `UNKNOWN` as infeasible.

The existing matched v2 static controls remain the primary target-scale
comparison evidence: weighted targeted R8/S1 reached `6,871`, weighted
targeted R8/S2 reached `6,869`, and ordinary R2/R4/R8 controls retained the
starting `6,875` in their bounded trials. Those results are documented in
`STUDENT_ASSIGNMENT_SEARCH_STRATEGY.md`; the single adaptive smoke run is not
being presented as a quality win.

## Current calibration roadmap

The current offline research order is:

1. measure matched static controls against the adaptive allocator;
2. measure student-pressure, utilization, and grade-escape role-specific
   productivity, resource cost, and stagnation across multiple states;
3. return to faster local operators after any successful grade escape;
4. consider full-school unrestricted escape only if evidence justifies it;
5. run production-promotion and policy studies only after these gates.

Heuristics may choose a student, pair, neighborhood, or grade to explore.
They must never authorize a candidate. CP-SAT plus unchanged full-model
validation remain the only authority.

## Target-scale session qualification gate

The reusable session primitives have now been exercised on the detached
Objective Semantics v2 production-scale input with one validated source
incumbent. Dynamic `r4_s1`, `r8_s1`, `r4_s2`, and `r8_s2` sessions each completed
three bounded attempts with complete, full-model-validated candidates. Their
final substantive values were respectively `37,578`, `37,572`, `37,470`, and
`37,470`, from a common starting value of `37,596`. Dynamic R2 returned
`UNKNOWN` without a candidate and retained the complete incumbent; it was not
classified as infeasible or locally optimal.

The source facts were unchanged across sessions: 1,400 students, 10,760
requests, 10,945 required groups, 10,635 assignments, zero unmet required
requests, and 310 fulfilled special commitments. The detached v2 input and
canonical checkpoint were not persisted or mutated. Fixed-target controls
completed as well, and a reused continuous `r8_s2` session reached the same
endpoint as three independent calls while taking materially less external
wall time. Peak process memory remained below 1 GB in the measured runs.

This closes the reusable-session qualification gate for diagnostic research,
not the production-promotion gate. Utilization-cluster and grade-bounded
families are now implemented as separate diagnostic capabilities. The current
evidence gate is matched cross-family static-versus-adaptive calibration; it
does not authorize production adaptive allocation or global search.

The first larger-neighborhood diagnostic is now implemented as an opt-in
utilization-cluster family. It uses the existing global section-utilization
pairwise penalty only as solver-neutral target-selection guidance. The
families are R16/S2, R16/S4, R32/S4, R32/S6, R64/S6, R64/S8, and R64/S10;
the selected scope is bounded to the operator's changed-student cap. Dynamic
sessions recompute the cluster after adoption and fixed sessions hold a
caller-supplied cluster. The guidance records are explicitly marked as
non-attributive, and every candidate still requires CP-SAT plus unchanged
full-model validation. Target-scale results and the decision not to promote
this family into ordinary scheduling are recorded in
`STUDENT_ASSIGNMENT_UTILIZATION_CLUSTER_SEARCH.md`.

## Matched target-scale promotion-readiness study (2026-08-26)

The first matched promotion-readiness screen used the authoritative detached
Objective Semantics v2 input fingerprint
`c07c77d0aa077a3e72240f27644d86b8a1a4faecb2f72a900aacc3fcb792d28a` and the
durable Stage 1 source seed fingerprint
`d5036a44e71d5a3b2a94eebe51d645bb4034179a0dd29537492ea81feda2e900`.
The canonical checkpoint was not mutated. Every retained state remained
complete, full-model validated, at `10,635` assignments, with zero unmet
required requests and `310` fulfilled special commitments.

The starting v2 substantive value was `37,596`. The values below are the
weighted Objective Semantics v2 substantive tier; the raw component values are
shown separately so that these results are not confused with the historical
v1 raw objective total.

| Policy | Budget | Final v2 tier | Adoption count | Attempts | Preparation / policy time | Peak tree working set |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| adaptive | 720 s | 37,590 | 1 | 4 | 825.155 s / 771.661 s | 1.74 GB |
| stateless role | 720 s | 37,596 | 0 | 4 | 809.401 s / 767.323 s | not recorded |
| R2 only | 720 s | 37,596 | 0 | 4 | 835.349 s / 767.353 s | not recorded |
| student repair only | 720 s | 37,596 | 0 | 6 | 805.897 s / 761.226 s | not recorded |
| utilization only | 720 s | 37,596 | 0 | 7 | 864.537 s / 799.872 s | not recorded |
| fixed cycle | 720 s | 37,278 | 4 | 6 | 776.342 s / 756.083 s | 0.94 GB |

Preparation includes detached seed materialization and validation. Policy time
is the allocator's external policy window, not just CP-SAT wall time. The
policy windows exceeded their nominal shared budgets by approximately `36` to
`80` seconds because the current diagnostic boundary does not interrupt model
construction and cleanup already in progress. These values are therefore
screening observations, not production time guarantees.

The final baseline-screen raw components were:

- adaptive: utilization `6,723`, semester balance `175`, difficulty `35,973`,
  category diversity `22,150`;
- fixed cycle: utilization `6,599`, semester balance `175`, difficulty
  `35,853`, category diversity `22,050`.

The remaining controls retained the starting raw components of utilization
`6,727`, semester balance `175`, difficulty `35,973`, and category diversity
`22,150`. The fixed-cycle result is the strongest simple control in this
screen; the one adaptive baseline run is not sufficient to establish a policy
win.

Two derived detached states were also tested for a longer matched diagnostic
window. The student-repair state was independently revalidated before each
trial and had source fingerprint
`e147beadd23c31a068acaa928cae3fb2fe5262ad6af92e695f5fa2ccbbe8e386`, starting
v2 tier `37,440`. Adaptive reached `37,002` after `14` adopted improvements in
`1,816.492` seconds of policy time; fixed cycle reached `37,026` after `8`
adopted improvements in `1,816.029` seconds. Both retained complete,
full-model-validated states. Adaptive selected a grade-bounded G12 escape
after repeated utilization/local stagnation and then returned to local
operators; the grade attempts produced validated improvements, but there was
no matched static grade control, so this is not grade-escape promotion
evidence.

The stateless-role trial on the same derived state is not a valid matched
1,800-second result. It reported a final retained tier of `37,320`, but its
external policy time was `9,362.483` seconds, including `7,562.483` seconds
of overrun. Its final attempt had zero CP-SAT attempts, zero CP-SAT wall time,
and no validation attempt, which localizes the overrun to work before a solver
attempt (such as model/setup or resource pressure), although the current
telemetry does not identify the exact operation. The result is retained as an
operational failure observation, not as quality evidence.

This study therefore closes the current screening step but does not authorize
adaptive search in ordinary scheduling. No finalist repeat set, target-scale
profile matrix, R2-derived branch, or full production-pipeline promotion run
was launched after the hard budget-boundary failure was found. Additional
target-scale comparisons require a reliable outer process/time boundary and
more complete matched detached states. The current conclusion is
**EVIDENCE REMAINS INCONCLUSIVE**: fixed cycle is the strongest simple
screening control, adaptive search is a useful research candidate on the
derived student-repair state, and neither is promotion-ready.

### Supervised calibration execution boundary

The detached calibration runner now has an optional pure-engine parent
supervisor. It launches one JSON-producing worker, clears stale output before
launch, records bounded phase history—including the selected role/operator and
its factual policy reasons/signals—plus process-tree resource facts, and
terminates the worker tree at a hard wall or configured resource guard. A
terminated worker never contributes a candidate; the already validated
starting incumbent is retained and the execution status is reported as an
operational fact. This protects long offline comparisons from model-construction
or native-solver overrun without changing ordinary student assignment,
CP-SAT constraints, objective semantics, or Stage 2 behavior.

The supervised protocol is versioned as
`student_assignment_adaptive_calibration_trial_v2` /
`adaptive-calibration-v2`. The parent performs the authoritative full-model
validation before launch and serializes a small prepared-incumbent artifact.
The worker rehydrates and fingerprint-checks that artifact and the immutable
branch, but does not repeat the same CP-SAT validation. This keeps the required
authority check while preventing duplicate validation from consuming the
policy comparison window. Parent preparation, worker setup, policy phases,
and serialization remain visible as separate timing facts; they are not
silently attributed to CP-SAT.

The target-scale diagnostic profile defaults to an `1,800`-second worker wall,
`5` seconds of termination grace, a `4 GiB` process-tree RSS guard, a `1,536
MiB` minimum available-system-memory guard, and `250 ms` polling. These are
execution-safety settings for offline experiments, not solver constraints or
schedule-quality semantics; callers may choose a stricter bounded profile for
screening.

### Supervised Objective Semantics v2 matched-policy closeout (2026-08-28)

The final matched-policy portion of study
`adaptive-promotion-v2-supervised-20260827` used the immutable detached
`mixed_grade_v2_production_shape` input with fingerprint
`c07c77d0aa077a3e72240f27644d86b8a1a4faecb2f72a900aacc3fcb792d28a`. The
population was 1,400 students, 10,760 requests, 10,945 required source
groups, 10,635 assignments, zero unmet required requests, and 310 special
commitments. The common source seed fingerprint was
`d5036a44e71d5a3b2a94eebe51d645bb4034179a0dd29537492ea81feda2e900`, with
substantive value `37,596`. All current trials used the balanced v2 profile,
eight workers, an 1,800-second policy budget, a 2,100-second parent wall,
immediate diagnostic branch persistence, and the study's 3.5 GiB process-tree
RSS guard with a 1 GiB available-memory floor. These are detached research
settings, not ordinary production scheduling policy.

The clean matched baseline scorecard was:

| Policy | Start | Final | Adopted | Attempts | Policy time |
| --- | ---: | ---: | ---: | ---: | ---: |
| adaptive | 37,596 | 37,584 | 2 | 9 | 1,858.27 s |
| stateless role | 37,596 | 37,248 | 10 | 11 | 1,817.28 s |
| fixed cycle | 37,596 | 37,386 | 2 | 11 | 1,859.56 s |

Each retained result was complete, full-model validated, had 10,635
assignments, zero unmet required requests, and 310 fulfilled special
commitments. The clean fixed-cycle run is the current post-protocol fixed
baseline. The older `37,302` fixed-cycle result remains historical solver
evidence only because it predates the canonical-fingerprint protocol fix.

The fresh adaptive trajectory was `37,596 -> 37,590 -> 37,584`. Its first
two utilization attempts (`targeted_utilization_r16_s4` and
`targeted_utilization_r16_s2`) returned no adopted improvement, followed by
Grade 12 attempts, two validated Grade 12 adoptions of six points each, and
R2 attempts that returned unresolved `UNKNOWN`. The fresh stateless trajectory
was `37,596 -> 37,584 -> 37,554 -> 37,536 -> 37,500 -> 37,458 -> 37,398 ->
37,356 -> 37,308 -> 37,272 -> 37,248`; it repeatedly selected
`targeted_utilization_r16_s4`. The fixed-cycle trajectory was
`37,596 -> 37,506 -> 37,386`, with a 90-point utilization-targeted adoption
and a later 120-point targeted R4/S2 adoption. The available telemetry shows
that adaptive changed roles after unsuccessful/unknown utilization and escape
attempts while stateless continued the same utilization family. It does not
provide a counterfactual proof that any particular history signal caused the
difference, so no adaptive coefficient or switching rule is changed on this
evidence.

The exact common derived branch was `37,128`, fingerprint
`f2e945f268314542f37667775a15be46d3db2a6aaa75f47142ac7ca5d27b7631`, with
10,635 assignments, zero unmet required requests, and 310 special
commitments. From that identical state, adaptive retained `37,128` after
nine attempts and stateless role retained `37,128` after sixteen attempts.
The fixed-cycle policy produced the strongest current detached branch:

    37,128 -> 36,990 -> 36,942 -> 36,912

It adopted three complete, full-model-validated improvements in 13 attempts
and took 1,827.22 seconds of policy time. Its final branch fingerprint is
`6722e64568fe30e19e9920cbeeb8389055f4a1c1584a2f545f96920f62392960`, and its
detached artifact is `derived-37128-fixed-current.json.gz`. A matched 3,600-
second ordinary R2 follow-on from that branch ran 23 probes, all unresolved
`UNKNOWN`, adopted no candidate, and retained `36,912`. `UNKNOWN` is recorded
as unresolved bounded search evidence; it is not a proof of an R2 local
optimum.

The common-branch component changes were utilization-driven. The fixed-cycle
branch ended at raw components utilization `6,359`, semester balance `175`,
difficulty `35,853`, and category diversity `22,050`. The other two common-
branch policies retained utilization `6,421`, semester `175`, difficulty
`35,973`, and category diversity `22,150`. The fresh stateless result also
demonstrates that repeated utilization search can be productive from the
original baseline, but these trials do not establish that one policy is a
universal winner from every incumbent state.

The stateless/common-state comparison therefore supplies clear state-
dependence evidence: stateless role selection was strongest from the fresh
baseline, while fixed cycle was strongest from the `37,128` derived state;
adaptive was not superior in either matched current result. The defensible
v2 closeout classification is **evidence remains inconclusive for policy
promotion, with state dependence observed**. Adaptive, stateless, and fixed
cycle remain diagnostic operators. No operator is production-wired by this
study, and no v1 endpoint is being reopened.

The study artifacts and manifest are the authoritative record for per-attempt
solver status, validation time, candidate counts, branch/conflict telemetry
where available, and process-resource samples. One stateless derived trial
ended with a final attempted-candidate validation error after retaining its
complete starting branch; that is an operational/protocol observation, not a
substantive quality result. The current study status is
`inconclusive_bounded_study`. Canonical benchmark inputs and prior lineages
remain read-only.

The next research boundary is deterministic policy calibration over the
already-implemented Objective Semantics v2 contract: distinguish operator
failure from role exhaustion, then test whether a state-aware hybrid can
retain simple-policy gains across incumbent states. Future adaptive policy
must remain separate from the question of what schedule is better:
normalized counselor-weighted objectives define quality, while operator
selection only chooses where CP-SAT searches. Grade-scoped global escape and
full-school unrestricted escape remain later diagnostic work. No
normalization, reweighting, adaptive tuning, or production promotion is part
of this closeout.

### Target-scale repeatability gate and supervision correction (2026-08-28)

The planned repeatability gate was started from the same v2 baseline source
fingerprint `d5036a44e71d5a3b2a94eebe51d645bb4034179a0dd29537492ea81feda2e900`
and substantive value `37,596`. The first stateless repetition was not policy
evidence: the parent supervisor reported `hard_deadline_terminated` only
after `7,206.024` seconds, never reached CP-SAT, and retained the validated
incumbent. Its last recorded worker phase was target preparation. This
exposed that a blocking process-tree telemetry call could prevent the polling
loop from enforcing its configured hard wall.

The pure-engine calibration supervisor now has a separate parent-side
watchdog that terminates the worker tree independently of telemetry sampling.
The hard wall remains an experiment safety boundary, not a scheduling rule or
solver objective. A focused regression test stalls telemetry sampling and
verifies prompt worker termination; the smoke trial terminated at `10.134`
seconds for a `10`-second wall and left no descendants.

After that correction, one clean repetition of each baseline control completed
under the matched `1,800`-second policy / `2,100`-second parent profile:

| Policy | Earlier single run | Clean repeat | Attempts | Adopted repeat improvements | Repeat total |
| --- | ---: | ---: | ---: | ---: | ---: |
| stateless role | `37,248` | `37,596` | 11 | 0 | `1,861.877` s |
| fixed cycle | `37,386` | `37,596` | 12 | 0 | `1,840.967` s |

Both clean repeats were complete, full-model validated, retained `10,635`
assignments, zero unmet required requests, and all `310` special commitments.
The earlier single runs had been complete and validated as well. Thus the
same source state and same policy can produce materially different quality
outcomes under the current eight-worker CP-SAT configuration. This is not
strong exact or directional policy repeatability; it is an unresolved,
inconsistent result dominated by solver/search variance. It is not enough to
attribute the earlier stateless/fixed difference to role allocation or to
justify a new state-aware hybrid policy.

The gate therefore stops before derived-state repetitions, target-scale hybrid
implementation, or production wiring. No adaptive coefficients, operator
portfolio, objective semantics, constraints, or ordinary scheduling behavior
were changed. The next valid research step is a separately bounded
repeatability study that controls or explicitly models this parallel CP-SAT
variance, followed only then by an operator-versus-role diagnosis. The
current v2 policy study remains `inconclusive_bounded_study`.
