# Student Assignment Objective Semantics

This document is the implementation contract for the versioned student-
assignment soft-objective semantics. The hard assignment model, fulfillment
priorities, special-commitment rules, approval workflow, and immutable snapshot
rules are unchanged by this document.

## Version selection

Each detached `StudentAssignmentInputDTO` carries
`objective_semantics_version`:

- `v1` preserves the historical label-to-lexicographic-tier behavior. Existing
  snapshots that do not contain the field load as `v1`.
- `v2` is an explicit opt-in semantics version. It uses the canonical
  counselor score and input-derived normalization described below.

The backend resolves transport settings into the DTO before the pure engine is
called. The DTO, result, quality facts, solver metadata, and immutable input
snapshot carry the selected version. A v1 snapshot is never silently
reinterpreted as v2.

The engine owns the mathematical constants and formulas in
`scheduling_engine/student_assignment/objective_semantics.py` and
`scheduling_engine/student_assignment/core.py`. Django owns only transport,
snapshot, and workflow persistence.

## Raw component definitions

The raw penalties are the existing solver expressions. They remain available
in v2 result and quality payloads so a counselor or benchmark can distinguish
the domain metric from its normalized contribution.

| Component | Raw expression | v2 denominator source |
| --- | --- | --- |
| Section utilization | Sum of `abs(section_count_left - section_count_right)` for every section pair within each delivery group | Sum of `max(left_capacity_max, right_capacity_max)` for the same pairs |
| Semester balance | Sum, for each non-Focus student, of `abs(semester_1_credit_units - semester_2_credit_units)` | Sum of the independently selectable normal/fixed credit units and two-credit Co-op units in the modeled input |
| Difficulty balance | Sum, for each non-Focus student, of the absolute difference between semester difficulty loads | Sum of the safe per-student maximum difficulty contributions represented by normal, fixed, Study, and Co-op sources |
| Category diversity | Existing similarity penalty for applicable course pairs sharing physical half-semester occupancy | Sum of the applicable catalog similarity scores |
| Sequence preferences | Number of applicable sequence opportunities not satisfied | Number of applicable sequence opportunities |

The denominator is an input-derived upper-bound scale, not the value of the
current candidate and not a benchmark constant. A component with no applicable
domain has denominator zero and normalized value zero. This makes empty,
single-course, missing-category, and no-sequence inputs deterministic without
inventing a penalty.

The normalization operation is integer and bounded:

```text
normalized = min(10_000, floor(raw_penalty * 10_000 / denominator))
```

The solver and quality evaluator preserve the raw aggregate and expose the
solver-authoritative normalized value. The evaluator's reconstructed values
are audit facts; they do not override CP-SAT.

## Canonical counselor importance

V2 has one canonical integer score from `0` through `10`. A score of zero
disables that component's contribution without changing hard feasibility. For
the first compatibility release, the existing labels are presets over this
same scale:

| Existing label | Canonical score |
| --- | ---: |
| `not_important` | 0 |
| `a_little_bit_important` | 2 |
| `important` | 5 |
| `really_important` | 8 |
| `extremely_important` | 10 |

The mapping is monotonic, uses the established endpoint meanings, and keeps
the intermediate labels understandable while allowing explicit v2 scores for
future counselor configuration. It is a compatibility preset, not a claim
that these five labels are the only future UI vocabulary.

For each component, the weighted v2 contribution is:

```text
normalized_component * canonical_importance_score
```

All five components are placed in one normalized soft tier. Equal scores
therefore give each component the same maximum normalized influence instead of
letting raw difficulty or category units dominate merely because their raw
numbers are larger. This is a mathematical scaling decision; it does not
change the counselor's hard/soft classification or add a new objective.

## Lexicographic placement of v2 objectives

V2 preserves the existing ordering of fulfillment decisions. Mandatory,
nominated-priority, primary-priority, and approved-backup fulfillment remain
higher-priority objectives and remain completion-defining as before.

