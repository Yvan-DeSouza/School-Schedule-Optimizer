# Student Assignment Operator Characterization

## Status and evidence boundary

This document is the evidence catalog for diagnostic student-assignment search
operators. It is not a production optimization policy. Every candidate in the
underlying trials was required to come from CP-SAT and pass the unchanged
full-model validator before it could be adopted by a diagnostic session.
`UNKNOWN` is unresolved; it is not evidence of infeasibility or optimality.

The evidence below is separated by objective-semantics version. Raw v1 and v2
objective totals are not numerically comparable. Every future trial must record
the objective version, input fingerprint, source-incumbent fingerprint,
operator configuration, and resource configuration.

## Operator roles

| Role | Operators | What the role is intended to repair |
| --- | --- | --- |
| Local descent | `r2` | A nearby improvement from the current complete incumbent |
| Student-pressure repair | `targeted_r4_s1`, `targeted_r8_s1`, `targeted_r4_s2`, `targeted_r8_s2` | Student-local semester, difficulty, category, and sequence pressure |
| Section-utilization repair | `targeted_utilization_r16_s2`, `targeted_utilization_r16_s4`, `targeted_utilization_r32_s4`, `targeted_utilization_r32_s6`, `targeted_utilization_r64_s6`, `targeted_utilization_r64_s8`, `targeted_utilization_r64_s10` | Global pairwise section-utilization imbalance |
| Basin escape | `grade_bounded_g9`, `grade_bounded_g10`, `grade_bounded_g11`, `grade_bounded_g12` | A broader, grade-scoped escape while freezing students outside the selected grade |

The role is an evidence label, not an attribution change. In particular,
section utilization remains global and is not assigned to individual students.
Grade scope uses actual `Student.grade_level` facts carried by the detached
engine input, never a course catalog grade.

## Methodology

Matched trials must use:

1. one immutable input DTO and fingerprint;
2. one validated semantic source-decision incumbent and fingerprint;
3. one Objective Semantics v2 counselor profile;
4. the same CP-SAT worker and time configuration;
5. the same full-model validation boundary;
6. clean process isolation for expensive trials.

## Solver-variance study protocol

Study `adaptive-policy-variance-v2-20260828` is the separate lineage for
measuring CP-SAT transition variance. It must not be merged into a policy
ranking or treated as a production controller. Each trial records the
Objective Semantics version, input fingerprint, source-incumbent fingerprint,
operator and target scope, worker count, diagnostic random seed, time limits,
model size, CP-SAT status, solver time, external operation time, validation
status, candidate/adoption values, and changed source decisions.

The policy-selection function is deterministic for a given state, history, and
budget. CP-SAT transition behavior can still vary under parallel search:

```text
state + operator -> candidate / no candidate / UNKNOWN
```

When a candidate is adopted, it becomes a new incumbent and changes the next
policy state. This is trajectory amplification, and must be separated from a
claim that the policy itself selected a better operator.

The controlled target-scale cases use the immutable v2 input fingerprint
`c07c77d0aa077a3e72240f27644d86b8a1a4faecb2f72a900aacc3fcb792d28a` and
baseline source fingerprint
`d5036a44e71d5a3b2a94eebe51d645bb4034179a0dd29537492ea81feda2e900`.
The stateless first-divergence case is
`targeted_utilization_r16_s4` over students `(417, 360, 482, 25)`. With
seed `101`, three one-worker repeats all adopted `37,590` from `37,596`;
three eight-worker repeats adopted `37,578`, `37,590`, and `37,590`. Every
one of those candidates was complete and passed full-model validation. This
is exact one-worker repeatability and material eight-worker same-seed
transition variance.

The fixed-cycle first-utilization case is
`targeted_utilization_r64_s8` over students
`(417, 360, 482, 25, 480, 90, 175, 514)`. Three one-worker repeats adopted
`37,590` with the same four students and 18 source decisions. Three clean
eight-worker trials generated the same-quality `37,590` candidate but full
validation returned `UNKNOWN`, so they are not adoption evidence. The
records distinguish candidate generation from validated adoption and do not
turn validation uncertainty into infeasibility. A separate extended run did
eventually validate and adopt `37,590`, but full validation took approximately
`2,957` seconds and the operation approximately `3,083` seconds against a
nominal `600`-second session envelope. It is retained as an out-of-budget
validation observation, not clean repeatability evidence.

The present classification is:

**Single-worker control is reproducible; eight-worker parallel variance is a
primary source of transition and policy-endpoint noise for the selected
stateless case.**

