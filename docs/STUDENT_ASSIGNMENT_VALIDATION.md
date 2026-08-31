# Student Assignment Validation

This document is the canonical source of truth for the student-assignment
validation architecture, candidate-authority rules, validation performance
evidence, and validation-specific research history. Future material changes or
experiments involving validation architecture, candidate authority, validation
timing or memory, validation CP-SAT behavior, scoped or deterministic
validators, validation benchmarks, or rejected validation ideas must update
this document. Other documents should link here rather than becoming separate
validation notebooks.

## Authority and purpose

Student assignment produces semantic source decisions: the selected section,
online-supervision session, or special-commitment placement for each source
decision. Those decisions are not authoritative merely because a diagnostic
operator or CP-SAT probe returned them. A candidate becomes an adopted
incumbent only after the unchanged full student-assignment model proves that
candidate valid.

The current authority rule is:

```text
semantic candidate source decisions
    -> unchanged full student-assignment model
    -> fresh validation model clone
    -> completion constraints and source-value fixes
    -> bounded CP-SAT satisfiability solve
    -> validated / hard_invalid / validation_unknown / validation_error
    -> objective and adoption checks
```

`validated` means CP-SAT found a complete assignment satisfying the model used
for validation. `hard_invalid` means CP-SAT proved the fixed candidate
inconsistent with that model. `validation_unknown` means proof was not
established within the bound and is unresolved, not evidence of infeasibility.
`validation_error` means the validation infrastructure or model could not
complete. Errors, unknown results, incomplete candidates, and hard-invalid
candidates fail closed and cannot become authoritative.

Validation is separate from quality measurement and counselor review. The
quality evaluator calculates Objective Semantics v2 facts after a complete
candidate is available; review diagnostics and evidence are presentation
facts, not substitutes for the hard-model proof.

## Current validation pipeline

| Step | Purpose | Required for authority? | Current implementation | Measured cost / status |
| --- | --- | --- | --- | --- |
| 1. Candidate produced | Obtain a complete diagnostic or optimization candidate | Yes, candidate must be complete | `scheduling_engine/student_assignment/substantive_probe.py`, `core.py` | Operator-specific |
| 2. Input/lineage verification | Ensure candidate and DTO belong to the same semantic input | Yes for detached replay identity | `stage2_benchmark.py`, `validation_benchmark.py` | Small relative to solve |
| 3. Semantic materialization | Convert source tuples into current model variable values | Yes | `core.py:_source_variable_values` | Sub-second for measured target source maps |
| 4. Full-model construction/availability | Build the unchanged model containing hard and derived constraints | Yes | `core.py:_solve_student_assignment` | About 7.6 s Stage 1 build; about 16.6 s in a detached candidate-validation operation |
| 5. Model clone | Isolate candidate validation from the production model | Yes for the current validator | `solver.py:validate_source_decision_candidate_with_status` | 0.17--0.46 s at target scale |
| 6. Completion constraints | Require every completion-defining group to be exactly one | Yes | `solver.py:validate_source_decision_candidate_with_status` | 0.49--2.15 s at target scale |
| 7. Source-variable fixing | Fix supplied semantic source decisions to the candidate | Yes | `solver.py:validate_source_decision_candidate_with_status` | 0.75--1.96 s normally |
| 8. Solver construction | Create a fresh bounded CP-SAT validator | Yes | `solver.py:new_solver` | Approximately 0.0001 s |
| 9. Full-model satisfiability solve | Prove hard validity and derive auxiliary state | Yes; sole current authority | `solver.py:validate_source_decision_candidate_with_status` | Dominant cost at target scale |
| 10. Result classification | Preserve optimal/feasible/infeasible/unknown/error distinction | Yes | `solver.py`, `core.py` | Included in validator operation |
| 11. Identity checks | Compare returned semantic source identity with supplied candidate where available | Yes for detached candidate adoption | `core.py`, `validation_benchmark.py` | Small |
| 12. Objective/adoption checks | Require strict improvement and preserve incumbent semantics | Yes for diagnostic adoption | `core.py`, `substantive_probe.py` | Small compared with CP-SAT |
| 13. Result/quality/review construction | Build result DTO and counselor-facing facts | No for hard authority; required for the returned diagnostic result | `core.py`, `quality.py`, review services | Extraction/quality under 1 s; detached review diagnostics about 3.3--8.5 s |

### Detached and direct-local paths

Detached source validation enters through
`run_student_assignment_source_decision_validation_diagnostic` in
`scheduling_engine/student_assignment/core.py` and is summarized by
`run_source_decision_validation_benchmark` in
`scheduling_engine/student_assignment/validation_benchmark.py`. It rebuilds
the current full model in that operation and validates the supplied semantic
source map before returning facts.

