# Student Assignment Runtime Pipeline

This is the canonical owner for the long-running student-assignment execution
pipeline and the meaning of its wall-clock phases. It describes what the
engine is doing during a multi-hour diagnostic branch and how timing facts are
accounted for.

It deliberately does not own resource sampling (see
[Observability and Monitoring](OBSERVABILITY_AND_MONITORING.md)), candidate
authority (see [Student Assignment Validation](STUDENT_ASSIGNMENT_VALIDATION.md)),
operator-selection mathematics (see
[Student Assignment Adaptive Search](STUDENT_ASSIGNMENT_ADAPTIVE_SEARCH.md)),
or Celery/process orchestration (see [Scheduling Workers](SCHEDULING_WORKERS.md)).
Objective meaning remains owned by
[Student Assignment Objective Semantics](STUDENT_ASSIGNMENT_OBJECTIVE_SEMANTICS.md).

## Runtime contract

The student-assignment engine is a Django-free diagnostic execution path. A
long-running branch repeatedly invokes a public operator-session boundary.
Each invocation may search locally, produce a candidate, validate it against
the unchanged full model, and either adopt a strictly better complete result
or retain the previous authoritative incumbent. The diagnostic adaptive
runtime is research-only; it is not ordinary production assignment or an
approval authority.

The durable lifecycle is:

```text
validated bootstrap source
    -> public operator session
    -> input preparation and base model
    -> mature incumbent preparation/validation
    -> target selection and probe model
    -> optimization CP-SAT
    -> candidate extraction
    -> canonical full-model candidate validation
    -> strict adoption or incumbent retention
    -> next authoritative incumbent
```

## Bootstrap and steady state

Before a branch can carry an incumbent internally, the source must be complete,
zero-unmet, identity-consistent, and fully validated by the canonical
validator. That first public session is the bootstrap. It may pay
mature-source materialization and full-model validation.

In later sessions, a provenance-carrying
`ValidatedStudentAssignmentBranchContext` may reuse the already validated
mature incumbent after its identity guards pass. This removes only the
repeated mature-incumbent validation solve. The current implementation still
rebuilds the production/base model for each public session and still validates
every newly generated candidate. The trusted-context contract is defined in
[Student Assignment Trusted Branch Context](STUDENT_ASSIGNMENT_TRUSTED_BRANCH_CONTEXT.md).

## Public-session phases

The public boundary is
`run_student_assignment_operator_session_diagnostic` in
`scheduling_engine/student_assignment/core.py`. Its nested phases are:

| Phase | What it means | Authority/role |
| --- | --- | --- |
| Public session initialization | Normalize the selected scope and session configuration. | Setup only |
| Student-assignment input preparation | Validate and prepare the detached immutable input. | Required input contract, not candidate proof |
| Production/base-model construction | Build the full CP-SAT model used by the production-shaped session. | Supplies the model; does not itself authorize a candidate |
| Mature-source materialization | Convert the incoming semantic source decisions to current model variable values. | Lineage preparation |
| Mature-source validation | Validate an untrusted incoming source through the full model, unless a compatible trusted branch context is reused. | Required for public untrusted input |
| Target preparation | Select the operator’s current target scope and derive target facts. | Search guidance only |
| Probe invocation | Run one local operator attempt using a fresh clone and fresh dynamic restrictions. | Candidate production |
| Candidate full validation | Validate the newly produced candidate with the unchanged full-model validator. | Necessary authority gate |
| Candidate processing | Check completeness, scope, quality, strict improvement, and adoption. | Adoption logic |
| Quality evaluation | Calculate Objective Semantics v2 facts for complete results. | Measurement, not hard-model proof |
| Policy selection | Choose the next diagnostic operator when an adaptive runtime is used. | Search-policy cost only |
| Serialization/checkpoint/finalization | Build returned records and optional external artifacts. | Observational/persistence boundary |
| Independent final validation | Revalidate a final detached checkpoint when a study contract requires it. | Separate final authority check |

## Probe phases

`probe_substantive_soft_tier` in
`scheduling_engine/student_assignment/substantive_probe.py` performs the
operator-local work. The probe clones the base model, then applies dynamic
facts in this order:

1. model clone;
2. completion constraints, so every completion-defining group remains exactly
   one;
3. neighborhood constraints for the selected students/radius;
4. objective-bound constraints for already protected objective tiers;
5. transferred incumbent hints;
6. solver setup;
7. native optimization CP-SAT;
8. candidate extraction and quality facts.

These are search/probe phases. They are not candidate authority. A successful
probe only supplies a candidate to the separate full-model validator.

## Candidate-validation phases

`validate_source_decision_candidate_with_status` in
`scheduling_engine/student_assignment/solver.py` is the canonical validator.
Its diagnostic timing detail separates model construction/clone, completion
constraints, source-variable fixes, fresh validator setup, native validation
CP-SAT, and result extraction. These phases remain necessary to interpret the
cost of authority; their timing instrumentation does not change validation
behavior or classification. See the validation document for the authoritative
meaning of `validated`, `hard_invalid`, `validation_unknown`, and
`validation_error`.

## Timing semantics

The diagnostic recorder in `runtime.py` emits
`hierarchical_phase_timing_v1`. Every span has an ID, one `parent_id`,
`inclusive_seconds`, and `exclusive_seconds` (self time). Inclusive time is
the complete elapsed interval of that span, including descendants. Exclusive
time is the span interval minus the inclusive time of its direct children.

For additive wall-clock reporting, use only direct children of the
`public_operator_session` root plus the root's own exclusive/self residual:

```text
root wall
  = initialization
  + input preparation
  + base-model construction
  + mature materialization
  + mature validation
  + probe invocation
  + candidate full validation
  + root self/residual
```

Nested probe and validation descendants are attribution detail. They must not
be added again to the direct-child totals. This prevents double-counting a
native solve that is already inside a probe or validator span. On exception,
the telemetry is diagnostic and may contain an incomplete root span; it must
never determine solver authority or candidate adoption.

The timing tree distinguishes four kinds of work:

- search CP-SAT: the operator’s exploratory optimization solve;
- necessary authoritative validation: full-model validation of a candidate or
  untrusted source;
- reusable infrastructure/setup: input/model preparation and clone/build
  work that may be studied for safe amortization;
- observational telemetry: timing, resource, and serialization facts that do
  not affect scheduling decisions.

Resource definitions, psutil/RSS/USS/CPU/process-tree sampling, host-memory
semantics, and monitor-failure behavior remain exclusively owned by
[Observability and Monitoring](OBSERVABILITY_AND_MONITORING.md). Worker
launching and Celery boundaries remain exclusively owned by
[Scheduling Workers](SCHEDULING_WORKERS.md).

## Current empirical reference

These are dated measurements, not permanent performance constants. In the
September 6, 2026 R64 target-scale reference study, the branch ran 18 public
sessions over 7,233.887 seconds. Historical persisted phase facts included
2,372.025 seconds of search CP-SAT, 1,561.047 seconds of mature-source
validation, 1,613.089 seconds of candidate validation, and 393.183 seconds of
production model construction. The raw lineage is external at
`C:\Users\desou\research_runs\v2_r64_s8_two_hour_long_horizon_20260906`;
the earlier runtime audit is at
`C:\Users\desou\research_runs\v2_runtime_pipeline_audit_20260906`.

The September 6 bounded trusted-context fixture used 80 students and showed
semantic parity plus mature validation reuse on two post-bootstrap calls. It
was not target-scale evidence and must not be extrapolated to the 1,400-student
workload. The target-scale trusted-runtime qualification is a separate,
date-stamped lineage and must report bootstrap versus steady-state phases
separately.

### Target-scale trusted-runtime qualification

The final September 6, 2026 bounded runtime-infrastructure qualification used
the exact validated `reference_target` source: source-artifact fingerprint
`f5cfd15465bab1815ad21a3565236f1ff383e8ffff82a4c783ab62ff410c9fb1`, semantic
source-decision fingerprint
`aa49dde149fe927ff1bb8707d139d44a12a28459170b292d3da06ca5082327c4`, input
fingerprint `f56b5c0d5b745d919a57281a2f1e49959b4b23d8feb9486eda3c81afd8bb7906`,
Objective Semantics v2, substantive value 42,750, and 140 special
commitments. The run used one `targeted_utilization_r16_s4` workload only,
seed 101, eight CP-SAT workers, a 300-second operator allowance, a
180-second candidate-validation allowance, strict improvement, and four
sequential attempts. It was runtime qualification, not a policy-quality
comparison. The qualification persisted model fingerprint
`ab929407fe3daed51c48e9280bc2f05f5e1610eeda2b316c8bb375ba30999cec` and
required decision-group count 9,170.