This is sufficient to block a causal adaptive-versus-static policy ranking
and hybrid calibration. It is not evidence that one worker is universally
better, nor is it a global optimality claim. No medium control or seed sweep
was run because the checkout contains no detached medium target-shaped state
and the target-scale same-seed result already established the key variance
gate. A future controlled policy comparison must use explicit predetermined
replicate seeds, identical source states, identical validation boundaries,
and clean process isolation.

The variance CLI provides that process boundary through `--supervised`, which
uses the shared parent-side watchdog for the entire worker operation. This is
important at target scale because full-model validation can outlive the CP-SAT
probe even when CP-SAT itself is bounded. The first supervised fixed-cycle
recheck completed inside its `600`-second wall, generated a `37,590` candidate,
and correctly withheld adoption when the unchanged validator returned
`UNKNOWN` within its `60`-second boundary. A terminated worker is never treated
as an authoritative transition.

The pure-engine `operator_characterization.py` module defines the
`OperatorCharacterizationRecord` schema and aggregation functions. A record
contains global and role-specific gains, component deltas, target pressure,
candidate/validation status, first-improvement time, total external time,
solver statistics, resource facts, attempt history, stagnation classification,
and optional downstream-basin facts. It intentionally excludes raw schedule
state and cannot authorize persistence.

For student-pressure operators, role-specific pressure is the weighted sum of
the existing student-local v2 facts: semester load, difficulty, category
diversity, and applicable sequence preferences. For utilization operators, it
is the existing global pairwise utilization penalty and its normalized/weighted
facts. For grade escape, direct gain and the optional downstream local-search
gain are reported separately so they are not double-counted.

Stagnation labels are descriptive observations only:
`productive`, `diminishing_or_stagnant`, `stagnant_or_unresolved`, or
`unresolved`. No label claims a mathematical local optimum.

## Current mixed-grade v2 benchmark

The current reproducible mixed-grade study fixture is generated by
`build_mixed_grade_v2_fixture` in
`scheduling_engine/realistic_student_assignment_validation.py`. It is a
synthetic, production-shaped benchmark rather than real school data. It is
derived from the existing special-commitment medium fixture and adds actual
grade facts, explicit v2 scores, special commitments, online supervision,
half-semester sections, prerequisites, and mixed section/capacity interactions.
The reusable `apply_mixed_grade_v2_profile` helper applies the same DTO-only
profile to a detached input for an isolated target-scale screen.

For the 80-student screening shape:

| Fact | Value |
| --- | ---: |
| Objective semantics | `v2` |
| Grade composition | 20 each of Grades 9, 10, 11, and 12 |
| Requests | 513 |
| Sections | 308 |
| Online supervision sessions | 4 |
| Special commitment requests | 6 |
| Input fingerprint | `32b6893e7d86e6b3804d112930ed1b3b40a61ca4ec00e77bc8768e877f20934c` |

The fingerprint is generated from the semantic DTO and is expected to change
if the fixture or its grade facts change. No claim is made that this synthetic
distribution represents a real school.

The existing durable 1,400-student artifact remains a separate historical v1
benchmark. It predates the grade field and its originating fixture has all
students in Grade 12, so it cannot provide a mixed-grade comparison. It must
not be rewritten to add synthetic grade data.

## Evidence inventory

### Student-pressure operators: v2 target-scale evidence

The detached v2 target-scale study used one 1,400-student input, one validated
source seed, eight workers, and bounded continuous sessions. It established
capability evidence, not a universal performance guarantee:

| Operator | Starting value | Final value | Validated adoptions | Evidence |
| --- | ---: | ---: | ---: | --- |
| `targeted_r4_s1` | 37,596 | 37,578 | 3 | Complete validated repeated session |
| `targeted_r8_s1` | 37,596 | 37,572 | 3 | Complete validated repeated session |
| `targeted_r4_s2` | 37,596 | 37,470 | 3 | Complete validated repeated session |
| `targeted_r8_s2` | 37,596 | 37,470 | 3 | Complete validated repeated session |
| `r2` | 37,596 | 37,596 | 0 | `UNKNOWN`; incumbent retained |

The observed targeted improvements included utilization and, in S2 sessions,
student-local difficulty/category movement. The trials do not establish that
the strongest endpoint is caused exclusively by the operator's intended role.
Role-specific deltas must be captured from the characterization record for
future matched trials.

### Utilization operators: v1 historical evidence

The following results are v1 evidence and must not be compared numerically with
the v2 values above:

| Operator | Final v1 value | Session adoptions | Classification at the time |
| --- | ---: | ---: | --- |
| `targeted_utilization_r16_s2` | 65,019 | 3/3 | useful diagnostic family |
| `targeted_utilization_r16_s4` | 65,009 | 3/3 | useful diagnostic family |
| `targeted_utilization_r32_s4` | 65,007 | 3/3 | strongest measured utilization family |
| `targeted_utilization_r32_s6` | 65,009 | 3/3 | reference family |
| `targeted_utilization_r64_s6` | 65,007 | 3/3 | escalation reference |
| `targeted_utilization_r64_s8` | — | — | not promoted to target-scale trial |
| `targeted_utilization_r64_s10` | — | — | not promoted to target-scale trial |

The v1 study reported that the utilization metric remained global and that
R64/S6 did not outperform R32/S4 in the measured sample. The v1 evidence is
useful historical context, not a v2 characterization.

### Grade escape evidence

Small mixed-grade tests prove that:

- all four operator identities require the matching actual grade;
- a grade-bounded operator has no radius or changed-student cap;
- source decisions owned by outside grades remain fixed;
- selected-grade source decisions remain searchable;
- grade opportunity facts are solver-neutral;
- absent grade facts are rejected rather than guessed.

The durable v1 target artifact remains all-Grade-12 historical input and is not
rewritten. A separate durable
`scheduling_engine/benchmarks/student_assignment/mixed_grade_v2_production_shape/`
artifact now records the deterministic synthetic mixed-grade profile used for
the current target-scale screens: 350 students in each of Grades 9–12, with
the original complete source seed preserved by fingerprint. This is synthetic
provenance, not an adapter-produced school dataset.

Before the current bounded screens below, no target-scale mixed-grade
grade-escape comparison had been completed. The remaining evidence gap still
includes repeated per-grade success rates, time to first validated improvement,
downstream R2 basin effects, and resource distributions.

### Current target-scale mixed-grade diagnostic screens

To obtain a comparable cross-grade screen without changing the durable v1
artifact, the dedicated mixed-grade-v2 artifact was loaded through the existing
transparent readers. Its explicit v2 profile and deterministic actual-grade
facts (350 students in each grade) produce input fingerprint
`c07c77d0aa077a3e72240f27644d86b8a1a4faecb2f72a900aacc3fcb792d28a`; the
canonical v1 files were not rewritten.  This is synthetic provenance, not an
adapter-produced school dataset. The artifact manifest also binds the source
seed to fingerprint
`d5036a44e71d5a3b2a94eebe51d645bb4034179a0dd29537492ea81feda2e900`.

The following one-attempt screens used the same 10,635-assignment complete
source seed, eight workers, a 120-second outer diagnostic budget, and a
30-second CP-SAT probe.  The outer operation includes the current wrapper's
seed validation/model setup; it is not a pure CP-SAT timing.

| Operator | Result | CP-SAT seconds | External seconds | Candidate/adopted | Variables / constraints |
| --- | --- | ---: | ---: | --- | ---: |
| `targeted_r4_s1` | complete seed retained | 5.08 | 127.08 | no / no | 112,322 / 200,583 |
| `targeted_r8_s1` | complete seed retained | 5.07 | 104.81 | no / no | 112,322 / 200,583 |
| `targeted_r4_s2` | complete seed retained | 6.30 | 97.07 | no / no | 112,322 / 200,575 |
| `targeted_r8_s2` | complete seed retained | 5.87 | 96.58 | no / no | 112,322 / 200,575 |
| `targeted_utilization_r16_s2` | complete seed retained | 4.34 | 89.36 | no / no | 112,322 / 200,575 |
| `targeted_utilization_r16_s4` | complete seed retained | 4.86 | 122.09 | no / no | 112,322 / 200,559 |
| `targeted_utilization_r32_s4` | complete seed retained | 7.66 | 126.98 | no / no | 112,322 / 200,559 |
| `targeted_utilization_r32_s6` | complete seed retained | 4.68 | 106.20 | no / no | 112,322 / 200,543 |
| `targeted_utilization_r64_s6` | complete seed retained | 6.82 | 95.75 | no / no | 112,322 / 200,543 |
| `grade_bounded_g9` | complete seed retained | 30.36 | 130.80 | no / no | not emitted by zero-iteration summary |
| `grade_bounded_g10` | complete seed retained | 30.29 | 95.51 | no / no | 110,922 / 184,109 |
| `grade_bounded_g11` | complete seed retained | 30.28 | 114.60 | no / no | 110,922 / 184,107 |
| `grade_bounded_g12` | complete seed retained | 30.27 | 107.06 | no / no | 110,922 / 184,107 |