Direct local operator validation is nested inside
`run_student_assignment_operator_session_diagnostic` and
`_solve_student_assignment`. The operator reuses one already-built model for
the local probe, extracts a candidate, validates it through the same full-model
validator, and only then adopts it. Direct-local timing is not interchangeable
with detached timing: setup and model reuse differ, and a continuous operator
session may also perform target selection, probe work, iteration recording,
quality comparison, and subsequent attempts.

## Central timing evidence

These values are historical measurements from the current code and are not
universal guarantees. Detached and direct-local measurements must not be
compared as if they were the same operation.

| Scenario | Model variables / base constraints | CP-SAT validation | Validator / operation | Result |
| --- | ---: | ---: | ---: | --- |
| Small focused fixture | 24 / 50 | ~0.0006 s | ~0.0011 s validator | validated |
| Medium production-shaped fixture | 10,901 / 16,942 | ~0.21 s | ~1.7--1.8 s detached operation | validated |
| Reference target Stage 1 source | 167,259 / 269,235 | ~35.88 s | ~53.14 s detached operation | validated |
| Special-pressure target Stage 1 source | 150,487 / 244,343 | ~28.67 s | ~44.92 s detached operation | validated |
| Exact reference target candidate, detached normal replay | 167,259 / 269,235 | ~74.98 s | ~97.68 s validator; ~129.93 s outer operation | validated / optimal |

The exact detached candidate contained 9,170 source decisions, 9,030
assignments, zero unmet requests, and 140 special commitments. Validation added
9,170 completion constraints and 57,666 source equalities, producing 336,071
constraints without changing the 167,259 variables. All 57,666 source
variables were fixed; 109,593 auxiliary variables remained for CP-SAT to
derive. Candidate extraction took 0.145 s, quality evaluation 0.307 s, result
reconstruction 0.080 s, and detached review diagnostics 8.54 s.

The normal exact-candidate replay with native search-start logging reported the
first search and first solution markers at approximately 74.93 s. OR-Tools
does not expose a supported first-branch timestamp through the current Python
API, so this does not prove an exact per-branch attribution. It does establish
that source-variable fixing alone does not make the native proof immediate.

A stop-after-presolve probe against the same candidate returned
`validation_unknown` intentionally and was never authoritative. It measured
approximately 62.54 s of native CP-SAT wall time, 87.55 s validator telemetry,
and 104.51 s outer operation time. The current log parser did not receive a
complete presolved-model summary in this mode; its zero presolved counts are a
telemetry limitation, not a claim that the model reduced to zero.

## Existing validation optimization history

| Date/study | Idea | Scope | Performance result | Correctness result | Decision |
| --- | --- | --- | --- | --- | --- |
| Validation cost audit | Skip completion `ExactlyOne` constraints when source groups appear complete | Medium and target | Medium improved; target was slower/noisier (~62.84 and ~64.47 s versus ~53.14 s baseline) | No authority semantics changed during trial | Rejected |
| Validation cost audit | `fix_variables_to_their_hinted_value` | Medium | Slower or more variable than baseline | Not promoted | Rejected |
| Validation cost audit | Non-binding auxiliary hints | Medium | No benefit | Not promoted | Rejected |
| 2026-08-30 Phase 0 | Source-variable freedom and native presolve/search telemetry | Small through target | Observational only | Default validator unchanged | Retained as diagnostic instrumentation |
| 2026-08-30 exact-candidate audit | Search-start and stop-after-presolve probes | Exact reference target | Showed native CP-SAT reconstruction/presolve dominates | Probes are not authority results | Retained as evidence |
| 2026-08-30 full-witness experiment | Equality-fix every original-model variable from the exact probe response | Small and target local A/B | Target CP-SAT fell from 69.79 s to 4.32 s; total validation fell from 109.71 s to 47.45 s, while preparation rose | Small parity passed; target candidate passed the unchanged full model | Diagnostic only; not authority-promoted |

## Current bottleneck ranking

1. **Current ordinary-path primary: repeated candidate-independent Python
   preparation.** In the latest direct target observations, variable-freedom
   accounting was about 5.2--5.9 s and model fingerprinting about 0.9--1.0 s
   inside a roughly 9.3--10.3 s validator wall. A prepared context removes
   these repeated scans for later same-process candidates.
2. **Detached-operation secondary: full-model construction.** The latest
   detached observations spent approximately 15--16 s outside the validator
   boundary rebuilding the model. This is not amortized by a context created
   after the model already exists, and is the next separate preparation/reuse
   question.