After fulfillment, v2 adds one aggregate normalized soft tier. The existing
opaque deterministic tie-break remains after that tier. Schedule preservation
is intentionally separate: when enabled for a rerun, its existing
`none`/`slight`/`moderate`/`strong` semantics remain a distinct rerun-only
objective after the normalized soft tier. This avoids silently making a
historical-preservation preference part of the five-component school-wide
quality scale.

V1 retains its historical per-label tier behavior, including its separate
schedule-preservation treatment. No v2 facts are used to reinterpret a v1
result.

## Special commitments and hard-model invariance

Study, Focus, Co-op, online supervision, and half-semester commitments remain
represented by their existing hard assignment variables and occupancy rules.
They are not converted into ordinary sections or enrollments by v2.

- Study contributes its existing small difficulty signal only when the solver's
  occupied timeslot belongs to the relevant semester.
- Focus students remain excluded from semester, difficulty, and category
  balance comparisons.
- Co-op contributes its existing two-credit academic load and paired-block
  occupancy; it remains category-neutral and has no local section or teacher.
- Online courses retain their academic difficulty/category while supervision
  occupancy remains physical full-semester occupancy where the existing
  contract requires it.
- Half-semester courses retain their existing per-half occupancy and linked
  staffing semantics.

Normalization adds no hard constraint, capacity, lock, prerequisite,
eligibility, collision, or approval rule. A v2 result must satisfy exactly the
same hard model as a v1 result for the same input.

## Snapshot and API compatibility

The API may request `objective_semantics_version: "v2"` and may provide all
five explicit scores in `objective_importance_scores`. Label-only v2 requests
resolve through the compatibility presets above. Explicit numeric scores are
rejected for v1 rather than silently ignored.

The fields are JSON data in the existing immutable run input snapshot and
result metadata. No Django model field or migration is required. Approval
reloads the snapshotted version and scores and therefore checks the same
semantic contract that produced the candidate.

## Measurement and promotion boundary

`scheduling_engine/student_assignment/quality.py` remains measurement-only. It
reports raw metrics, normalized facts, importance scores, component
distributions, and Stage 1/Stage 2 changes; it never changes the solver model.
The v2 report identifier is `student_schedule_quality_v4`; the established v1
report identifier remains `student_schedule_quality_v3` for historical
compatibility.

The first v2 production-scale run is a new baseline. It must be compared with
the existing frozen v1 evidence only as a versioned semantic comparison, never
as if the raw objective values were expected to be numerically identical.

Student-targeted repair is a diagnostic search policy that consumes v2; its
ranking and operator contract is documented separately in
`STUDENT_ASSIGNMENT_SEARCH_STRATEGY.md`. Adaptive operator selection,
grade-bounded escape, and full-school unrestricted search remain outside the
current implementation. Any search policy may choose where to explore, but
CP-SAT and full hard-model validation remain the only authorities for accepting
a candidate.

## Why these normalization methods were selected

The v2 denominators were selected from the modeled input, not from one solved
candidate. The alternatives considered were:

- A theoretical range based on a universal school-wide constant was rejected:
  it would be difficult to explain, would not scale with the actual school
  input, and would make small fixtures artificially negligible.
- The current benchmark outcome was rejected as a denominator because it
  would make the semantics depend on one historical solution and could create
  circular comparisons.
- A simple count of applicable entities was insufficient for pairwise metrics:
  it ignores section capacities and catalog similarity magnitudes.
- The selected method uses objective-specific input facts: pair capacities for
  utilization, modeled credit/difficulty source maxima for student-local
  balance, applicable catalog similarity for category diversity, and the
  number of constructed sequence opportunities for sequence preferences.

These are deterministic upper-bound scales. They make each component's full
input-derived range occupy the same normalized integer range; they do not
promise that every particular local move has the same magnitude across
components. If future product evidence requires comparable *local opportunity*
rather than comparable full-range influence, that is a separate objective
semantics decision and must not be introduced as an unannounced solver tweak.

The common scale is `0..10,000`. With a counselor score of `0..10`, one
component contributes at most `100,000` per normalized tier expression. The
model uses integer floor division, so there are no floating-point coefficients
or division-by-zero behavior. Python integer construction and the bounded
normalized variables keep the v2 contribution far below CP-SAT's supported
integer range for the modeled objective family.

