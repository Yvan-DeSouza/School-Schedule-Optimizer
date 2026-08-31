# Student Assignment Search Strategy

This document owns the overall distinction between production student
assignment and opt-in diagnostic search guidance. Objective definitions,
validation authority, and quality metrics are maintained in their specialized
documents.

## Current status

Objective Semantics v2 is the current quality contract for the diagnostic
student-assignment runs described here. It uses one canonical counselor
importance score from 0 through 10 and input-derived normalization for the
counselor-controlled soft metrics. The v1 R2/R4/R8 search study remains
historical evidence and is not reopened by the targeted-repair diagnostics.

## Student-targeted repair guidance

`scheduling_engine/student_assignment/search_guidance.py` ranks students from
the quality facts already produced for a complete candidate. It attributes only
student-local metrics:

- student semester-load balance;
- difficulty balance;
- category diversity; and
- applicable soft course-sequence opportunities.

Section utilization is a global pairwise section-load metric and is therefore
not assigned to an individual student. The ranking reports the same v2
normalized, counselor-weighted local penalty used by the aggregate objective.
It also reports a cheap opportunity signal: the number of non-zero local
penalty components plus unsatisfied applicable sequence opportunities. This is
guidance, not proof of mobility or improvement.

The ranking does not call CP-SAT, create assignments, modify the production
objective, or authorize a candidate. It is intentionally deterministic, with
student ID as the final tie-break.

`reconcile_student_quality_pressure` compares the summed student-local
weighted pressure with the aggregate v2 local components. Section utilization
is reported as an excluded global component, and independent integer rounding
is exposed as a delta rather than silently treated as exact equality.

## Targeted S1/S2 diagnostics

The targeted repair entry points in
`scheduling_engine/student_assignment/core.py` are diagnostic-only wrappers
around the existing substantive soft-tier probe:

- S1 selects exactly one student;
- S2 selects exactly two students;
- source decisions owned by every other student are fixed to the supplied
  validated incumbent;
- the unchanged full model, higher-priority fulfillment values, and strict
  aggregate v2 substantive-improvement requirement remain in force; and
- every returned candidate must pass the existing full-model validation before
  it can replace the diagnostic incumbent.

The target list is a search restriction, not a counselor rule and not an
assignment recommendation. Grade-bounded escape is implemented separately as
an opt-in diagnostic operator. The offline adaptive allocator can choose
among these existing diagnostic families, but no adaptive controller or
global unrestricted operator is used by ordinary production assignment.

The ordinary two-stage path also retains a complete validated Stage 1 seed
when a later bounded optimization pass returns a weaker candidate or becomes
inconclusive. This protects completeness without claiming that the lower
objective tier was optimized.

## Soft sequence versus hard prerequisite

`CoursePrerequisite` remains a hard same-year sequencing rule. When it applies,
the prerequisite must be in Semester 1 and the dependent course in Semester 2;
soft importance cannot reverse it. `CourseSequencePreference` is separate
non-binding evidence: when both courses are applicable, Semester 1 for the
earlier course and Semester 2 for the later course is rewarded by the existing
v2 normalized objective, but a legal reverse order remains possible when the
other hard constraints and higher-priority objectives require it.

Applicability is based on both courses being present in the student's target-
year request/fixed context, not on the preferred orientation already being
available. Thus a student whose only legal order is the reverse still produces
one unsatisfied soft opportunity rather than silently removing the opportunity
from the denominator.

Sequence edges are part of the semantic student-assignment input fingerprint,
so changing the configured directed relation causes immutable-run drift
detection rather than silently changing the meaning of an existing run.

## Authority and future work

CP-SAT remains the sole assignment authority. Targeted guidance may choose
which diagnostic neighborhood to inspect, but it may never authorize a result.
The full-model validator and existing approval/snapshot rules remain the
durable boundaries.

The target-scale evidence obtained so far is diagnostic, not a production
policy. On the durable 1,400-student input, using the unchanged validated v1
source seed under v2 semantics, the highest-pressure S1 probe (one student,
radius 2) found a complete validated candidate at raw section-utilization
penalty `6,871`, down from `6,875`. A matched S2 probe (the two highest-pressure
students, radius 4) found a complete validated candidate at `6,869`. Both
retained `10,635` assignments, zero unmet requests, and all `310` special
commitments. The isolated calls took approximately `157.7` seconds for S1 and
`148.4` seconds for S2, including model construction, seed validation, CP-SAT,
and finalization. These are target-scale capability results, not evidence that
the ranking is globally optimal or that an adaptive controller should select
these students in production.