3. **Remaining per-candidate work: source fixing and CP-SAT.** Source equality
   construction was about 1.0--1.2 s and CP-SAT about 1.3 s in the direct
   target replay. Clone and completion construction are small in the warm
   prepared path. Quality, extraction, and DTO/review work remain outside the
   authority proof and should be measured separately before being moved.

Priorities may change after repeated measurements on the same process and
machine.

## Exact-witness investigation

### Hypothesis

The local CP-SAT probe already has a concrete solution for the full base model.
If the probe model is a clone of the full base model and only appends
probe-specific constraints and variables, the solution values for the original
base variable prefix can be carried into a diagnostic validation clone. Fixing
those values would ask CP-SAT to verify one exact full-model witness instead of
searching for an auxiliary completion.

### Variable identity findings

The current probe creates `probe_model = context.model.Clone()` and then adds
completion, scope, neighborhood, objective-bound, and hint information. The
only variables created by the observed probe path are
`changed_student_<id>` indicators, created after the base clone when the
student-change bound is active. The original base model therefore occupies the
prefix `[0, len(context.model.Proto().variables))`; probe-only indicators are
outside that prefix.

The implementation now records a model-proto fingerprint and asserts that the
fresh probe clone has the same base proto before adding probe constraints. A
candidate witness captures exactly the original base variable prefix and its
variable count. The validation side requires the expected fingerprint, exact
base variable index coverage, and equality between witness source values and
the semantic source-value map. Missing, extra, mismatched, or stale values fail
closed.

This mapping is proven only for the same in-process model lineage used by the
diagnostic operator. A witness must not be serialized and trusted across a
different model build, process, DTO, or benchmark lineage. The durable branch
format intentionally stores semantic source decisions, not CP-SAT witnesses.

### Authority analysis

The current validator proves:

```text
there exists an auxiliary completion satisfying the full model
for these semantic source decisions
```

The diagnostic witness formulation proves:

```text
this exact complete assignment of the original full-model variables
satisfies the unchanged full model
```

For a witness whose variable identity, completeness, lineage, and values are
proven as above, the second statement is sufficient as a hard-validity proof:
every original-model variable is fixed and CP-SAT still checks every original
hard constraint. It is not a heuristic and it does not expose anonymous
placement staffing identities. Nevertheless, this goal does not promote it to
production authority because authority qualification requires broader
differential evidence, repeated target measurements, and explicit treatment
of all candidate-generation paths.

### Diagnostic implementation

The witness path is opt-in only:

- `substantive_probe.py` captures the complete response for the original base
  variable prefix and records the base model fingerprint.
- `core.py` can pass that witness to local candidate validation.
- `solver.py` adds equality constraints for every base-model variable in the
  diagnostic validation clone and keeps the unchanged full model authoritative.
- Missing or inconsistent witness data produces `validation_error`; an
  infeasible or unknown CP-SAT result remains non-authoritative.
- Ordinary production validation defaults remain unchanged.

No witness is included in durable semantic checkpoint identity, public schedule
results, or named assignments.

### Small parity gate

The focused witness test compares normal and witnessed validation on a small
CP-SAT model. A valid witness is accepted with the same `validated`
classification; an altered auxiliary value is `hard_invalid`; an incomplete
witness is `validation_error`. Existing validation, adaptive-search, and
student-assignment tests also remain green.

### Target A/B evidence

One matched direct-local reference-target run was performed for each mode using
the same detached input, Stage 1 semantic source seed, `targeted_r4_s2`
operator, selected students `(204, 604)`, one worker, 300-second probe bound,
180-second validation allowance, and 600-second session bound.

| Mode | Candidate | Probe CP-SAT | Validation CP-SAT | Validation total | Local session | Peak working set | Result |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| Current full validator | 42,600 | 117.99 s | 69.79 s | 109.71 s | 267.63 s | 922.8 MB | complete, validated |
| Full witness diagnostic | 42,600 | 122.44 s | 4.32 s | 47.45 s | 209.31 s | 996.5 MB | complete, validated |

Both runs produced 9,030 assignments, zero unmet requests, 140 special
commitments, two changed source decisions, and one changed student. The witness
run fixed 167,259 original-model variables and increased the validation clone
to 503,330 constraints. It is therefore a substantial native-solve reduction
with a measurable preparation and memory cost.

The concise one-off local records did not persist the complete semantic source
fingerprint for both A/B rows, so they prove matched input/configuration and
matching reported candidate shape, but not bit-for-bit source-decision identity
between those two runs. The separately durable branch has source fingerprint
`50b482c435cc802b2b3e785a738f8e7d26b6a7ff6422cfa3a96f5595facfb46d`, parent
Stage 1 fingerprint
`f5cfd15465bab1815ad21a3565236f1ff383e8ffff82a4c783ab62ff410c9fb1`, and
compressed artifact SHA-256
`73d213aae080bd0fa578cbe0f6f8a42336dbde4ea19574d8eacfd24bdbe32cf6`.

