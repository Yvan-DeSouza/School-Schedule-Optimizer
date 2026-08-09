# Architecture & Development Rules

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
