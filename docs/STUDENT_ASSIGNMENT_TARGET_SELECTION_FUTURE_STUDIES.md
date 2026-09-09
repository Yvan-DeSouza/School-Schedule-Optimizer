# Future R16/S4 target-selection and search studies

This document contains research designs only. None of the studies below was
run as part of the 2026-09-08 TOP-versus-interaction-aware qualification, and
none is production-wired. The current qualification was classified **C: TOP
and IA show state-dependent complementary value**. Its states were selected
from the previously analyzed 70-state TOP trajectory, so the next target
policy qualification must use genuinely fresh authoritative states if the
goal is state-aware generalization.

## Common contract

Unless a design below explicitly says otherwise, hold these controls fixed:

- R16/S4 (`targeted_utilization_r16_s4`), with radius 16 and a four-student
  changed-student cap;
- Objective Semantics v2 and the same counselor importance profile;
- current complete incumbent-derived hints;
- CP-SAT seed 101, eight optimization workers, one validation worker;
- a 300-second CP-SAT ceiling, an independently protected 180-second
  full-model validation allowance, and strict full-model validation;
- strict lower-v2-value adoption, unchanged hard constraints, unchanged model
  construction, and unchanged candidate authority;
- clean sequential processes, immutable source resets, complete attempt and
  resource telemetry, and sealed artifacts;
- quality claims based only on authoritative validated incumbents. A changed
  scope, candidate, hint, or shadow selection is not an alternative schedule
  outcome.

Every design requires a pre-registered contract, exact source and code
fingerprints, balanced process order, repeated cells, and a solver-free
analysis plan before execution. A study may be cancelled or classified
inconclusive when its minimum sample or operational-validity requirements are
not met.

## A. Bounded dynamic TOP-versus-IA continuation

Purpose: test whether allowing a selected target policy to continue after a
validated adoption improves the authoritative gain trajectory, without
confounding continuation with a new target heuristic.

Compare a fixed two-continuation cap with one separately named larger-cap
treatment, preferably cap 4. Use the same starting source states, target
policy, R16/S4 contract, and clean repeated processes. The ordinary selector
and current target policies remain unchanged; only the continuation allowance
is treatment. Record the full ordinary competition table before every forced
continuation so a continuation override can be separated from an independent
score winner.

The analysis must report adoption and non-adoption rates, productive streaks,
scope freshness, repeated-scope blocking, gain per attempt and wall minute,
component movement, and the cap-shadow force decision. It must not claim what
the unexecuted cap would have produced. A larger cap is not justified by one
long streak: require at least five post-cap evaluable states and the frozen
sample thresholds in the continuation analysis plan. Do not run this study
automatically from the current C result.

## B. Truly fresh-state state-aware target-policy qualification

Purpose: measure TOP versus interaction-aware targeting on authoritative
pre-states that were not used to select or label the all-70 discovery cohort.

Build states from a new trajectory or scenario, not by choosing additional
rows from the existing 70-attempt corpus. Freeze the state-construction rule
before seeing target-policy outcomes. Use at least three clean repeats per
policy and balance policy order within each state. Stratify only on
pre-state facts supported by the new source, such as utilization pressure,
remaining difficulty/category pressure, student-local pressure, search
maturity, and scope divergence. Do not fit new coefficients from a small
jackpot sample.

Primary outcomes are per-state gain distributions, authoritative adoption
rates, jackpot/upside distribution, changed-student and changed-decision
geometry, component movement, solver/validation time, and resource health.
Report both all-state results and prespecified sensitivity strata. The study
must not introduce a third policy, dynamic continuation, a hint treatment, or
an Objective Semantics change.

## C. Core-plus-near-tie rotation study (conditional)

Status: design only; the current evidence does not yet justify executing it.

Purpose: test the observational hypothesis that a stable two- or three-student
core plus one or two candidates from the prior rank-5-to-8 band can expose
jackpot-capable scopes after a low-yield exact scope.

Use only a new, separately named research policy and a matched current-TOP
control. Freeze the core-retention rule, rank band, near-tie width, recent
gain window, and scope-size rule before the study. Do not derive a coefficient
from the four historical jackpot labels. Require enough fresh states in each
prespecified regime to compare fresh versus repeated scopes, and retain the
current TOP scope as the policy baseline.

Report entrant ranks, leverage deltas, retained core size, recent exact-scope
gains, scope overlap, authoritative gains, candidate fingerprints, and all
five v2 component changes. The rank and structural features are targeting
guidance, not feasibility or quality objectives. A policy change is not
validated by selecting the hypothesized students; it requires a complete
candidate and independent full-model validation. If the preregistered
fresh-state evidence does not strengthen the hypothesis, explicitly drop it.

## D. Fixed-scope hint treatment study

Status: deferred until exact identity and target-local release accounting are
ready.

Purpose: determine whether hint changes affect search time or candidate
variance without changing target selection or destroying jackpot quality.

Hold source, exact target scope, R16/S4 operator, seed, worker count, and
validation fixed. Compare current complete incumbent-derived hints, no hints,
and target-release hints in independently repeated cells. Record exact
semantic variable identity, incumbent values, actual hint values, released
variables, candidate fingerprints, solver wall, validation wall, authoritative
gain, component movement, and resource facts.

Do not infer hint causality from the current TOP-versus-IA qualification:
current hint values were unchanged there. Do not add directional or oracle
hints until destination predictions and ranks are replayable. The full-model
validator and strict adoption boundary remain unchanged in every treatment.

## E. First-qualifying versus bounded within-scope refinement

Purpose: test the separate question raised by the same-scope +108 versus +114
attempt-27 candidates: after a qualifying candidate is found, does spending a
bounded amount of additional search within that exact scope improve the
validated v2 outcome?

Use the same authoritative source states, exact target scope, current hints,
seed, workers, and R16/S4 model in both treatments. The control stops at the
first qualifying candidate. The refinement treatment receives a frozen,
bounded post-qualification budget and a separately specified refinement
objective or acceptance rule. Count all refinement time inside the same parent
wall and independently validate every reported candidate.

Compare final authoritative value, time to first qualifying candidate, time
to final adoption, candidate-fingerprint diversity, component tradeoffs,
validation cost, and the fraction of refinements that strictly improve the
first candidate. Do not treat a different candidate as better without
validation, and do not assume that a utilization-oriented hint would preserve
the observed category/difficulty tradeoff.

## F. Matched 180-second versus 300-second duration qualification

Purpose: measure duration allocation for an already selected operator without
mixing it with persistence or target-policy selection.

Sample authoritative states and independently run 180- and 300-second
ceilings under repeated seeds or repeated eight-worker trials. Keep target
selection, scope, hints, Objective Semantics v2, validation, and adoption
fixed. Validate every returned candidate. Keep later trajectory decisions out
of the first-stage duration comparison; compare each fork from the same
starting state.

Report candidate-found and validated-adoption rates, time-to-return, gain,
candidate identity, component movement, near-ceiling UNKNOWN frequency, and
resource cost. Do not claim that a candidate observed at one duration would
or would not have appeared at another duration. The current qualification's
roughly 60--73 second solver walls are descriptive evidence only and do not
replace this matched fork.

## Promotion and execution boundary

None of these designs authorizes production promotion. Before any one is run,
select one scientific question, freeze its contract and analysis thresholds,
verify the source and code state, and create a new unique sealed lineage.
Do not combine target selection, continuation, hints, within-scope refinement,
and duration into one factorial study. Preserve the current Objective
Semantics v2 definition, hard constraints, validation authority, and
production wiring throughout.