### Classification

`FULL-WITNESS VALIDATION IS CORRECT BUT NOT YET PERFORMANCE-QUALIFIED FOR
AUTHORITY.`

The exact mapping and fail-closed behavior are implemented and the small parity
gate passed. The target A/B is promising and materially faster, but it is one
run per mode, has a roughly 74 MB peak working-set increase, and does not have
complete persisted source-identity parity. It must not replace the current
validator in production or authorize a candidate by itself in the current
workflow.

## Authority-parity qualification study (2026-08-30)

The qualification harness is a clean-process diagnostic surface in
`validation_qualification.py`. One operator solve captures one complete
semantic candidate, one candidate source fingerprint, one base-model
fingerprint, and one in-process witness. The same candidate and witness are
then sent to both validation paths. The ordinary source-fixed validator is
recorded as the authority; witness validation is shadow-only and cannot rescue
an ordinary rejection or authorize adoption.

The durable study lineage is
`scheduling_engine/benchmarks/student_assignment/validation_qualification_20260830/`.
Its manifest and records contain only compact facts and file hashes. Raw
anonymous auxiliary values are never serialized.

The four reference-target pairs all used the verified durable benchmark input
fingerprint `1c4843ac33fccabd76218c63d8818c94a0a8ddab2886e3f5718ca1cd9576a11`,
the Stage 1 parent fingerprint
`00889b7f4110dc19c6cdcb413b44fe77ab9598cb0fde25de6ea618ddb27325e7`,
`targeted_r4_s2`, selected students `(204, 604)`, one worker, a 300-second
probe allowance, and a 180-second validation allowance. All four produced the
same candidate fingerprint
`02fed7c072ddffea70900796eb19a0fcde723dcb8fb8e7f6f85c308169015a52`,
substantive value `65,171`, 10,760 assignments, zero unmet requests, and 310
special commitments. Each changed one source decision for one student.

| Pair | Order | Ordinary validation | Witness validation | Ordinary CP wall | Witness CP wall | Ordinary peak WS | Witness peak WS | Parity / false acceptance |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 1 | normal -> witness | 44.54 s | 54.21 s | 2.87 s | 3.11 s | 738,103,296 | 779,202,560 | parity / none |
| 2 | witness -> normal | 44.19 s | 56.18 s | 2.69 s | 3.40 s | 870,244,352 | 774,885,376 | parity / none |
| 3 | normal -> witness | 47.59 s | 57.28 s | 2.80 s | 2.86 s | 742,064,128 | 772,239,360 | parity / none |
| 4 | witness -> normal | 46.73 s | 54.09 s | 2.63 s | 2.76 s | 877,711,360 | 775,032,832 | parity / none |

Across the four pairs, ordinary validation was 44.19--47.59 seconds with a
45.64-second median; witness validation was 54.09--57.28 seconds with a
55.19-second median. Witness validation was therefore slower in every pair by
7.35--11.99 seconds (15.73--27.14% relative to ordinary validation; median
18.78%). Native CP-SAT wall time was similar and showed no stable witness
advantage. Witness preparation was higher because it added equality constraints
for all 110,916 base variables: ordinary validation ended with 259,077
constraints, while witness validation ended with 369,993. Witness peak working
set was approximately 775--779 MB in these runs, while ordinary peak working
set varied from approximately 738--878 MB with validation order; there was no
monotonic accumulation across the four clean processes.

The medium production-shaped gate also passed classification parity. It used
the same candidate callback with 2,064 source decisions and 39 special
commitments: ordinary validation was `validated/optimal` in 19.92 seconds
with 7.77 seconds of CP-SAT wall time; witness validation was
`validated/optimal` in 22.94 seconds with 1.25 seconds of CP-SAT wall time.
The witness path was slower overall. This confirms the harness works beyond
the tiny unit model but does not qualify production replacement.

### Differential parity matrix

The focused corpus currently covers the deterministic cases supported by the
small model builders:

| Candidate/witness case | Ordinary | Witness | Result |
| --- | --- | --- | --- |
| Valid source and exact witness | validated | validated | exact parity |
| Source violates an exactly-one hard rule | hard_invalid | hard_invalid | exact parity |
| Auxiliary value altered | validated | hard_invalid | conservative witness rejection; no false acceptance |
| Stale model fingerprint | validated | validation_error | fail closed |
| Missing witness variable | validated | validation_error | fail closed |
| Extra witness variable | validated | validation_error | fail closed |