All screens preserved zero unmet requests.  “No candidate” means no strict,
full-model-validated improvement was adopted in this bounded one-attempt
screen; it does not prove that the incumbent is optimal.  The grade screens
provide capability/plumbing evidence, not a production policy or repeated
mixed-grade performance distribution.  R64/S8 and R64/S10 were intentionally
not promoted because the required smaller utilization families produced no
actionable strict improvement in this screen and historical v1 evidence did
not justify their target-scale promotion.

A two-attempt `targeted_r8_s2` continuation was also run from the same seed.
The shared outer budget allowed one attempt: CP-SAT returned a lower raw
candidate value of `37,590`, but full-model validation did not accept it, so it
was not adopted.  The complete `37,596` incumbent remained in place.  This
supplies an unresolved validation/stagnation observation, not evidence of a
usable improvement or of optimality.

### Promoted target-scale follow-up screens

The durable mixed-grade-v2 artifact was then used for a small number of
promoted single-attempt screens. These were intentionally selective: they were
not a full target-scale matrix and did not run the downstream R2 continuation.
Each used eight workers, the unchanged complete 10,635-assignment source seed,
and the existing full-model validation boundary.

`targeted_r8_s2` returned `OPTIMAL` in approximately 5.10 seconds of CP-SAT
time and produced a complete, full-model-validated/adopted candidate of
`37,590` from `37,596` (gain `6`). The change involved one student and one
source decision; utilization improved by `6`, while semester balance,
difficulty, and category diversity were unchanged. The external operation was
approximately `113.21` seconds, including setup and validation; full-model
validation took approximately `10.10` seconds. The model had `112,322`
variables and `200,575` constraints.

The promoted `targeted_utilization_r32_s4` attempt found a raw `37,584`
candidate, but full-model validation rejected it, so the complete `37,596`
incumbent was retained. This is an authority-boundary rejection, not a usable
improvement. A promoted Grade-12 attempt returned `UNKNOWN` after
approximately `55.15` seconds without a candidate; it retained the complete
incumbent. Two additional direct R8/S2 repeats reproduced the raw `37,590`
candidate for student `1068` and sections `210`/`213`, but both were rejected
by the unchanged full-model validator. Their CP-SAT times were approximately
`4.36` and `4.29` seconds, validation times were `7.63` and `7.07` seconds,
and external operation times were `69.95` and `66.30` seconds. This is
directional candidate-generation repeatability, but unresolved adoption
repeatability.

A Grade-9 repeat returned `UNKNOWN` after approximately `30.07` seconds of
CP-SAT time without a candidate. A repeat of the only previously productive
grade family, Grade 12, also returned `UNKNOWN` after approximately `30.48`
seconds without a candidate. At that earlier stage, no grade-escape branch
checkpoint or downstream local-return session was launched. These observations
justified keeping the characterization diagnostic and the adaptive decision at
NO-GO until the later endurance evidence was collected below.

### Validation-authority classification

The original two rejected R8/S2 records were collected before validator-status
telemetry existed. They record only `candidate_validated=false`, so they cannot
be classified retrospectively as hard-invalid, validation-unknown, or an
infrastructure error. They remain unresolved evidence.

The two fresh clean R8/S2 repeats run with the classified validator both
reproduced the semantic candidate `37,590` and returned validator status
`optimal`/classification `validated`; both were adopted. Thus current
candidate generation and validation are directionally repeatable, while the
historical unclassified records prevent an unconditional strong-repeatability
claim for the entire record set.

### Current target-scale endurance characterization

The later target-scale endurance study used the same detached v2 input and
validated `37,596` source seed, with eight workers, continuous session reuse,
full validation after every candidate, and no operational persistence. These
are diagnostic sessions; they do not replace the ordinary Stage 2 policy.

The R2 session ran ten bounded attempts and adopted all ten validated
improvements, reaching `37,506` from `37,596`. The attempt-cap stop is not a
local-optimum proof. It retained `10,635` assignments, zero unmet required
requests, and all `310` special commitments. Session wall time was
approximately `1,436s`; peak process memory was approximately `3.4GB` RSS.

The interaction-aware student-pressure sessions produced:

| Operator | Final value | Adopted | External seconds | CP-SAT seconds | Result |
| --- | ---: | ---: | ---: | ---: | --- |
| `targeted_r4_s1` | 37,566 | 4/4 | 101.1 | 11.6 | fifth scope attempt infeasible |
| `targeted_r8_s1` | 37,566 | 4/4 | 101.5 | 11.2 | fifth scope attempt infeasible |
| `targeted_r4_s2` | 37,440 | 5/5 | 114.9 | 12.6 | complete validated session |
| `targeted_r8_s2` | 37,440 | 5/5 | 110.7 | 11.2 | complete validated session |