The bootstrap public-session root was 183.161 seconds. The three steady-state
roots totaled 583.854 seconds, averaging 194.618 seconds (p50 148.327
seconds). Mature validation was present only in the bootstrap; the three later
attempts reused the trusted incumbent and had no mature-validation span. The
steady-state additive profile was: productive optimization CP-SAT 339.201
seconds (58.10%), authoritative candidate-validation CP-SAT 126.686 seconds
(21.70%), all native CP-SAT 465.888 seconds (79.80%), and non-CP-SAT work
117.966 seconds (20.20%). Base-model construction was 25.788 seconds (4.42%).
Candidate validation overall was 181.747 seconds, 95.91% native CP-SAT.

The previous 71.510-second residual (10.85%) was decomposed with additional
observational spans. Named direct children accounted for the work in the
qualification artifact; the remaining unexplained root self/exclusive residual
was 12.383 seconds (2.12%). The largest named non-CP phases were result-review
diagnostics (15.254 seconds), optimization-result assembly (7.160 seconds),
and the quality/extraction/reconstruction phases, each below 3 seconds in
aggregate. This is classified as a small residual rather than a safe repeated
infrastructure opportunity.

The qualification lineage is
`C:\Users\desou\research_runs\v2_target_scale_runtime_optimization_pass_20260906`.
The earlier trusted-runtime lineage remains the source of the historical
baseline figures in this document. During this pass, the first runner
invocation briefly targeted that prior output directory before the new lineage
path was corrected; therefore its compact qualification/attempt files must not
be treated as an untouched immutable copy. The new lineage is the authoritative
artifact set for the final timing measurements.

## Interpretation and limitations

Historical flat engine phase totals are often inclusive and nested. They are
not mutually exclusive unless a report explicitly uses the direct-child/root
self method above. A reduction in runtime does not imply a better schedule,
more attempts, or policy superiority. A changed operator or unresolved result
does not authorize a candidate. Measurements must name the source/scenario,
Objective Semantics version, worker/seed contract, date, and artifact lineage.

The current recorder covers the public engine session and its probe/validator
descendants. Outer adaptive-policy evaluation, resource-monitor sampling, and
external serialization may sit outside that root and should be reported as
separate outer-run facts where applicable. Future optimization ideas must be
marked as proposals until structural equivalence, authority preservation, and
target-scale measurements justify promotion.

## Neighboring canonical owners

| Concern | Owner |
| --- | --- |
| Runtime phases and wall-clock accounting | This document |
| Trusted validated-incumbent carry-forward | [STUDENT_ASSIGNMENT_TRUSTED_BRANCH_CONTEXT.md](STUDENT_ASSIGNMENT_TRUSTED_BRANCH_CONTEXT.md) |
| Static/dynamic model reuse boundaries | [STUDENT_ASSIGNMENT_MODEL_REUSE.md](STUDENT_ASSIGNMENT_MODEL_REUSE.md) |
| Resource monitoring and psutil telemetry | [OBSERVABILITY_AND_MONITORING.md](OBSERVABILITY_AND_MONITORING.md) |
| Candidate/source validation authority | [STUDENT_ASSIGNMENT_VALIDATION.md](STUDENT_ASSIGNMENT_VALIDATION.md) |
| Celery/process worker execution | [SCHEDULING_WORKERS.md](SCHEDULING_WORKERS.md) |
| Adaptive operator selection | [STUDENT_ASSIGNMENT_ADAPTIVE_SEARCH.md](STUDENT_ASSIGNMENT_ADAPTIVE_SEARCH.md) |
| Objective meaning | [STUDENT_ASSIGNMENT_OBJECTIVE_SEMANTICS.md](STUDENT_ASSIGNMENT_OBJECTIVE_SEMANTICS.md) |