The target candidates exercised complete normal, special-commitment, online,
half-semester, Study/Focus/Co-op, lock, capacity, and shared-resource model
state through the unchanged full model. The corpus does not yet contain an
independent mutation fixture for every listed lock or special-commitment
failure mode; those remain qualification gaps rather than invented evidence.
No false acceptance occurred. The required invariant held in every supported
case: the witness path never classified a candidate as valid when the ordinary
authority rejected it.

### Candidate-generation witness eligibility

The current probe implementation clones the same base model for every operator
family. It appends only probe constraints to that clone; targeted families also
append `changed_student_*` indicators after the original base-variable prefix.
The implementation asserts the base variable count and proto fingerprint before
capturing the witness.

| Operator family | Current classification | Reason |
| --- | --- | --- |
| R2 | witness eligible with assertion | Same base clone; no student-change indicator variables |
| targeted R4/S1, R4/S2, R8/S1, R8/S2 | witness eligible with assertion | Same base clone; bounded student indicators are appended after the base prefix |
| utilization-cluster families | witness eligible with assertion | Same probe path; target-selection constraints do not replace the base model |
| grade-bounded families | witness eligible with assertion | Same base clone; grade scope adds constraints over the original variables |

These are diagnostic eligibility classifications, not production wiring. A
future caller must still require a complete witness, matching model lineage,
matching semantic source values, and full validation classification checks.

### Qualification decision

`PERFORMANCE QUALIFIED BUT AUTHORITY COVERAGE INCOMPLETE` is not appropriate
for this study because total validation was not faster. The result is:

`AUTHORITY PARITY PASSES BUT PERFORMANCE BENEFIT INSUFFICIENT`

for the tested reference target and medium gate. Exact classification parity
and the no-false-acceptance invariant passed, but the witness adds preparation
work and memory without a repeatable total-wall-time benefit on the current
Objective Semantics v2 target. Special-pressure promotion was correctly
skipped because the prerequisite target performance gate failed. Witness
validation is not production-enabled and the ordinary full validator remains
the sole authority.

The initial one-off witness result remains historical evidence and is not
overwritten: it used a different candidate/model state and showed a one-run
native improvement. The repeated paired study is the stronger current result.

## Ordinary validation phase telemetry and prepared-context study (2026-08-30)

The ordinary validation wall boundary starts when
`validate_source_decision_candidate_with_status` is entered and ends after the
CP-SAT status has been classified. The detached benchmark's outer operation
also includes input fingerprinting, full-model construction, semantic source
materialization, and result reconstruction; those costs are not part of the
validator's internal wall field. The validator now reports additive phase
telemetry and accounted/unattributed totals so these boundaries are explicit.

The first three clean target observations used the unchanged durable
production-scale input and the same complete Stage 1 semantic candidate
(10,945 source decisions). They were ordinary source-fixed validations only:

| Observation | Outer operation | Validator wall | Fingerprint | Clone | Completion | Source fixes | Variable-freedom accounting | CP-SAT external | Accounted internal phases |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 26.22 s | 10.30 s | 0.93 s | 0.12 s | 0.65 s | 1.16 s | 5.87 s | 1.34 s | 10.07 s |
| 2 | 25.11 s | 9.30 s | 0.97 s | 0.13 s | 0.62 s | 1.03 s | 5.24 s | 1.30 s | 9.30 s |
| 3 | 24.61 s | 9.73 s | 0.94 s | 0.12 s | 0.62 s | 1.02 s | 5.69 s | 1.34 s | 9.73 s |

The remaining detached-operation time is primarily full-model reconstruction
outside the validator (approximately 15--16 seconds in these observations).
Result extraction and final classification are small in this path. The
historical 44--48 second qualification observations used a different local
candidate/operator execution context; the new phase telemetry should be used
for future attribution instead of assuming the older aggregate split.

The dominant repeatable candidate-independent validator cost is the
`_validation_variable_freedom` accounting scan, followed by model fingerprint
construction and ordinary source-equality construction. CP-SAT proof was
approximately 1.3 seconds in this direct target replay. This telemetry is
observational and does not alter ordinary authority behavior.

### Diagnostic prepared validation context

Because repeated local diagnostic sessions validate multiple candidates against
one unchanged in-process model, an opt-in `PreparedValidationContext` is now
available in `solver.py` and `validation_qualification.py`. Preparation owns a
clone of the unchanged full model, adds the same completion `ExactlyOne`
constraints once, caches the model fingerprint and immutable variable/index
accounting, and stores no candidate values or solver response. Every warm
validation still clones the prepared model and creates a fresh CP-SAT solver;
the same full model and source-value equality rules remain in force.

The context is same-process only. It is accepted only with the exact source
model object, unchanged model shape, and unchanged required-group registry. A
different model object, changed shape, or changed group registry fails closed.
Native solver objects and responses are never stored in or shared through the
context, and no context is trusted across process or durable-checkpoint
boundaries.

