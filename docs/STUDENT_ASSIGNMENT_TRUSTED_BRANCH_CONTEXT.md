# Student Assignment Trusted Branch Context

This is the canonical owner for carrying an already authoritative
student-assignment incumbent through one internal long-running optimization
branch. It explains the provenance boundary between public/untrusted input
and trusted internal continuation.

It does not redefine candidate validity or hard constraints; those belong to
[Student Assignment Validation](STUDENT_ASSIGNMENT_VALIDATION.md). It does not
own runtime phase meanings, resource monitoring, adaptive selection, or model
reuse boundaries; see [Runtime Pipeline](STUDENT_ASSIGNMENT_RUNTIME_PIPELINE.md),
[Observability and Monitoring](OBSERVABILITY_AND_MONITORING.md),
[Student Assignment Adaptive Search](STUDENT_ASSIGNMENT_ADAPTIVE_SEARCH.md),
and [Student Assignment Model Reuse](STUDENT_ASSIGNMENT_MODEL_REUSE.md).

## The authority boundary

Arbitrary semantic source decisions supplied to a public engine entry point
are untrusted. They may be stale, incomplete, mutated, produced under another
input/configuration, or inconsistent with the current full model. A public
entry therefore performs canonical full-model validation before treating the
source as an incumbent.

Trusted carry-forward is not a casual `skip_validation=True` flag. It is a
proof-carrying internal object produced only by the canonical validation path,
with enough identity to reject reuse when the model lineage changes.

Most importantly:

> Trusted incumbent reuse does **not** mean candidate validation is skipped.

Every newly generated candidate still goes through the unchanged canonical
full-model validator before it may advance the branch.

## `ValidatedStudentAssignmentBranchContext`

`ValidatedStudentAssignmentBranchContext` is defined in
`scheduling_engine/student_assignment/operator_session.py`. It carries:

- the detached input fingerprint;
- Objective Semantics version;
- the counselor objective-importance tuple;
- the production model-proto fingerprint;
- required decision-group variable indexes;
- the canonical source-decision tuple and source variable values;
- the solver witness returned by canonical full-model validation; and
- the authority marker `canonical_full_model_validator`.

The private `_from_canonical_validation` factory rejects construction without
the internal canonical-validation token. Callers cannot create a trusted
context merely by passing a source map or by claiming that a probe succeeded.

## Lifecycle

1. **Bootstrap.** Public input is prepared and the incoming source is
   materialized and fully validated. A context is created only from that
   validated source and solver witness.
2. **Probe.** The operator receives a compatible trusted incumbent. It clones
   the model and builds fresh target, neighborhood, bound, and hint state.
3. **Candidate validation.** A newly generated candidate is validated against
   the full model with a fresh validator. This is mandatory even when the
   mature incumbent was trusted.
4. **Strict adoption.** Only a complete, scope-consistent, zero-unmet,
   validated candidate that satisfies the existing strict-improvement rule
   can become the next incumbent.
5. **Context advancement.** The callback publishes a new context only after
   that authoritative adoption. Its source fingerprint therefore advances
   exactly with the adopted source.
6. **Fallback.** If any guard fails or a validation result is unresolved, the
   prior complete incumbent remains authoritative. The caller may record the
   failure and may fall back to ordinary validation; it must not weaken the
   guard to gain speed.

## Identity guards

Trusted reuse requires equality of all of the following:

| Identity | Why it matters |
| --- | --- |
| Input fingerprint | Prevents reuse across different detached school inputs. |
| Objective Semantics version | Prevents reuse under different objective meaning. |
| Counselor importance tuple | Prevents reuse under different counselor weighting. |
| Model-proto fingerprint | Prevents reuse after model structure/configuration changes. |
| Required decision-group indexes | Prevents reuse if completion ownership changes. |
| Supplied source fingerprint/content | Prevents reuse of a different incumbent. |

The source fingerprint is not advanced on discovery, on a probe result, on an
UNKNOWN, on a validation error, on a hard-invalid result, on a scope mismatch,
on an incomplete result, or on a non-improvement. Those outcomes cannot
advance authority.

