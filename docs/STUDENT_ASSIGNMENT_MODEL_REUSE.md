# Student Assignment Model Reuse

This is the canonical owner for which student-assignment model structures may
be reused across repeated optimization attempts and which state must be
rebuilt or refreshed. It separates the current trusted-incumbent optimization
from future base-model or validation-preparation work.

It does not own runtime phase accounting, resource semantics, candidate
authority, objective mathematics, or adaptive selection. See
[Runtime Pipeline](STUDENT_ASSIGNMENT_RUNTIME_PIPELINE.md),
[Student Assignment Validation](STUDENT_ASSIGNMENT_VALIDATION.md),
[Student Assignment Objective Semantics](STUDENT_ASSIGNMENT_OBJECTIVE_SEMANTICS.md),
and [Student Assignment Adaptive Search](STUDENT_ASSIGNMENT_ADAPTIVE_SEARCH.md).

## Current status

| Reuse boundary | Status |
| --- | --- |
| Trusted mature-incumbent authority reuse | Implemented internally after canonical validation and identity checks |
| Production/base-model construction reuse | Not promoted or implemented |
| Candidate full-validator prepared-context reuse | Not promoted or implemented as an authority shortcut |
| Candidate validation itself | Always mandatory for a newly generated candidate |

The trusted branch context is a Category B authority-carry-forward mechanism,
not a general model cache. It reuses a validated incumbent witness while the
current public session still rebuilds its production/base model.

## Static and dynamic state

Potentially reusable input-static structures include:

- detached immutable input and its fingerprint;
- variables, domains, and source-decision ownership;
- sections, requests, timeslots, prerequisites, conflicts, and capacities;
- locks/fixed rows and other hard-constraint structure;
- Objective Semantics metadata, normalization denominators, and counselor
  importance values;
- an immutable base-model proto, if structural parity is demonstrated.

State that depends on the current incumbent or attempt must be refreshed:

- current source decisions and source variable values;
- incumbent solver witness and transferred hints;
- objective-bound targets and protected objective values;
- selected target students/grade and target-derived facts;
- operator radius and neighborhood restrictions;
- candidate domains altered by the current operator, if any;
- fresh candidate-validation source fixes and validator solver state.

The distinction is important: a static model clone can be a safe starting
point only if all dynamic facts are applied to a fresh clone and no previous
probe constraints remain in it.

## Current reuse architecture

The trusted context carries validated source values and the canonical solver
witness, together with input/objective/model/completion identities. On the
next call, the engine checks the supplied source and these identities before
using the context for mature-incumbent continuation. If a check fails, the
caller must use ordinary validation or retain the incumbent; it must not force
reuse.

Within each probe, `probe_substantive_soft_tier` clones the model and rebuilds
completion constraints, neighborhood constraints, objective bounds, hints,
solver setup, and the native search solve. The context has no operator-specific
state, so switching operators cannot inherit a stale radius, scope, bound, or
hint. The candidate validator creates its own fresh full-model clone, applies
completion constraints and source equalities, and runs a fresh bounded CP-SAT
validation solve.

## Prepared/base-model concept

A future prepared base-model context would need to contain only immutable
input/configuration structure. It would need explicit identity checks and a
clone-per-attempt boundary. Every attempt would then apply fresh source
values, target scope, operator radius, neighborhood restrictions, objective
bounds, and hints. Probe-only variables or constraints must never mutate the
prepared base. Candidate validation would still use the unchanged full-model
authority path unless a separately proven prepared validator were introduced.

Structural parity requires comparing the prepared model proto with the model
currently built by `_solve_student_assignment`, including variable indexes,
domains, hard constraints, objective metadata, and completion ownership. A
timing improvement alone is not evidence of equivalence. Tests must prove
that cloned prepared models produce the same feasible assignments, objective
vectors, source identity, hard-constraint behavior, and validation outcomes
under operator switches and unresolved results.

## Candidate-validation reuse boundary

The validator’s clone, completion constraints, source fixes, solver setup,
native CP-SAT, and result extraction are separately timed. A previous
candidate’s validation result may not be cached for a new candidate, and a
witness-only check is not authority. Any prepared validation context would
need to preserve the full model and all authority semantics; it is not
promoted by the current code.

## Empirical evidence and promotion gates

In the September 6, 2026 R64 target-scale reference study, production/base
model construction consumed 393.183 seconds across 18 public sessions, while
mature-source validation consumed 1,561.047 seconds and candidate validation
consumed 1,613.089 seconds. Those are dated external measurements, not fixed
costs. The bounded 80-student trusted-context fixture demonstrated mature
validation reuse but did not measure target-scale construction.

The September 6, 2026 target-scale qualification measured 41.519 seconds of
base-model construction across three steady-state attempts, 6.30% of their
additive root wall. Because that is meaningful enough to inspect but the
structural equivalence boundary for a prepared base model has not been proven,
the current base-model assessment is:

> **D — STRUCTURAL SAFETY / PARITY IS NOT YET SUFFICIENT TO CONSIDER REUSE.**

No base-model reuse was implemented. Candidate validation totaled 245.330
seconds across four candidates; 95.55% was native authoritative validation
CP-SAT and 4.45% was reconstructive/setup/extraction work. Its assessment is:

> **A — VALIDATION IS DOMINATED BY NATIVE CP-SAT; NO MAJOR INFRASTRUCTURE
> OPTIMIZATION IS JUSTIFIED.**

The target-scale root self/exclusive residual was 10.85% of steady-state root
wall. Additional quality-evaluation timing has been instrumented after that
measurement, so the residual should be decomposed in a later bounded runtime
check before any base-model optimization is designed.

The target-scale qualification required to classify the next opportunity is
currently pending the host-memory launch gate. Until it runs, no base-model
reuse classification is promoted and no model-reuse implementation should be
started. After measurement, the assessment must distinguish:

- large, well-defined, safely removable base-model work;
- meaningful but non-blocking base-model work;
- negligible base-model work; or
- insufficient structural safety/parity for reuse.

The same evidence must classify candidate validation as native authoritative
CP-SAT-dominated, meaningfully reusable static construction, or insufficient
breakdown. A large Python phase is not automatically safe to remove; authority
and structural parity are gating requirements.

## Explicit non-goals

This document does not authorize changing Objective Semantics v2, counselor
weights, hard constraints, CP-SAT parameters, adaptive coefficients, operator
portfolios, candidate authority, production wiring, or database schemas. It
does not turn a diagnostic context into a public validation bypass. It does
not claim that more runtime attempts produce a better schedule.
