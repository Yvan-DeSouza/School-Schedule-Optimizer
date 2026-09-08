# Student Assignment CP-SAT Hint Strategy

This document owns the separate research question: once an operator and exact
target scope have been selected, how is CP-SAT guided inside that neighborhood?
It does not select an operator, select students, change Objective Semantics v2,
or weaken candidate validation.

## Current behavior

The current path is incumbent-derived complete hints. In
`student_assignment/solver.py`, `set_solver_hints` clears the probe model's
old hints and adds the incumbent value for every model variable available from
the validated source response. Diagnostic clone-only changed-student
indicators receive zero because the incumbent has no changed indicators. The
target source decisions are hinted toward the incumbent but remain eligible to
change under the R16/S4 neighborhood constraints; non-target decisions are
restricted by the operator's existing neighborhood construction.

Hints guide search; they do not fix variables unless a separately named
validated initial-hint path explicitly uses `fix_variables_to_their_hinted_value`.
The normal diagnostic target probe does not turn the current hint into a hard
assignment. CP-SAT still produces the candidate, and the unchanged full-model
validator still checks completeness, all hard constraints, commitments, and
the strict v2 improvement requirement before adoption.

The hint list is rebuilt on a fresh probe/model clone. Trusted branch context
can carry a validated incumbent between compatible diagnostic attempts, but it
does not authorize an accumulating or cross-scope hint state. A target snapshot
is therefore not a hint snapshot: target selection chooses the neighborhood;
hinting supplies incumbent guidance within it.

## Counselor-facing meaning

There is no counselor-facing soft constraint called “hint strength,” and no
soft preference is changed by the current hint implementation. Counselor
importance scores, special commitments, and the five Objective Semantics v2
components remain the authoritative schedule-quality contract. Hints only affect
which feasible qualifying candidate CP-SAT may find first inside the already
defined search neighborhood. More guidance is not automatically better and
more workers are not monotonically better; parallel CP-SAT changes the search
portfolio and candidate distribution.

## Research variants that are not currently enabled

Future matched experiments may compare, with operator, scope, source, seed,
workers, and validation held fixed:

- current incumbent-derived hints;
- no hints;
- target-release hints, where only target-local guidance is released or
  changed under a precisely recorded contract;
- directional hints based on replayable destination alternatives;
- an oracle positive control using a known authoritative target candidate.

These are separate hint treatments. A target-policy experiment must not silently
change hints, and a hint experiment must not silently change target selection.
No such comparison is qualified by the current one-worker screen. The current
artifacts record semantic source decisions, selected scopes, and compact
destination summaries, but they do not provide a deterministic mapping for
every student/request/source key to every CP-SAT variable index, assignment
option, and destination-variable hint. Exact per-variable hint identity and
destination predictability are therefore incomplete.

## Readiness boundary

Current hints remain the frozen control for the eight-worker jackpot calibration.
No production hint change was made. A current/no-hint/target-release study is
not ready until variable identity, release accounting, and per-variable hint
fingerprints are durable. Directional hints are not ready until pre-state
destination candidates and deterministic destination ranks are replayable. An
oracle positive control is not ready until a known semantic target candidate
can be mapped exactly to its CP-SAT destinations and independently validated.

The forensic evidence records this as three separate “not ready” findings. It
does not claim that current hints are harmful; it identifies missing
observability. The next hint study should use clean matched processes and
retain candidate discovery, validation, adoption, source-decision, destination,
and worker/resource facts without changing production defaults.

Related ownership: [Target Selection](STUDENT_ASSIGNMENT_TARGET_SELECTION.md)
chooses the scope; [Adaptive Search](STUDENT_ASSIGNMENT_ADAPTIVE_SEARCH.md)
chooses the operator; [Validation](STUDENT_ASSIGNMENT_VALIDATION.md) owns
authority; and [Objective Semantics](STUDENT_ASSIGNMENT_OBJECTIVE_SEMANTICS.md)
owns counselor-weighted quality.