The S2 paths included a genuine same-tier trade-off observation: one adopted
move worsened utilization while improving difficulty and category components.
This confirms that the current aggregate objective can trade components; it
does not establish that R8/S2 is better than R4/S2 on a broader sample.

The utilization-cluster endurance sessions also completed five validated
attempts each. Their final values were `37,554` (R16/S2), `37,554`
(R16/S4), `37,548` (R32/S4), `37,560` (R32/S6), `37,554` (R64/S6),
`37,506` (R64/S8), and `37,548` (R64/S10). External session times ranged
from approximately `110s` to `119s`; all retained the complete source
assignment state. R64/S8 was the strongest single observed utilization-cluster
endpoint in this bounded sample, but it is not a production recommendation.

### Target-scale grade-bounded endurance and basin return

One-attempt target-scale grade probes from the common `37,596` seed all
returned complete, validated candidates:

| Grade | Candidate | CP-SAT seconds | Changed students | Component result |
| --- | ---: | ---: | ---: | --- |
| 9 | 37,590 | 104.8 | 10 | utilization -4 |
| 10 | 37,488 | 34.3 | 1 | difficulty -38, category -100 |
| 11 | 37,590 | 77.7 | 11 | utilization -4 |
| 12 | 37,590 | 43.3 | 19 | utilization -4 |

Clean repeats reproduced the Grade-9, Grade-10, and Grade-12 objective/component
outcomes. Grade 11 reproduced the value and changed-student count but changed
the exact source-decision set, so its repeatability is directional rather than
exact. Grade 10 had three exact value/component outcomes at `37,488`.

A Grade-10 branch was then returned to ordinary R2 without rerunning upstream
stages. The R2 attempt returned `UNKNOWN` without an adopted candidate after
approximately `121s` of CP-SAT time; the complete `37,488` incumbent was
retained. This is unresolved local-return evidence, not proof that the grade
branch is a local optimum.

These runs establish target-scale capability and repeatability evidence for
bounded grade scopes, but not a 30-minute per-grade search curve, a complete
grade-opportunity correlation analysis, or a production grade-allocation
policy.

### Medium all-operator plumbing screen

An additional 80-student screen exercised every currently registered operator
family against the same mixed-grade v2 fixture and one complete source seed.
It intentionally used a one-second CP-SAT attempt cap per operator, with one
worker, to verify portfolio coverage and result handling without launching a
large experiment matrix. The initial result retained 510 assignments and
zero unmet requests. All 16 operator records were emitted; none produced a
validated/adopted strict improvement. Fifteen returned `UNKNOWN`, while
`targeted_r8_s1` returned `OPTIMAL` without improvement. External operation
times ranged from approximately 4.48 to 7.10 seconds because the wrapper
includes setup, candidate extraction, and full-model validation boundaries.

This screen is coverage and safety evidence only. The intentionally tiny
attempt budget is too short to compare operator quality, first-improvement
time, gain per minute, or stagnation behavior. It must not be used to rank
operators or to infer that a family is ineffective. The result does confirm
that every registered family can be represented by the characterization
schema and that an unresolved/non-adopted attempt leaves the complete
incumbent intact.

### Medium meaningful characterization screen

A subsequent sequential screen used the same 80-student fixture and one
complete seed, with one worker and a ten-second CP-SAT attempt cap. Unlike the
plumbing screen above, this cap was long enough for most bounded operators to
return a complete, full-model-validated strict improvement. The values below
are v2 weighted-substantive gains; role-specific gain is reported separately
where the role metric has different units.