One clean target local diagnostic sequence generated one complete candidate
through the existing `targeted_r4_s2` callback and then validated that exact
candidate five times through each path. The candidate had substantive value
65,171; the sequence was diagnostic-only and did not authorize adoption.

| Path | Cumulative validation wall | Per-validation average | Result |
| --- | ---: | ---: | --- |
| Current ordinary path, 5 validations | 63.09 s | 12.62 s | all validated |
| Prepared path, 5 warm validations | 17.02 s | 3.40 s | all validated |
| Prepared path including 8.43 s creation | 25.45 s | 5.09 s amortized | all validated |

The same run's prefix amortization, including one-time context creation in the
prepared column, was: N=1 ordinary 13.85 s versus prepared 11.85 s; N=2
ordinary 13.12 s versus prepared 7.72 s; N=3 ordinary 12.88 s versus
prepared 6.26 s; and N=5 ordinary 12.62 s versus prepared 5.09 s. These are
one clean-process sequence measurements, not universal timing guarantees.

The target sequence had classification parity and zero false acceptances. The
prepared path removed repeated fingerprint, completion-constraint, and static
variable-accounting work while retaining ordinary source-fix construction,
fresh model cloning, and fresh CP-SAT solving. The context is therefore useful
for multi-candidate local sessions, not for a one-shot detached validation
whose cold preparation cannot be amortized. The sequence did not enable the
context in production/default validation and did not promote witness
validation.

The same sequence sampled memory per validation. Context preparation peaked at
approximately 688 MiB working set / 654 MiB USS. Ordinary validations, run
first in this process, peaked at approximately 864, 865, 971, 1,062, and
1,178 MiB working set. Prepared validations then peaked at approximately
1,070, 1,078, 958, 868, and 874 MiB working set. Because the two paths were
intentionally run in one process and in a fixed order, these values are a
resource envelope rather than a cold-order A/B comparison. The prepared
sequence itself showed no monotonic memory accumulation; a dedicated
fresh-process lifetime study remains appropriate before any broader reuse.
The target process returned below its transient peak after native work was
released. Existing worker recycling remains required.

**Classification:** `PREPARED CONTEXT STRONGLY QUALIFIED FOR DIAGNOSTIC
MULTI-CANDIDATE SESSIONS`. The context passed distinct-candidate parity,
zero-false-acceptance, incumbent-transition, and 20-repetition lifetime gates.
This qualifies an opt-in diagnostic facility; it is not a promotion to the
default authority path.

The next validation-research direction is to retain/refine this prepared
in-process context for diagnostic multi-candidate sessions, add explicit
memory/lifetime and distinct-candidate parity coverage, and keep detached
one-shot validation on the ordinary full validator.

### Multi-candidate and lifetime qualification (2026-08-30)

A clean target-scale `targeted_r4_s2` session from the durable Stage 1 seed
produced three distinct complete candidates in one unchanged model lineage:

| Candidate | Semantic source fingerprint | Substantive value | Changed students | Changed source decisions |
| --- | --- | ---: | ---: | ---: |
| 1 | `02fed7c072ddffea70900796eb19a0fcde723dcb8fb8e7f6f85c308169015a52` | 65,171 | 1 | 1 |
| 2 | `3ca0f71435401d3d5116b00313595fecd7ce028fe405c8242ed0824a9dfa378e` | 65,169 | 1 | 1 |
| 3 | `048e7c89f17925680661e04729deeb3e2580418b11d41552ee47477deb36927c` | 65,167 | 1 | 2 |

The operator fully validated and adopted all three candidates, ending
`complete`/`feasible` at 65,167 with 10,635 assignments, zero unmet requests,
and all 310 special commitments fulfilled. The prepared context was created
once from the unchanged model and required-group registry and held no
candidate values. The initial incumbent was validated before context creation;
the three subsequent semantic candidates were distinct source states in the
same prepared lineage. This establishes the intended A-to-B-to-C-to-D session
pattern operationally; the direct corpus comparison covers the three
candidate states that entered the reusable context.

The direct three-candidate differential corpus reported:

| Candidate | Ordinary | Prepared | Ordinary wall | Prepared wall |
| ---: | --- | --- | ---: | ---: |
| 1 | validated | validated | 12.48 s | 3.20 s |
| 2 | validated | validated | 12.55 s | 3.31 s |
| 3 | validated | validated | 12.45 s | 3.34 s |

Classification parity was true for every candidate and false acceptance was
zero. The prepared operator path also adopted all three candidates while the
ordinary full-model CP-SAT validator remained the authority for every
adoption.

