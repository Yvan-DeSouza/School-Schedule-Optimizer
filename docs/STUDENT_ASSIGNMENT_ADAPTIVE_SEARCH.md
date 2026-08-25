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

The current portfolio contains only existing local operators:

| Operator | Search scope | Role |
| --- | --- | --- |
| `r2` | ordinary radius 2, no student bound | local descent |
| `targeted_r8_s1` | targeted radius 8, one student | targeted repair |
| `targeted_r8_s2` | targeted radius 8, two students | targeted repair |
| `targeted_r4_s2` | targeted radius 4, two students | targeted repair |

Grade-bounded unrestricted search, full-school global escape, reinforcement
learning/bandits, adaptive objective weighting, and production operator
selection are not implemented. In particular, the allocator cannot change
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

## Initial evidence gate

The first medium screening run used the v2 realistic quality-tradeoff
fixture. It produced a complete two-assignment incumbent and a complete final
result under a bounded shared diagnostic budget; a targeted S1 scope was
proven locally infeasible while preserving the incumbent. The policy and
session record remained deterministic and JSON-safe.

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

## Deferred roadmap

The research order after the current diagnostic allocator is:

1. establish repeatable v2 operator baselines;
2. test the allocator on medium fixtures and then only promising target-scale
   candidates;
3. retain only evidence-backed policy behavior;
4. investigate student-targeted repair refinements;
5. investigate grade-bounded unrestricted escape while freezing students
   outside the selected grade and retaining the full model for everyone;
6. return to faster local operators after a successful grade escape;
7. consider full-school unrestricted escape only if evidence justifies it;
8. run production-promotion and policy studies only after these gates.

Heuristics may choose a student, pair, neighborhood, or grade to explore.
They must never authorize a candidate. CP-SAT plus unchanged full-model
validation remain the only authority.