The current separately scoped research increment is offline calibration of the
implemented operator portfolio against matched static controls. It must report
policy choices, resource cost, repeatability, and validation outcomes before
any promotion decision. Full-school global escape remains deferred. The
diagnostic operators do not change
the production objective, counselor policy, approval behavior, or production
operator allocation.

## Objective semantics versus search strategy

Objective Semantics v2 defines what constitutes a better complete schedule:
the unchanged hard model plus the normalized counselor-weighted objective.
Search guidance defines only where a diagnostic probe should look. A ranking,
control sample, or pair heuristic cannot authorize a candidate. CP-SAT and the
full-model validator remain authoritative.

## Search-operator taxonomy

The current diagnostic vocabulary is:

- `R2`, `R4`, and `R8`: source-decision neighborhood radius.
- `S1` and `S2`: maximum one or two changed students.
- `grade_bounded_g9` through `grade_bounded_g12`: unrestricted source
  decisions for one selected actual student grade, with all other student-
  owned source decisions frozen.
- ordinary: CP-SAT chooses the changed students inside the bound.
- targeted: guidance selects the students before CP-SAT search and all other
  student-owned source decisions are frozen.

The targeted wrappers are `run_student_assignment_targeted_s1_diagnostic` and
`run_student_assignment_targeted_s2_diagnostic`. The matched ordinary control
is `run_student_assignment_ordinary_repair_diagnostic`. All are diagnostic
entry points; none is called by ordinary production assignment.

## Ranking policies and counselor profiles

The evidence module defines three explicit policies:

1. `v2_counselor_weighted_pressure` uses normalized local penalties and the
   canonical counselor scores.
2. `raw_local_penalty_control` ranks comparable raw local penalties without
   normalization or counselor weighting. It exists only as an experiment.
3. `deterministic_semantic_control` selects a reproducible hash-ordered sample
   independent of quality pressure.

The balanced target-scale profile used in the first matched study assigned
score `6` to all five v2 components. Difficulty-heavy, sequence-heavy,
semester-heavy, category-heavy, and utilization-heavy profiles are supported
by the same ranking API. On the current target input, sequence opportunities
were absent and the leading students remained ordered the same under the
tested profiles; this is a property of this input, not a claim that profiles
cannot change ranking. Focused unit coverage proves a difficulty-versus-
sequence profile can change ranking when both opportunities exist.

## Structured experiment records

`search_experiments.py` defines `StudentSearchExperimentRecord`, a compact
JSON-safe record containing the input and source-seed fingerprints, profile,
ranking policy, operator scope, candidate/validation/adoption facts, objective
vectors and components, changed students/sections, solver timings, model
size, search statistics, stopping reason, and process-tree resource facts.
Records are diagnostic artifacts and are not persisted as scheduling state.

## Target-scale weighted/control evidence

The matched v2 experiment used one detached input and one validated source
seed:

| Fact | Value |
|---|---|
| Input fingerprint | `faa7a016b553d662821cb1247bb70fed8b9021dc289a6b406ff9f7c993b0d280` |
| Source seed fingerprint | `54a1dc6324fcdd6055f5c5f5dc4a9f0b3c417d4d9520f5ae19cd124e3f3acd2f` |
| Students / requests / required groups | `1,400 / 10,760 / 10,945` |
| Sections / assignments / unmet | `317 / 10,635 / 0` |
| Special commitments | `310` |
| Profile | all five scores `6` |

The baseline section-utilization penalty was `6,875`; semester balance was
`175`, difficulty was `35,973`, and category diversity was `22,150`.

| Operator/policy | Trials | Candidate utilization | Complete/validated | Typical total time |
|---|---:|---:|---|---:|
| Weighted R4/S1 | 1 | `6,871` | yes | `53.1s` |
| Weighted R8/S1 | 3 | `6,871` | 3/3 | `54.4–58.2s` |
| Weighted R4/S2 | 1 | `6,869` | yes | `53.4s` |
| Weighted R8/S2 | 3 | `6,869` | 3/3 | `53.9–54.9s` |
| Raw R8/S1 | 1 | `6,871` | yes | `52.9s` |
| Deterministic R8/S1 | 1 | `6,873` | yes | `51.7s` |
| Deterministic R8/S2 | 1 | `6,873` | yes | `53.8s` |
| Ordinary R4/S1 | 1 | `6,875` retained | no improvement | `111.6s` |
| Ordinary R8/S1 | 1 | `6,875` retained | no improvement | `107.7s` |
| Ordinary R4/S2 | 1 | `6,875` retained | no improvement | `102.4s` |
| Ordinary R8/S2 | 1 | `6,875` retained | no improvement | `44.3s` |