| Operator | Status | Gain | Role gain | CP-SAT s | External s | Validated/adopted | Variables / constraints |
| --- | --- | ---: | ---: | ---: | ---: | --- | --- |
| `r2` | `UNKNOWN` | 0 | 0 | 10.188 | 19.540 | no / no | 10,907 / 17,473 |
| `targeted_r4_s1` | `OPTIMAL` | 426 | 408 | 1.520 | 11.989 | yes / yes | 10,987 / 18,668 |
| `targeted_r8_s1` | `OPTIMAL` | 426 | 408 | 1.800 | 12.521 | yes / yes | 10,987 / 18,668 |
| `targeted_r4_s2` | `OPTIMAL` | 450 | 432 | 2.557 | 13.220 | yes / yes | 10,987 / 18,665 |
| `targeted_r8_s2` | `OPTIMAL` | 450 | 432 | 2.426 | 12.191 | yes / yes | 10,987 / 18,665 |
| `targeted_utilization_r16_s2` | `OPTIMAL` | 114 | 6 | 1.763 | 9.834 | yes / yes | 10,987 / 18,658 |
| `targeted_utilization_r16_s4` | `OPTIMAL` | 114 | 6 | 1.633 | 9.883 | yes / yes | 10,987 / 18,644 |
| `targeted_utilization_r32_s4` | `OPTIMAL` | 114 | 6 | 2.132 | 11.929 | yes / yes | 10,987 / 18,644 |
| `targeted_utilization_r32_s6` | `OPTIMAL` | 114 | 6 | 2.515 | 11.023 | yes / yes | 10,987 / 18,630 |
| `targeted_utilization_r64_s6` | `OPTIMAL` | 114 | 6 | 2.011 | 8.617 | yes / yes | 10,987 / 18,630 |
| `targeted_utilization_r64_s8` | `OPTIMAL` | 114 | 6 | 3.052 | 10.555 | yes / yes | 10,987 / 18,616 |
| `targeted_utilization_r64_s10` | `OPTIMAL` | 114 | 6 | 2.971 | 10.990 | yes / yes | 10,987 / 18,602 |
| `grade_bounded_g9` | `UNKNOWN` | 0 | 0 | 9.998 | 16.585 | no / no | 10,907 / 17,863 |
| `grade_bounded_g10` | `OPTIMAL` | 0 | 0 | 6.512 | 15.385 | no / no | 10,907 / 17,861 |
| `grade_bounded_g11` | `OPTIMAL` | 0 | 0 | 8.053 | 16.756 | no / no | 10,907 / 17,860 |
| `grade_bounded_g12` | `OPTIMAL` | 12 | 12 | 3.954 | 9.499 | yes / yes | 10,907 / 17,861 |

The same screen repeated `targeted_r8_s2` three times and obtained gain `450`,
role-specific gain `432`, `OPTIMAL`, and full validation/adoption on all three
runs. CP-SAT times were 1.917, 1.855, and 2.571 seconds; external times were
9.882, 10.259, and 9.781 seconds. It also repeated
`targeted_utilization_r32_s4` three times and obtained gain `114`,
role-specific gain `6`, and validation/adoption on all three runs. CP-SAT
times were 2.210, 2.118, and 1.404 seconds; external times were 13.656,
11.613, and 9.561 seconds.

The utilization policy comparison used `targeted_utilization_r32_s4` with one
trial per policy. `top_individual`, `delivery_group_focused`,
`interaction_aware`, and `mixed` all returned `OPTIMAL`, adopted a validated
gain of `114`, and a role-specific utilization gain of `6`. External times
were 7.102, 8.604, 7.005, and 7.433 seconds respectively. This is promising
matched evidence, not a policy-selection result: it is one small fixture and
one trial per policy.

### Medium pressure and utilization state

The 80-student starting state had total weighted student-local pressure of
`418,572`. Seventy-nine students had non-zero pressure and 79 had a meaningful
movable opportunity. The top-1, top-2, top-5, and top-10 pressure shares were
approximately `4.90%`, `9.81%`, `24.33%`, and `48.45%`. This is a broad-pressure
state rather than a highly concentrated one, so it is useful for comparing
targeting policies but does not establish when targeting should be preferred.

The same state exposed 52 multi-section delivery groups with total pairwise
utilization penalty `2,299`. The five most pressured groups had penalties
`94`, `86`, `84`, `82`, and `80`; the interaction-aware four-student selection
reported optimistic leverage `616`. These are solver-neutral opportunity facts,
not feasibility claims.

For orientation only, the observed one-trial role-specific productivity was
approximately `1,960.67` points/minute for R4/S2, `2,126.16` for R8/S2,
`30.18` utilization points/minute for R32/S4, and `75.80` points/minute for
Grade 12. The units are not interchangeable and the sample is too small to
support a production ranking.

The screen confirms useful bounded behavior on the synthetic medium fixture,
including that R4 and R8 and S1 and S2 can produce different gains for the
student-pressure family, and that wider utilization radii can still produce a
valid result. It does not establish target-scale success rates, grade-escape
downstream value, or production allocation policy. The complete source
incumbent remained available in every non-adopted or unresolved case.

## Intentionally skipped promotions

The following experiments were not launched because the preceding evidence
did not justify their cost or would not have produced an interpretable result:

- No additional target-scale utilization families beyond the bounded endurance
  sessions were launched; the completed R64/S8 and R64/S10 runs remain
  diagnostic evidence and were not promoted into ordinary scheduling.
- A target-scale R4/S2 versus R8/S2 matched comparison on the durable
  mixed-grade artifact was not promoted after repeated R8/S2 candidates failed
  the full-model validation boundary. The medium matched comparison remains
  available, but it is not a substitute for target-scale evidence.
