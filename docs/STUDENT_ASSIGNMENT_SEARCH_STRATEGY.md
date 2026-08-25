# Student Assignment Search Strategy

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
assignment recommendation. No adaptive controller, grade-bounded escape, or
global unrestricted operator is implemented by this module.

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

The next separately scoped research increment may study adaptive allocation
among targeted repair and retained local operators. Grade-bounded unrestricted
escape and full-school global escape remain deferred. The targeted diagnostic
does not change the production objective, counselor policy, approval behavior,
or operator allocation.

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
it may choose among targeted repair and retained local operators, but it must
never change objective semantics, authorize a schedule, or bypass full
validation.

Grade-bounded unrestricted search and full-school unrestricted escape remain
deferred until after that adaptive-policy study.

## Objective Semantics v2 adaptive allocator

The diagnostic-only allocator is implemented in
`scheduling_engine/student_assignment/adaptive_search.py` and
`adaptive_runtime.py`. It chooses among the existing R2, targeted R8/S1,
targeted R8/S2, and targeted R4/S2 operators using explicit v2 pressure,
counselor-intent, history, and shared-budget signals. It does not call from
ordinary production assignment and does not alter the objective or hard-model
contract. Each attempt still goes through CP-SAT and the existing full-model
validator, and only a strict validated improvement is adopted. Quality and
student ranking are recomputed after adoption. Unknown and proven infeasible
outcomes remain distinct. The policy state and session records are documented
in [`STUDENT_ASSIGNMENT_ADAPTIVE_SEARCH.md`](STUDENT_ASSIGNMENT_ADAPTIVE_SEARCH.md).

This is an experiment boundary, not a production recommendation. Static
R2-only, targeted-operator-only, fixed-cycle, and adaptive sessions must be
compared on identical v2 input and source-seed fingerprints before any future
promotion decision. Grade-bounded and unrestricted global operators remain
deferred.