The targeted model contained approximately `110,922` variables and `186,829`
to `186,837` constraints. The ordinary control contained `112,322` variables
and `189,646` constraints because of the unrestricted changed-student
indicators. Targeted probes generally used about `0.81–0.82 GB` peak process-
tree RSS; ordinary controls reached approximately `0.87–0.89 GB` in the
longer trials.

The weighted and raw policies selected the same leading student and pair on
this particular input, so this study does not prove weighted ranking
superiority over raw ranking. The deterministic control selected students
`376` and `286` and produced the weaker `6,873` result. The strongest weighted
S2 candidate changed one student-owned source decision: student `1068`,
request `8515`, section `210` to section `213`.

The first target-scale pair-selection comparison used the same weighted first
student (`1052`). Top-two pressure selected `(1052, 1068)` and reached `6,869`.
The interaction-aware partner selector chose `(1052, 1072)` and reached
`6,871` in one matched R8/S2 trial. This is preliminary evidence that static
interaction is useful as a measurable alternative but is not automatically
better than top-two pressure; repeated pair-policy trials belong in the next
adaptive study.

## Repeatability and promotion classification

Within the balanced profile and fixed input, weighted R8/S1 and R8/S2 were
strongly repeatable in the measured sample: three clean trials each found a
complete validated candidate with the same substantive endpoint and same
affected student (`1052` for S1 and `1068` for S2). This is strong repeatability
for this input/profile, not a universal guarantee.

Current evidence classification:

- Targeted R4/S1: useful escape candidate; one successful trial.
- Targeted R8/S1: candidate for the next adaptive portfolio; three matching
  successful trials, but no demonstrated advantage over targeted R4/S1.
- Targeted R4/S2: useful escape candidate; one successful trial.
- Targeted R8/S2: candidate for the next adaptive portfolio; three matching
  successful trials and the strongest measured targeted endpoint.
- Raw targeted control: reference only; it matched weighted selection here.
- Deterministic targeted control: reference only; weaker endpoint in the one
  measured trial.
- Ordinary R4/S1, R8/S1, R4/S2, and R8/S2: bounded controls only. They found no
  improvement in these trials, but are not declared infeasible.

The evidence supports designing the next adaptive-allocation study, but does
not authorize enabling an adaptive allocator in production. The next study
must include repeated matched controls, profile-specific cases with real
sequence opportunities, and an explicit gain-per-minute/resource policy.

## Contributor contract for search operators

A new operator must specify its scope, source-decision freedom, student-bound
semantics, objective authority, full-validation requirement, timeout and stop
classifications, telemetry, benchmark identity, and production-promotion
boundary. A new heuristic must specify its input facts, deterministic ordering,
attribution semantics, control comparison, and the fact that it cannot
authorize a candidate.

## Historical adaptive terminology

Earlier quality documentation uses “adaptive bootstrap” and “adaptive VNS” for
historical v1 diagnostic experiments. Those names describe bounded v1 search
behavior and must not be confused with the v2 adaptive operator allocator.
The v2 allocator now exists as a diagnostic-only policy documented separately:
it may choose among targeted repair, retained local, utilization-cluster, and
grade-bounded operators, but it must never change objective semantics,
authorize a schedule, or bypass full validation.

Grade-bounded unrestricted search is now a separately characterized
diagnostic operator and is available to the offline adaptive allocator's
portfolio. It remains outside ordinary production scheduling and cannot
authorize a candidate. Full-school unrestricted escape remains deferred until
evidence justifies it.

## Objective Semantics v2 adaptive allocator

The diagnostic-only allocator is implemented in
`scheduling_engine/student_assignment/adaptive_search.py` and
`adaptive_runtime.py`. It exposes R2, targeted student-pressure, utilization
cluster, and grade-bounded families using explicit v2 pressure,
counselor-intent, history, and shared-budget signals. It is not called by
ordinary production assignment and does not alter the objective or hard-model
contract. Each attempt still goes through CP-SAT and the existing full-model
validator, and only a strict validated improvement is adopted. Quality and
student ranking are recomputed after adoption. Unknown and proven infeasible
outcomes remain distinct. The policy state and session records are documented
in [`STUDENT_ASSIGNMENT_ADAPTIVE_SEARCH.md`](STUDENT_ASSIGNMENT_ADAPTIVE_SEARCH.md).