A separate 20-repetition same-candidate sequence in a clean process reported
ordinary validation total 243.61 s (12.18 s average) and prepared validation
total 66.71 s (3.34 s warm average). Including the 8.37 s context creation,
prepared amortized time was 3.75 s per validation. All 20 ordinary/prepared
classifications matched and false acceptance was zero.

Resource samples did not show monotonic prepared-context accumulation. Context
creation peaked at approximately 619 MiB working set / 557 MiB USS in this
sequence. Prepared validation peaked at approximately 1,075 MiB working set
on the first sample, then approximately 774, 762, 773, and 780 MiB at samples
5, 10, 15, and 20; the process ended below the first prepared peak. Ordinary
validation, deliberately run first in the same process, reached approximately
1,385 MiB at sample 10 before declining. Because this is fixed-order
same-process evidence, it is a resource envelope rather than a cold-order
memory A/B. It supports stable reuse for this diagnostic lifetime but does not
remove the existing requirement for process recycling.

The matched three-attempt operator A/B produced identical semantic candidate
states and endpoint 65,167. Ordinary candidate validation totaled 15.13 s;
prepared validation totaled 9.48 s after 7.83 s context creation. End-to-end
operator wall time was 92.01 s ordinary and 102.95 s prepared in that pair.
The slower prepared end-to-end result is not evidence that the context changed
the model: probe CP-SAT search varied between runs, and the measured validation
subphase was lower. It does mean a broader operator-level speedup has not been
established. The context is diagnostic-session qualified on parity and warm
validation cost, but is not entitled to replace ordinary validation by default
or to claim a total-session performance win.

The expensive telemetry audit has a separate result:
`EXPENSIVE VALIDATION TELEMETRY SAFELY MADE OPT-IN`. Target-scale ordinary
authority-only validation took 2.97 s of validator wall time, versus 9.64 s
with full diagnostic telemetry. CP-SAT was approximately 1.25 s in both. The
default path now skips the non-authoritative model fingerprint scan and
variable-freedom accounting; benchmark and qualification helpers opt in when
those measurements are required. Fingerprint checks remain mandatory for full
auxiliary-witness validation and prepared-context identity checks. No
hard-validity check was removed.

### Prepared-context break-even and lazy activation study (2026-08-30)

The context-construction audit identified the following pre-minimal target-scale
costs. The earlier implementation recorded approximately 12.09 seconds total:

| Creation phase | Measured time | Classification |
| --- | ---: | --- |
| source-model identity verification | 0.00 s | Same-object safety is enforced during validation |
| model clone | 0.17 s | Required to isolate the reusable validation model |
| required-group index preparation | 0.10 s | Required for the completion registry |
| completion-constraint construction | 0.93 s | Required for ordinary-validator equivalence |
| model fingerprint | 1.40 s | Required for cached lineage identity |
| source-variable index preparation | 0.47 s | Diagnostic freedom accounting only |
| static family accounting | 7.72 s | Diagnostic model-complexity metadata only |
| static counts/hint accounting | 1.32 s | Diagnostic metadata only |

Static family/count metadata was therefore made opt-in. Minimal context
creation retains the model clone, required-group registry, completion
constraints, cached base fingerprint, model shape, and same-process identity
checks. It skips only diagnostic source/family/count accounting unless full
telemetry is requested. A post-change target-scale measurement was 1.72 seconds
(clone 0.12 s, group indexes 0.06 s, completion constraints 0.61 s, fingerprint
0.94 s). Every candidate still receives a fresh model clone, fresh source
equalities, and a fresh CP-SAT solve.

The authority-only break-even corpus used ten distinct complete candidates
returned and ordinarily validated by the target-scale operator. The cumulative
costs were:

| N | Ordinary | Prepared including cold creation | Difference (ordinary - prepared) |
| ---: | ---: | ---: | ---: |
| 1 | 3.607 s | 5.439 s | -1.832 s |
| 2 | 7.100 s | 8.205 s | -1.105 s |
| 3 | 10.424 s | 11.267 s | -0.843 s |
| 4 | 13.839 s | 14.159 s | -0.320 s |
| 5 | 18.249 s | 17.403 s | +0.846 s |
| 6 | 21.840 s | 20.299 s | +1.542 s |
| 8 | 29.780 s | 26.956 s | +2.824 s |
| 10 | 37.143 s | 32.798 s | +4.346 s |

The measured break-even count is **N=5**. With measured approximate values
`C=2.04 s`, `O=3.71 s`, and `P=3.08 s`, the explanatory model
`ceil(C / (O-P))` predicts **N=4**. The one-count difference is timing noise
near the boundary; the empirical curve is authoritative for this study.