- Only the Grade-10 escape received a downstream local-return probe. It was
  run from a validated branch and retained that branch when R2 returned
  `UNKNOWN`; other grade returns remain unmeasured.
- Adaptive allocation is now implemented as a diagnostic-only policy layer.
  Matched static-versus-adaptive calibration remains an evidence gate; no
  production policy or switching threshold is implied by the implementation.

## Pressure concentration and target selection

The existing pressure ranking exposes total local weighted pressure,
non-zero-pressure student count, meaningful opportunity count, and top-1,
top-2, top-5, and top-10 shares. The utilization guidance exposes global
delivery-group pressure, optimistic leverage, and target-policy facts. The
characterization harness records these as state evidence and does not freeze
thresholds.

The student-target controls are:

- `v2_counselor_weighted_pressure`;
- `raw_local_penalty_control`;
- `deterministic_semantic_control`;
- utilization `top_individual`, `delivery_group_focused`,
  `interaction_aware`, and `mixed` policies where applicable.

Existing target-scale v2 trials found weighted and raw leading targets equal on
one input and the deterministic control weaker in one trial. That is not enough
to declare ranking superiority. Repeated matched mixed-grade trials are still
required.

## Time horizons and stagnation

The record aggregator reports median operation time, median time to first
validated improvement, total and role-specific gain per minute, and estimated
attempt counts for 1-, 5-, 10-, and 30-minute windows. These are empirical
descriptors. They must not be used as production allocation thresholds until
normal run-to-run variation and clean repeated trials have been measured.

For every repeated session, report attempt-level starting/ending role values,
adoption, total and role-specific gain, external time, CP-SAT time, validation
time, memory, and status. A run ending in `UNKNOWN` is an unresolved stop, not
a proven plateau.

## Capability-card and readiness rules

Capability cards are generated from aggregate evidence and must include the
operator role, trial count, success rate, first-improvement time, total and
role-specific gain/minute, useful attempt range, stagnation facts, unknown
rate, memory, follow-on evidence, and limitations. A card based on one trial
must say so explicitly.

The readiness matrix is descriptive:

| State signal | Evidence-supported action for a future controller |
| --- | --- |
| Student pressure is high and concentrated | Consider targeted student-pressure trials; do not assume S1/S2 or ranking superiority without matched evidence |
| Utilization pressure is high and actionable | Consider utilization-cluster trials; keep utilization global and use measured radius/cap evidence |
| Local operators repeatedly stagnate or remain unresolved | Consider a grade-scoped escape only after grade opportunity and downstream evidence are available |
| Candidate is partial, unvalidated, `UNKNOWN`, or not strictly better | Retain the complete incumbent and record an unresolved/non-adopted attempt |

This matrix does not select operators in production. The allocator remains
diagnostic-only.

## Current classification

The classification below reflects evidence strength, not implementation status:

- `r2`: useful local-descent reference; the target-scale endurance session
  adopted ten validated improvements but stopped at its attempt cap, so no
  local-optimum claim is made.
- Targeted R4/S1 and R4/S2: useful student-pressure candidates; additional
  mixed-grade repeated trials required.
- Targeted R8/S1 and R8/S2: strongest current v2 targeted candidates in the
  measured target-scale study; not promoted to production policy.
- R16/R32/R64-S6 utilization families: current v2 bounded screens were
  completed but produced no strict validated improvement; repeated sessions
  and policy comparisons remain required.
- R64/S8 and R64/S10: useful diagnostic utilization-cluster candidates in the
  measured bounded endurance sample; not production-retained.
- Grade 9–12 operators: current v2 bounded screens were completed on the
  synthetic mixed-grade target input, but repeated outcomes and downstream
  return-to-local evidence remain unresolved.

## Adaptive-calibration decision

**Diagnostic adaptive calibration is now in progress; production promotion
remains NO-GO.**

The portfolio is implemented and the offline policy can select among current
student-pressure, utilization-cluster, local, and grade-escape diagnostics.
The evidence is not yet sufficient to promote it: repeated matched policy
trials, role-specific target-scale metrics, full grade-opportunity correlation,
and downstream return-to-local evidence are still required. Calibration must
not mix v1 and v2 totals or memorize a benchmark-winning operator.

The current evidence gate is to run matched static controls and adaptive policy
replays first on medium detached states, then promote only informative policy
comparisons to target scale. Grade escape must return to local search before
further allocation is judged. Only after those records are complete should an
adaptive policy enter a separate production-promotion study.

The diagnostic runtime now executes adaptive, stateless-role, and caller-
supplied fixed-cycle selections through the same operator-session boundary.
This keeps CP-SAT, full-model validation, adoption, and incumbent retention
matched across policy controls; the selection policy remains diagnostic metadata
and is not used by ordinary student assignment.