## Result classifications

The validation meanings remain owned by
`STUDENT_ASSIGNMENT_VALIDATION.md`:

- `validated`: the full model established a complete valid candidate;
- `validation_unknown`: unresolved, never proof of infeasibility;
- `validation_error`: infrastructure/model failure, never authority;
- `hard_invalid`: authoritative proof that the fixed candidate is inconsistent.

An operator scope mismatch, incomplete candidate, or non-improving candidate
also cannot advance the context. The complete incumbent is retained. A
validation retry, where the existing validation contract permits one, remains
the same canonical validation path; trusted context does not turn a retry into
an alternate authority.

## Operator-switch safety

The context stores no operator-specific neighborhood constraints, bounds,
target scope, or hints. Each `probe_substantive_soft_tier` call clones the
model and rebuilds those facts for the selected operator. This prevents a
previous radius, grade scope, or target from contaminating a later operator.

The context may be reused across an operator switch only when the source and
all identity guards still match. The switch does not reuse the previous
operator’s dynamic constraints. Candidate validation independently rebuilds
its fresh full-model validation clone and source fixes.

## Telemetry boundary and invariants

Trusted-context telemetry is diagnostic and exception-safe. It may report
reuse classification, incoming/previous source fingerprints, identity facts,
validation facts, exception details, and hierarchical timing. A telemetry
serialization failure must not change selection, adoption, or authority.

The implementation invariants are:

1. Public arbitrary sources remain untrusted.
2. Context construction requires canonical-validation provenance.
3. Context advancement requires canonical candidate validation plus strict
   adoption.
4. UNKNOWN, errors, hard-invalid, scope mismatch, incomplete, and
   non-improving outcomes cannot advance it.
5. All model/objective/input/completion identities must match.
6. Operator-specific dynamic state is never carried in the context.
7. Full candidate validation remains mandatory.
8. The incumbent is retained on unresolved or failed outcomes.

## Current status and evidence

The implementation is diagnostic/internal in
`scheduling_engine/student_assignment/core.py` and
`adaptive_runtime.py`; no production adaptive-policy wiring was added. The
September 6, 2026 80-student bounded fixture showed complete, zero-unmet,
Objective Semantics v2, commitment-preserving parity and reused mature
validation on two post-bootstrap calls. That fixture is not target-scale
evidence.

The historical R64 lineage at
`C:\Users\desou\research_runs\v2_r64_s8_two_hour_long_horizon_20260906`
showed that the incoming source fingerprint matched the prior adopted source
through its successful chain, but it did not itself prove this reusable object
for arbitrary inputs. The real 1,400-student qualification is required before
making a target-scale performance claim.

That qualification completed on September 6, 2026 using the exact validated
reference target, four sequential R16 workload attempts, seed 101, and eight
CP-SAT workers. Attempt 0 performed the bootstrap mature validation. Attempts
1–3 carried the prior adopted source fingerprints forward and recorded no
mature-validation span; each retained probe execution and canonical candidate
validation. The complete target-scale evidence is in
`C:\Users\desou\research_runs\v2_target_scale_trusted_runtime_20260906`.

The compact qualification artifact records source-chain equality and the
successful trusted-guard outcome. It does not persist the full model-proto
fingerprint or every decision-group index for each attempt, so those facts are
reported as guard-passed but not reproduced verbatim in that artifact. Future
qualification runners should persist those exact identity values through the
context provenance callback.

Focused coverage is in
`scheduling_engine/tests/test_trusted_branch_context.py`. Tests must continue
to cover token provenance, every identity guard, source advancement only after
adoption, operator-switch freshness, unresolved-result retention, and
candidate validation authority.

Known limitation: the current context reuses the validated mature incumbent
but not the production/base model. Model reuse boundaries and promotion gates
are owned by [Student Assignment Model Reuse](STUDENT_ASSIGNMENT_MODEL_REUSE.md).
