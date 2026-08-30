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

1. **Primary: native CP-SAT derived-state reconstruction and presolve/search.**
   The exact target candidate fixed all source variables, yet CP-SAT still
   consumed roughly 70--75 seconds in normal validation. The witness A/B shows
   that supplying a complete original-model response can reduce this cost, but
   the target experiment is not yet a repeated authority-parity qualification.
2. **Secondary: full-model construction and constraint-fix preparation.**
   Target model construction was about 16.63 s in the detached exact replay.
   Witness mode added 167,259 equality constraints and took about 9.96 s for
   source/witness fixing, so fewer native seconds do not automatically mean a
   proportionally smaller total operation.
3. **Lower priority: clone, completion constraints, extraction, quality, and
   DTO reconstruction.** These were generally sub-second to low-single-digit
   seconds in measured target runs, except for completion/fix construction in
   the witness A/B.

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

## Validation-specific backlog

| Idea | Upside | Risk/complexity | Evidence required | Status |
| --- | --- | --- | --- | --- |
| Repeat exact-witness A/B on the same candidate with persisted source identity | Confirm native and total speedup is real | Medium | Multiple clean-process or same-lineage repetitions; exact source fingerprints; memory envelope | Investigate now |
| Prepared in-process validation context | Reduce repeated model/index construction | Medium | Safe clone/lifetime tests and process-recycle measurements | Later |
| Immutable index/model-build reuse | Reduce Python preparation overhead | Low/medium | No stale DTO or lineage state; benchmark parity | Later |
| Differential-equivalence framework | Prevent false-positive fast acceptance | High | Randomized/adversarial candidate matrix against current validator | Investigate now before authority promotion |
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

Repeat the exact-witness A/B with a durable result record that stores both
complete semantic source fingerprints and witness telemetry, preferably using
one captured candidate/model lineage for both validation modes. Require
classification parity across valid, hard-invalid, incomplete, altered-auxiliary,
special-commitment, online, half-semester, Study, Focus, Co-op, lock, and
capacity cases. Compare native CP-SAT, preparation, total wall time, and memory
over multiple repetitions.

Only if that gate passes should a separate authority-parity qualification study
consider production use. Otherwise, the next fallback is prepared in-process
full-validator reuse and more native CP-SAT forensics. The current full-model
validator remains the sole authority.