This is an experiment boundary, not a production recommendation. Static
R2-only, targeted-operator-only, fixed-cycle, and adaptive sessions must be
compared on identical v2 input and source-seed fingerprints before any future
promotion decision. Grade-bounded operators are a separate diagnostic family;
unrestricted global search remains deferred.

## Continuous operator-session boundary

The mature-R2 continuation has now been generalized into the reusable pure-
engine diagnostic entry point
`run_student_assignment_operator_session_diagnostic`. It supports `r2`,
`targeted_r4_s1`, `targeted_r8_s1`, `targeted_r4_s2`, and `targeted_r8_s2`.
The session builds one immutable model/context boundary and performs multiple
strict-improvement probes without returning to ordinary Stage 2 between
attempts. Each attempt still clones the model, is bounded, and must pass the
unchanged full-model validator before adoption.

Targeted sessions explicitly declare `dynamic` or `fixed` target policy. A
dynamic policy retargets from current validated quality pressure after an
adoption; a fixed policy holds the supplied student scope. Target selection is
search guidance only and cannot create or authorize a schedule. Session facts
record target history, attempts, CP-SAT/validation time, total deadline
elapsed time, external overrun, resource telemetry, and explicit unresolved or
proven stop reasons.

This is a diagnostic capability and a characterization boundary. It does not
enable adaptive search in ordinary production scheduling, alter Objective
Semantics v2, introduce full-school global search, or replace CP-SAT and
full-model validation as the authority. Comprehensive portfolio
characterization and adaptive-controller calibration remain separate
evidence-gated studies.

## Target-scale reusable-session qualification (Objective Semantics v2)

The reusable continuous-session boundary was qualified against the detached
production-scale v2 input, not by rerunning placement or named-teacher
assignment. The input fingerprint was
`c07c77d0aa077a3e72240f27644d86b8a1a4faecb2f72a900aacc3fcb792d28a`, and the
validated source-seed fingerprint remained
`d5036a44e71d5a3b2a94eebe51d645bb4034179a0dd29537492ea81feda2e900`. The
source facts remained 1,400 students, 10,760 requests, 10,945 required source
groups, 10,635 assignments, zero unmet required requests, and 310 fulfilled
special commitments. These are diagnostic-session facts only; the detached
input and canonical benchmark checkpoint were not mutated.

The matched target-scale runs used eight workers, a 180-second session budget,
three attempts, 30 seconds per CP-SAT attempt, and 20 seconds for each
single-worker full-model validation. The starting v2 substantive value was
37,596. Results were:

| Family | Final value | Attempts/adoptions | Target behavior | Session wall time | Peak working set |
| --- | ---: | ---: | --- | ---: | ---: |
| `r2` | 37,596 | 1 / 0 | dynamic, no candidate | 57.0 s | 0.88 GB |
| targeted `r4_s1` | 37,578 | 3 / 3 | dynamic, one target | 73.6 s | 0.83 GB |
| targeted `r8_s1` | 37,572 | 3 / 3 | dynamic, one target | 70.3 s | 0.83 GB |
| targeted `r4_s2` | 37,470 | 3 / 3 | dynamic, retargeted after adoption | 71.2 s | 0.83 GB |
| targeted `r8_s2` | 37,470 | 3 / 3 | dynamic, retargeted after adoption | 74.6 s | 0.83 GB |

Every adopted candidate was complete, passed full-model validation, retained
10,635 assignments, retained zero unmet required requests, and fulfilled all
310 special commitments. Section-utilization improvement appeared throughout
the targeted trajectories. The S2 trajectories also found a validated
intermediate move changing the difficulty and category components while
preserving the semester component; those component changes are part of the
existing v2 aggregate objective, not a new weighting rule. The two-student S2
families both followed the same target transition, `(1052, 1068)` to
`(1052, 1072)`, and produced the same final value in this bounded sample. The
R2 result remained a complete retained incumbent but returned `UNKNOWN` without
an improvement, so that is unresolved search evidence rather than a proof of
local optimality.