## Matched target-scale calibration status (2026-08-26)

The first target-scale matched screen used one detached, revalidated
Objective Semantics v2 source state for all six policy controls. All controls
preserved a complete `10,635`-assignment state with zero unmet required
requests and `310` fulfilled special commitments. In the 720-second policy
window, fixed cycle reached `37,278` from `37,596`; adaptive reached `37,590`;
R2-only, student-repair-only, utilization-only, and stateless-role controls
retained `37,596` in their bounded screen. These are v2 weighted substantive
values; raw component values remain documented separately in
`STUDENT_ASSIGNMENT_ADAPTIVE_SEARCH.md`.

A longer derived-state comparison gave adaptive `37,002` versus fixed cycle
`37,026` from the same revalidated student-repair state. This is useful
directional evidence that stateful escalation and return-to-local behavior can
find additional validated improvements, but it is not a promotion result.
The matched stateless control exceeded its nominal 1,800-second budget by
approximately 7,562 seconds before its final attempt reached CP-SAT. Because
the historical diagnostic wrapper had no hard outer process deadline around
model construction/setup, that cell cannot be compared fairly with the
bounded adaptive and fixed-cycle cells. The detached calibration runner now
provides a separate supervised boundary with a hard wall and optional resource
guards; that later boundary does not retroactively make this historical cell
valid quality evidence.

The target-scale study is consequently **inconclusive**. Fixed cycle is the
strongest simple control observed in the baseline screen, adaptive remains a
research candidate, and ordinary scheduling remains unchanged. A subsequent
supervised target-scale recheck reached its `600`-second hard deadline without
a candidate and retained the complete starting incumbent, so the new boundary
improves operational safety but does not yet establish a promotion result.
Future comparisons must use semantically identical detached states under that
boundary.

### Supervised-boundary qualification results (2026-08-26)

The current detached target input was exercised again under the supervised
`adaptive-calibration-v2` boundary. The parent-side preparation now reports
benchmark loading, temporary branch materialization, and authoritative branch
validation separately; the worker rehydrates a prepared, fingerprint-checked
incumbent rather than repeating that validation. On the exact target input,
parent preparation was approximately `55.9` seconds for the adaptive trial
(`52.9` seconds of branch validation) and `58.6` seconds for the fixed-cycle
trial (`56.6` seconds of branch validation). Both workers exited normally and
cleanly, but their short `60`-second policy windows were consumed by operator
setup before a CP-SAT attempt was reached. Both retained the complete
`10,635`-assignment, zero-unmet incumbent at `37,596`.

Short hard-wall smoke trials for adaptive and stateless-role execution used a
`15`-second wall and a `2`-second cleanup grace. They terminated at
approximately `15.14` and `15.30` seconds respectively, with the last live
breadcrumb in `model_construction`, no solver attempt, no candidate accepted,
the validated starting source fingerprint retained, and no descendant
processes remaining. This proves operational containment and incumbent safety;
it is not evidence about policy quality.

The historical student-repair branch fingerprint
`e147beadd23c31a068acaa928cae3fb2fe5262ad6af92e695f5fa2ccbbe8e386` is not
present in the current checkout. A fresh `student_repair_only` regeneration
attempt from the frozen baseline used three targeted R4/S2 attempts under a
`300`-second policy window. CP-SAT was reached and the first two attempts were
fully validated, but no strict validated improvement was found; the final
attempt ended in a validation error after the shared policy budget was
consumed. Therefore the historical derived-state adaptive/fixed/stateless
comparison has not been recovered and must not be replaced by the baseline or
by this non-equivalent regeneration attempt.

The supervised boundary is consequently operationally qualified for bounded
experiments, but the promotion-readiness study remains incomplete: the first
fresh target adaptive-versus-fixed screen reached CP-SAT without finding a
strict improvement, and no fair stateless derived-state comparison exists.

A later matched fresh baseline screen used a `300`-second policy window, an
external `420`-second wall, eight workers, and the same canonical source
fingerprint. Adaptive completed in approximately `341.4` seconds of policy
time (`347.3` seconds worker time); fixed cycle completed in approximately
`344.8` seconds of policy time (`352.7` seconds worker time). Adaptive reached
three CP-SAT attempts and fixed cycle reached three; both produced complete
fully validated trial outcomes but no strict substantive improvement, so both
retained `37,596`, `10,635` assignments, zero unmet requests, and `310`
special commitments. These are valid bounded search observations, not
promotion results: the first two attempts were validated but non-improving and
the final attempt in each trial ended with CP-SAT `UNKNOWN` before validation.