## Implementation ownership and data flow

The current implementation boundary is:

| Responsibility | Current owner |
| --- | --- |
| Transport validation for version and explicit scores | `backend/apps/scheduling/serializers.py`: `StudentAssignmentRunCreateSerializer` and `StudentAssignmentImportanceScoreSerializer` |
| Persistence/workflow defaults and immutable snapshot creation | `backend/apps/scheduling/services/student_assignment.py`: `create_student_assignment_run` |
| ORM-to-engine resolution of labels and scores | `backend/apps/scheduling/services/engine_adapter.py`: `load_student_assignment_input` |
| Immutable detached input | `scheduling_engine/dto.py`: `StudentAssignmentInputDTO` |
| Label/numeric canonical resolver and normalization helpers | `scheduling_engine/student_assignment/objective_semantics.py`: `resolve_importance_scores`, `normalize_penalty`, and `weighted_normalized_penalty` |
| Raw objective construction and v2 CP-SAT tier | `scheduling_engine/student_assignment/core.py`: `_solve_student_assignment` |
| Result objective and normalization facts | `scheduling_engine/student_assignment/core.py`: `_solver_objective_components` and result reconstruction |
| Measurement-only quality facts | `scheduling_engine/student_assignment/quality.py`: `evaluate_student_assignment_quality` |
| Snapshot reload and stale/approval workflow | `backend/apps/scheduling/services/student_assignment.py`: `_current_input_for_run`, `approve_student_assignment_run`, and the preview helpers |

The end-to-end flow is:

```text
counselor label or v2 score
    -> serializer transport validation
    -> service request and immutable input snapshot
    -> adapter resolves one canonical score mapping
    -> StudentAssignmentInputDTO
    -> pure-engine raw objective construction
    -> input-derived normalization and weighted v2 tier
    -> CP-SAT Stage 1/Stage 2
    -> solver-authoritative result and quality metadata
    -> review, drift check, and approval without re-solving
```

Backend transport constants mirror only the wire vocabulary; they do not
reimplement normalization mathematics. The engine remains Django-independent.

## Preservation and historical compatibility

Schedule preservation remains a separate rerun-specific semantic because it
answers a different question: how much accepted context should a controlled
rerun disturb? It is not silently folded into the school-wide five-component
canonical score. V1 runs retain their historical label-to-tier interpretation;
v2 metadata is explicit in DTOs, snapshots, results, and quality reports.

The scheduling-input semantic fingerprint intentionally identifies the modeled
school/request facts. The objective semantics version and score mapping are
separate immutable snapshot facts, so the same source input can be compared
across v1 and v2 without pretending that their objective vectors share a
numeric scale.

## Contributor and testing contract

Adding or materially changing a soft objective requires all of the following:

1. Define its raw domain expression and applicable population.
2. Define an input-derived denominator and explain why it is principled.
3. Define its integer bound, rounding, empty-domain, and overflow behavior.
4. Define its canonical `0..10` contribution and hard-feasibility invariants.
5. Add deterministic normalization, label/numeric, solver-tradeoff, and
   hard-constraint regression tests.
6. Add raw, normalized, importance, and weighted facts to diagnostic output.
7. Update this contract and the measurement documentation.

The counselor-facing contract is the label and/or canonical score plus the
human-readable raw quality facts. Internal CP-SAT variable indices,
coefficients, and solver implementation details are not counselor-facing API
values.

Required test categories include formula and determinism tests, ordering and
opaque-ID independence, bounded integer behavior, zero-through-ten profiles,
label/numeric equivalence, genuine soft tradeoffs, special-commitment and hard
constraint invariance, snapshot/API compatibility, quality reconstruction,
and bounded compatibility checks for retained diagnostic search operators.

## Non-goals

This document does not define or implement the student-targeted search policy,
adaptive operator allocation, grade-bounded unrestricted search, or full-school
global escape. The implemented targeted diagnostics are defined separately and
must consume this objective contract rather than alter it. Future search-policy
experiments must follow the same boundary.
