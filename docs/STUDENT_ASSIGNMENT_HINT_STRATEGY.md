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
destination summaries. Historical artifacts do not contain the live model's
complete variable namespace, so exact variable identities cannot be added to
them retroactively.

The repository now has an opt-in, research-only exact identity mapping in
`student_assignment/hint_observability.py`. When
`collect_hint_identity_telemetry=True`, the model-building path records each
student and semantic request/source key, each explicit assignment option, its
CP-SAT variable index, current or alternate section, incumbent variable value,
and actual incumbent hint value. The mapping uses model-builder objects and
variable indexes directly; it does not parse variable names, use fuzzy
matching, or assume numeric ID offsets. Duplicate semantic identities and
duplicate variable indexes are rejected, and the canonical row set receives a
stable fingerprint. The default remains off, so current hint behavior and
historical telemetry contracts are unchanged.

## Readiness boundary

Current hints remain the frozen control. No production hint change was made.
Exact target-release accounting is now technically observable, but it is not
experimentally qualified: before any heavy hint study, one small opt-in smoke
cell must capture the mapping, reproduce its fingerprint, and prove that merely
recording identity leaves the incumbent hint vector and candidate-authority
path unchanged. Only then may a separately approved fixed-state/fixed-scope
study compare current hints, no hints, and exact target-release hints.

Directional hints remain unready until pre-state destination candidates and
deterministic destination ranks are replayable. An oracle positive control
also remains later work. No hint experiment was run as part of the all-70
scope analysis, and the evidence does not claim that current hints are harmful.

Related ownership: [Target Selection](STUDENT_ASSIGNMENT_TARGET_SELECTION.md)
chooses the scope; [Adaptive Search](STUDENT_ASSIGNMENT_ADAPTIVE_SEARCH.md)
chooses the operator; [Validation](STUDENT_ASSIGNMENT_VALIDATION.md) owns
authority; and [Objective Semantics](STUDENT_ASSIGNMENT_OBJECTIVE_SEMANTICS.md)
owns counselor-weighted quality.