Fixed-target controls also completed without leakage: fixed `r8_s1` ended at
37,578 and fixed `r8_s2` ended at 37,464 in their respective single trials.
Those runs are useful control evidence, not a claim that fixed targeting is
globally superior; the sample is too small and CP-SAT uses parallel search.

The session-reuse comparison used the same fixed `r8_s2` scope and source
lineage. Three independent wrapper calls took 163.7 seconds in aggregate and
ended at 37,464; one reused continuous session took 73.2 seconds and reached
the same endpoint. This is approximately a 55% external-wall-time saving in
that comparison, while the reused process remained within a measured peak of
approximately 0.95 GB. The result supports reusing the static model/context
for diagnostic sessions, but it is not production enablement.

This qualification supports the current offline adaptive-calibration study
over the implemented portfolio, including larger utilization neighborhoods
and grade-bounded escape. It is not sufficient to promote an adaptive
allocator or any operator into ordinary scheduling. Calibration must retain
detached, fingerprinted inputs, complete incumbent validation, full-model
candidate validation, clean-process controls, and explicit
memory/unknown/infeasible reporting.

### Target-scale timing and gain scorecard

The session record exposes CP-SAT and validation totals; the remainder below
is the observed external session time after subtracting those reported values.
It includes model/context preparation, target ranking, cloning, hint and bound
setup, extraction, quality reconstruction, finalization, and process overhead.
The subtraction is an operational remainder, not an additive claim about every
nested probe timer.

| Family | First validated improvement | CP-SAT total | Validation total | External session | Non-CP remainder | Gain/minute | Stop |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `r2` | none | 30.3 s | 0.0 s | 57.0 s | ~26.7 s | 0.0 | `unresolved_unknown` |
| targeted `r4_s1` | 17.2 s | 6.8 s | 11.8 s | 73.6 s | ~55.0 s | 14.7 | `attempt_cap_reached` |
| targeted `r8_s1` | 18.7 s | 6.5 s | 10.7 s | 70.3 s | ~53.1 s | 20.5 | `attempt_cap_reached` |
| targeted `r4_s2` | 16.8 s | 6.9 s | 11.2 s | 71.2 s | ~53.1 s | 106.2 | `attempt_cap_reached` |
| targeted `r8_s2` | 19.6 s | 6.8 s | 11.5 s | 74.6 s | ~56.3 s | 101.6 | `attempt_cap_reached` |

The gain-per-minute values use the total validated substantive gain divided by
the actual external session wall time. The sessions reached only three
attempts, so there are no honest five- or ten-minute checkpoints to report.
No family proved a target scope exhausted; `UNKNOWN` remained unresolved.

The S1 trajectories were 37,596 → 37,590 → 37,578 → 37,572 for R8/S1 and
37,596 → 37,590 → 37,584 → 37,578 for R4/S1. The S2 trajectories were
37,596 → 37,590 → 37,476 → 37,470 for both dynamic R4/S2 and dynamic R8/S2.
Section utilization improved throughout the targeted trajectories. The S2
middle move also changed difficulty and category components under the existing
aggregate v2 objective; semester balance and fulfillment remained preserved.

Target-scale telemetry records per-attempt model sizes, branches, conflicts,
CP-SAT wall time, validation time, target history, and sampled process memory.
It does not currently expose independent timers for every setup sub-phase or a
separate quality-reconstruction timer at session scope. Those phases are
therefore included in the reported non-CP remainder rather than fabricated as
individual measurements.

## Utilization-cluster search boundary

The next diagnostic family uses the existing global pairwise section-
utilization penalty to select bounded multi-student scopes. It is documented
in [`STUDENT_ASSIGNMENT_UTILIZATION_CLUSTER_SEARCH.md`](STUDENT_ASSIGNMENT_UTILIZATION_CLUSTER_SEARCH.md).
The guidance is intentionally not a student-local objective: it computes
optimistic single-request leverage while ignoring feasibility interactions,
then lets the unchanged CP-SAT model and full-model validator decide whether a
candidate is legal and better.

The opt-in families are `targeted_utilization_r16_s2`,
`targeted_utilization_r16_s4`, `targeted_utilization_r32_s4`,
`targeted_utilization_r32_s6`, `targeted_utilization_r64_s6`,
`targeted_utilization_r64_s8`, and `targeted_utilization_r64_s10`. The
selected candidate pool equals the changed-student cap, dynamic sessions
retarget after adoption, and fixed sessions retain their supplied scope.
This capability remains diagnostic-only; no ordinary production portfolio or
adaptive controller has been changed.
