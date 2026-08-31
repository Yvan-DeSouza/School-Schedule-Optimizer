# Architecture & Development Rules

This document owns cross-layer engineering rules. It does not replace the
accepted stage decision records or the specialized student-assignment
objective, validation, quality, worker, and search documents.

This project is a counselor-controlled school scheduling system. The codebase is
allowed to grow, but new work should keep the current boundaries explicit so
future scheduling stages do not reimplement the same rules in different places.

## Layer Ownership

- Django owns identity, authorization, validation orchestration, persistence,
  audit records, and transactional writes.
- DRF views handle HTTP concerns only: authentication/authorization, request
  parsing, serializer selection, service calls, and responses.
- Services own domain workflows: state transitions, multi-model writes,
  transactions, current-state revalidation, and audit side effects.
- Serializers validate transport shape and reusable field/cross-field rules.
  Business workflow rules belong in services so non-HTTP callers reuse them.
- Models may protect local invariants and immutable audit records, but they
  should not become orchestration services.

## Domain Values

- Shared school-wide values such as grades, room types, semesters, and A-D
  blocks live under `backend.apps.common`.
- Role values live under `backend.apps.people`.
- Course/catalog/offering/request values live under `backend.apps.courses`.
- Qualification values live under `backend.apps.constraints`.
- Planning, roster, backup, and section-lifecycle values live under
  `backend.apps.scheduling`.
- Shared Django-free engine vocabulary and solver constants live in
  `scheduling_engine/constants.py`; stable engine diagnostic codes live in
  `scheduling_engine/diagnostics.py`.
- `backend.apps.common.constants` remains a compatibility export for older
  imports, but new code should import from the domain that owns the value.

## API And Authorization

- Every API endpoint must declare either a resource policy or a named action
  policy, except explicit authentication/self endpoints.
- Resource-policy querysets must apply policy scope before client query
  parameters. Client filters may narrow authorized data; they must never broaden
  it.
- Resource access rules should be tested in two places: policy-level tests for
  the rule matrix and endpoint-level tests proving the view selected that policy.
- Unknown, unauthenticated, or undeclared access fails closed.

## Data Access

- Reusable query rules belong in selector modules when more than one service,
  view, or adapter needs them.
- Active solver input must exclude retired sections and cancelled/retired
  delivery state through shared selectors.
- Section fixed-context rules must be read through the section-state helper.
  Future placement, teacher-assignment, student-assignment, and override code
  must not invent a separate definition of "fixed."

## Scheduling Engine Boundary

- `scheduling_engine` must not import Django, DRF, ORM models, or backend apps.
- The backend adapter loads ORM data into immutable DTO snapshots.
- Scheduling application services may invoke pure engine entrypoints with DTOs
  loaded by the adapter. Non-scheduling Django apps must not import
  `scheduling_engine` directly.
- Engine solvers should depend on public shared helper modules, not private
  underscore functions from sibling solver modules.
- Versioned student-assignment objective mathematics, including input-derived
  normalization and canonical counselor scores, belongs in Django-free engine
  modules. Backend adapters may resolve transport settings into immutable DTO
  fields, but services must not duplicate objective formulas or reinterpret a
  snapshotted semantics version.
- New solver stages return recommendations first. Operational writes happen
  only after an explicit authorized approval workflow.
- Annual placement locks are pre-section timing decisions keyed by academic
  year, physical delivery group, and stable annual ordinal. Do not make
  `Section.semester` nullable or create fake Section rows to represent them.
- A staffing-feasibility witness proves a legal future teacher assignment but
  is never a persisted or API-returned teacher recommendation. Named teacher
  assignment remains its own later reviewed stage.
- Rooms are not inferred by semester/A-D placement. A timeslot-only accepted
  schedule is fixed context for later stages; room selection remains explicit.
- Named teacher assignment consumes accepted timeslot-only sections and must
  not alter their semester/block or introduce a room/student decision.
- A `Section.teacher` is fixed teacher context; `SectionLock.locked_teacher`
  is an exact candidate requirement. A timeslot-only lock is not a teacher lock.
- Ready rosters require explicit annual and semester capacities. Availability
  is available by default; only explicit unavailable records remove a slot.
- Named teacher candidates are immutable runs reviewed before approval. A
  partial named-teacher result is diagnostic-only and cannot be approved.
- Continuous student-assignment operator sessions are diagnostic-only search
  experiments. Session-static model/context data may be reused, but each
  attempt must rebuild its incumbent-dependent bounds, hints, target scope,
  and validation state. CP-SAT plus the unchanged full-model validator remain
  the only authority for adopting a candidate; an UNKNOWN result is never
  treated as a proof of exhaustion or infeasibility.
- When transferring CP-SAT hints from a solved model into a cloned diagnostic
  model, transfer solution values by response/proto variable index. Never call
  `CpSolver.Value()` with a variable owned by a different model. If a cloned
  probe adds variables that do not exist in the source model, initialize those
  probe-only variables conservatively rather than treating them as source
  assignments. This preserves safe model boundaries during diagnostic search.

## Immutable Workflow Records

- Planning runs, approvals, request resolutions, reconciliation rows, and
  decision/audit rows are append-only.
- A correction creates a new record or explicit reconciliation/override; it does
  not rewrite prior audit facts.
- Approval services must revalidate current state inside the write transaction
  instead of trusting an earlier preview.

## Qualification Rules

- Teacher eligibility uses normalized qualifications and compiled eligibility
  sets.
- Raw Aspen/source text is provenance only. It must not become solver matching
  logic.
- Grade 11-12 statutory qualification behavior must fail closed when required
  normalized rules are missing.

## Comments

- Add comments where they explain domain decisions, invariants, transaction
  boundaries, solver modeling choices, or non-obvious compatibility behavior.
- Avoid comments that restate a single line of code. Comments should save a
  future developer from rediscovering the rule behind the code.