The diagnostic operator supports `ordinary`, `eager`,
`after_first_validated_candidate`, and `threshold` strategies. After-first
activation performs the first candidate validation ordinarily and creates a
context only after that candidate is validated. Threshold activation uses only
the configured maximum attempt count and a configured minimum threshold; it
does not assume that every future attempt will produce a candidate. Neither
strategy is production-enabled.

On the target max-three session, ordinary validation totaled 14.22 seconds,
after-first validation plus context creation totaled 12.09 seconds, and
threshold-three totaled 13.42 seconds. All reached the same complete 65,167
endpoint. Total session times were 96.58, 98.36, and 103.35 seconds
respectively, demonstrating that probe CP-SAT variance obscures the smaller
validation saving at the end-to-end level. A bounded no-candidate/`unknown`
session under after-first activation created no context; it remained unresolved
and was not treated as infeasible.

The second-family smoke check used the existing six-student R2 fixture. Ordinary,
eager, and after-first sessions each completed two sequential improvements;
after-first used ordinary validation for the first and prepared validation for
the second. A larger medium R2 attempt returned `unknown` without a candidate
under its bounded budget, so it does not establish target-scale R2 performance.

The 20-repetition lifetime study remained parity-preserving with zero false
acceptance. Ordinary validation totaled 243.61 seconds; prepared validation
totaled 66.71 seconds plus 8.37 seconds creation. Prepared memory showed no
monotonic accumulation: its sampled working-set peaks were approximately 1,075,
774, 762, 773, and 780 MiB at samples 1, 5, 10, 15, and 20. The fixed-order
same-process study is a resource envelope, not proof of cross-process reuse.

**Eager-versus-lazy classification:** `PREPARE ONLY FOR LONG/HIGH-ATTEMPT
SESSIONS`. The evidence favors after-first activation when candidate production
is uncertain and prepared reuse only once multiple validated candidates are
likely. **Production-promotion gate:** `MORE DIAGNOSTIC OPTIMIZATION REQUIRED
BEFORE PROMOTION`. A future promotion study needs more clean-process matched
operator observations and an end-to-end resource/performance gate. The ordinary
full-model validator remains the sole authority.

## Validation-specific backlog

| Idea | Upside | Risk/complexity | Evidence required | Status |
| --- | --- | --- | --- | --- |
| Repeat exact-witness A/B on the same candidate with persisted source identity | Confirm native and total speedup is real | Medium | Multiple clean-process or same-lineage repetitions; exact source fingerprints; memory envelope | Completed; parity passed, total performance benefit not demonstrated |
| Prepared in-process validation context | Reduce repeated model/index construction | Medium | Safe clone/lifetime tests and process-recycle measurements | Minimal context and after-first strategy qualified for opt-in diagnostic multi-candidate sessions; not default authority |
| Immutable index/model-build reuse | Reduce Python preparation overhead | Low/medium | No stale DTO or lineage state; benchmark parity | Later |
| Differential-equivalence framework | Prevent false-positive fast acceptance | High | Randomized/adversarial candidate matrix against current validator | Small deterministic gate passed; broader corpus remains before any authority study |
| Deterministic full-schedule shadow validator | Potentially avoid native solve | Very high | Formal coverage of every hard/shared/special rule and exhaustive differential tests | Later; not justified yet |
| Dependency-aware shadow validation | Fast small-move validation | Very high | Complete transitive dependency closure and proof of global-resource coverage | Blocked pending dependency proof |
| Separate review/result work from authority validation | Remove non-authority overhead | Low/medium | Phase telemetry and API/result compatibility tests | Later |
| Candidate batching/tournament | Amortize validation | High | Every intermediate candidate needs exact authority; no unvalidated incumbent chaining | Not recommended now |

## Batching and scoped validation policy

An unvalidated candidate may not become the source incumbent for another search.
Generating several independent proposals from the same validated incumbent is
safe only if each proposal remains independent until one exact validator accepts
it. Periodic full validation after a chain of unvalidated moves is not safe.

Student-only, grade-only, or likely-affected-resource validation is not current
authority. Shared capacities, roster locks, student groups, online supervision,
paired half-semester resources, prerequisites, special commitments, and global
completion/objective facts can cross the apparent local boundary. A future
scoped validator must either prove a complete dependency closure or escalate to
the full validator.

## Recommended next step

The prepared context is now qualified for opt-in diagnostic multi-candidate
sessions, but a separate production-promotion study is still required before
making it a default validation path. That study should use more clean-process
operator sessions and a matched end-to-end performance/resource gate. The
ordinary full-model validator remains the sole authority. Exact-witness
validation remains diagnostic-only and is not reopened by this study.
Dependency-scoped and deterministic validators remain deferred; this work did
not prove that either can safely replace the full validator.
