# Project State Summary

Snapshot date: 2026-08-11

This document is a repository-backed orientation snapshot. It is based on the
actual code, tests, and docs in this checkout, not on prior chat memory.

## Current Snapshot

| Area | State today | Evidence |
| --- | --- | --- |
| Core backend, auth, and policy system | Implemented | `backend/apps/access/*`, `backend/apps/people/*`, `backend/tests/test_access_policies.py`, `backend/tests/test_api_authorization_contracts.py` |
| Course offering, backup, combination, and demand workflows | Implemented | `backend/apps/courses/services/offerings.py`, `backend/apps/courses/services/demand.py`, `backend/tests/test_upstream_planning_workflow.py` |
| Section-count planning, approval, and reconciliation | Implemented | `backend/apps/scheduling/services/section_planning.py`, `backend/apps/scheduling/services/section_reconciliation.py`, `backend/tests/test_section_planning_api.py`, `backend/tests/test_section_reconciliation_api.py` |
| Section budgeting and staffing-feasible physical counts | Implemented | `backend/apps/scheduling/services/section_budget_planning.py`, `backend/apps/scheduling/services/staffing_planning.py`, `backend/tests/test_upstream_planning_workflow.py` |
| Semester/A-D placement with staffing feasibility | Implemented | `backend/apps/scheduling/services/section_placement.py`, `scheduling_engine/section_placement.py`, including shared online-supervision placement and sequential paired half-semester sections |
| Named teacher assignment | Implemented | `backend/apps/scheduling/services/teacher_assignment.py`, `scheduling_engine/teacher_assignment.py`, including workload-safe online supervision without subject qualification |
| Student assignment, special commitments, and controlled reruns | Implemented | `backend/apps/scheduling/services/student_assignment.py`, `backend/apps/scheduling/services/student_special_commitment_locks.py`, `backend/apps/scheduling/services/online_supervision.py`, `scheduling_engine/student_assignment.py`, `backend/tests/test_special_student_schedule_commitments.py`, `scheduling_engine/tests/test_student_assignment.py` |
| Room assignment | Not implemented | Deferred by the placement and teacher-assignment decisions |
| Frontend | Not started | `docs/Implementation_Roadmap.md` phase 6, no frontend directory in repo |

The system is now a counselor-controlled decision-support pipeline with
immutable review/approval checkpoints. It does not silently turn solver output
into operational state.

## 1. System Overview & Product Philosophy

This is a school scheduling system for counselors, staff, and directors. The
product does not try to fully automate scheduling in one step. Instead, it
breaks the work into reviewed stages:

`demand -> offering changes -> normal-section and online-supervision capacity planning -> staffing feasibility -> semester/A-D placement -> named teacher/supervisor assignment -> student enrollment and commitment assignment -> later room/conflict work`

That staged shape is visible in `README.md`, `docs/Implementation_Roadmap.md`,
`docs/decisions/semester-placement-and-staffing-feasibility.md`, and
`docs/decisions/named-teacher-assignment.md`.

The key philosophy is counselor control plus auditability:

- The engine produces recommendations, not operational writes.
- An approval is explicit and transactional.
- Existing history is preserved rather than overwritten.
- The system prefers fail-closed behavior over guessing.
- Stable machine-readable codes matter more than English error text.

The code already reflects that philosophy in the immutable run/approval models,
the pure `scheduling_engine` package, and the policy-first API layer.

## 2. Architecture & Layer Boundaries

The current architecture is deliberately layered.

| Rule | Current owner | Evidence |
| --- | --- | --- |
| Django owns persistence, transactions, audit rows, and operational writes | Django services and models | `backend/apps/scheduling/services/*.py`, `backend/apps/courses/services/*.py`, `backend/apps/people/services/*.py`, `backend/apps/control/services/locks.py` |
| DRF views stay thin and delegate workflow logic | `backend/apps/*/views.py` | `backend/apps/courses/views.py`, `backend/apps/constraints/views.py`, `backend/apps/scheduling/views.py`, `backend/apps/people/views.py` |
| Serializers validate transport shape; workflow rules live in services | DRF serializers + service layer | `backend/apps/*/serializers.py`, `backend/apps/scheduling/services/run_contracts.py` |
| The scheduling engine is Django-independent | `scheduling_engine/` | `scheduling_engine/README.md`, `backend/apps/scheduling/services/engine_adapter.py`, `backend/tests/test_engine_boundary_contracts.py` |
| The adapter is the ORM-to-DTO translation boundary | `backend/apps/scheduling/services/engine_adapter.py` | same file plus the engine DTOs in `scheduling_engine/dto.py` |
| Policy filtering happens before client query filtering | `backend/apps/access/viewsets.py` | `backend/tests/test_api_authorization_contracts.py` |
| Shared selectors own reusable query logic | selectors modules | `backend/apps/courses/selectors.py`, `backend/apps/scheduling/services/engine_adapter.py` |
| Fixed-context rules have one shared definition | section-state service | `backend/apps/courses/services/section_state.py`, `docs/decisions/section-lifecycle.md` |
| New code should import owned constants from the owning domain module | domain constant modules | `backend/apps/common/school_values.py`, `backend/apps/people/constants.py`, `backend/apps/courses/constants.py`, `backend/apps/constraints/constants.py`, `backend/apps/scheduling/constants.py`, `backend/apps/common/constants.py` |

Important boundary details:

- `backend/apps/access/permissions.py` and `backend/apps/access/viewsets.py`
  fail closed if a view forgets to declare a policy.
- `backend/apps/scheduling/services/engine_adapter.py` is the only module that
  should know both ORM models and engine DTOs.
- `backend/apps/courses/services/section_state.py` defines what counts as
  fixed context for sections so placement, assignment, and reconciliation do
  not diverge.
- `backend/apps/common/constants.py` is now a compatibility export, not the
  authoritative home for every value.

## 3. API Contract Philosophy

The runtime API contract lives in DRF serializers, view routes, and tests. That
is the actual contract the backend enforces today.

`docs/API_Contract_Strategy.md` makes the practical rule explicit:

- serializers remain the runtime source of request/response shape;
- every backend API view must declare a resource policy or action policy;
- tests must cover role access and stable diagnostic/error codes.

The API contract also depends on stable codes rather than English prose:

- `scheduling_engine/diagnostics.py` defines durable solver/workflow codes;
- tests assert on those codes;
- view code can improve wording without breaking clients;
- future frontend work should key off codes, not human text.

Before frontend work becomes serious, the project should publish either:

1. a generated OpenAPI schema, or
2. a tested `docs/api.md` built from real serializers and endpoint tests.

The current repository does not yet have a frontend or a published machine-
readable contract, so the serializer-and-test contract is still the source of
truth.

## 4. Domain Glossary

| Term | Meaning today | Canonical file(s) |
| --- | --- | --- |
| Active section | A section that participates in operational planning and solver input | `backend/apps/scheduling/constants.py`, `backend/apps/courses/services/section_state.py`, `docs/decisions/section-lifecycle.md` |
| Retired section | A preserved historical section that is read-only and excluded from active solver input | same as above |
| Annual virtual slot | A stable pre-section identity like "Physics slot 3" used before a real `Section` row exists | `backend/apps/scheduling/models.py`, `docs/decisions/semester-placement-and-staffing-feasibility.md` |
| Materialized annual section | A real `Section` row created only after annual placement approval | `backend/apps/scheduling/models.py`, `backend/apps/scheduling/services/section_placement.py` |
| Staffing witness | An anonymous hidden teacher-load proof that shows a placement could later be staffed | `scheduling_engine/section_placement.py`, `backend/apps/scheduling/services/engine_adapter.py` |
| Named teacher assignment | The later stage that writes `Section.teacher` after review | `backend/apps/scheduling/services/teacher_assignment.py`, `docs/decisions/named-teacher-assignment.md` |
| Fixed context | A section that downstream automation must not silently move or replace | `backend/apps/courses/services/section_state.py`, `docs/decisions/section-lifecycle.md` |
| DeliveryGroup | One physical delivery identity shared by one or more course offerings | `backend/apps/courses/models.py`, `backend/apps/courses/services/offerings.py` |
| CourseCombinationRule | An approved counselor rule that allows multiple courses to share one physical class | `backend/apps/courses/models.py`, `backend/apps/courses/services/offerings.py` |
| A-D block | A recurring timetable block inside a semester, not a calendar date | `backend/apps/common/school_values.py`, `backend/apps/scheduling/models.py`, `backend/apps/scheduling/services/section_placement.py` |
| CourseConflictMatrix | A yearly counselor-managed pair matrix for co-request overlap and score overrides | `backend/apps/constraints/models/course.py`, `backend/apps/constraints/conflict_matrix.py` |
| Ready roster | The staffing roster state that confirms teacher membership plus explicit semester and annual capacity rows | `backend/apps/scheduling/models.py`, `backend/apps/scheduling/services/staffing_configuration.py` |
| Planning run | An immutable solver snapshot and result for a single stage | `backend/apps/scheduling/models.py`, `backend/apps/scheduling/services/*.py` |
| Approval | The immutable human decision that turns a reviewed recommendation into operational state | same as above |
| Scope | The set of items the current solver is allowed to change; everything else is fixed context | `backend/apps/scheduling/services/section_placement.py`, `backend/apps/scheduling/services/teacher_assignment.py`, `backend/apps/courses/services/section_state.py` |
| Effective course difficulty | A frozen 0--100 scheduling estimate: metadata fallback or recency-weighted student-relative historical evidence unless a counselor sets a manual override | `backend/apps/courses/services/difficulty.py`, `backend/apps/courses/models.py`, `backend/apps/scheduling/services/engine_adapter.py` |
| Course category relationship | An optional school-wide similarity score for one unordered pair of distinct catalog categories | `backend/apps/courses/models.py`, `scheduling_engine/student_assignment.py` |

## 5. Module Map

| If you need to do X | Canonical place | Why here |
| --- | --- | --- |
| Resolve a user role or ownership rule | `backend/apps/people/roles.py`, `backend/apps/access/*` | Central role resolution and fail-closed policy logic live here |
| Enforce policy-first queryset filtering | `backend/apps/access/viewsets.py` | Keeps authorization ordering consistent before query params |
| Validate teacher qualification eligibility | `backend/apps/constraints/qualification_review.py`, `backend/apps/constraints/services.py`, `scheduling_engine/constraint_compiler.py` | Normalized eligibility and fail-closed senior-course behavior are centralized |
| Manage course cancellations, restorations, combinations, or separations | `backend/apps/courses/services/offerings.py` | This owns the year-specific offering lifecycle |
| Explain whether a section is fixed context | `backend/apps/courses/services/section_state.py` | One shared answer prevents solver drift |
| Reconcile a newer approved section count plan against existing sections | `backend/apps/scheduling/services/section_reconciliation.py` | This is the explicit replacement workflow |
| Configure capacity profiles and course priority profiles | `backend/apps/scheduling/services/planning_configuration.py` | Shared planning configuration is mutable and future-facing |
| Manage teacher planning readiness and capacities | `backend/apps/scheduling/services/staffing_configuration.py` | Ready-roster validation and capacity invalidation live here |
| Run section-count planning | `backend/apps/scheduling/services/section_planning.py` | Immutable base/what-if section planning belongs here |
| Run teacher-independent budget planning | `backend/apps/scheduling/services/section_budget_planning.py` | This is the physical budget and backup-demand workflow |
| Run staffing-feasible physical planning | `backend/apps/scheduling/services/staffing_planning.py` | This proves the selected counts can be staffed |
| Build semester/A-D placement input snapshots | `backend/apps/scheduling/services/engine_adapter.py` | ORM-to-DTO translation belongs at the boundary |
| Build and refresh the annual conflict matrix | `backend/apps/constraints/conflict_matrix.py` | The yearly pair matrix is counselor-managed policy |
| Manage annual placement locks | `backend/apps/scheduling/services/annual_placement_locks.py` | Pre-section locks are a dedicated scheduling workflow |
| Run named teacher assignment | `backend/apps/scheduling/services/teacher_assignment.py` | Named teacher assignment is a separate reviewed stage |
| Archive or restore teachers | `backend/apps/people/services/teacher_directory.py` | Teacher status changes are audited state transitions |
| Review teacher qualifications | `backend/apps/constraints/qualification_review.py` | Qualification verification invalidates ready rosters |
| Aggregate raw demand | `backend/apps/courses/services/demand.py` | This is the counselor-facing request summary API |

## 6. Implemented Capabilities - Technical Breakdown

### Auth, roles, and authorization

Role resolution is centralized in `backend/apps/people/roles.py`. Domain
profiles (`student_profile`, `teacher_profile`, `counselor_profile`) take
precedence over general flags, then `role_profile`, then `is_superuser`, then
`is_staff`, and anything else falls back to `unknown`.

Authorization is policy-driven:

- `backend/apps/access/permissions.py` converts DRF requests into resource or
  action policy checks.
- `backend/apps/access/resource_policies/*.py` define read/write scope by role.
- `backend/apps/access/action_policies/scheduling.py` splits solver runs from
  approval/status actions.
- `backend/tests/test_access_policies.py` and
  `backend/tests/test_api_authorization_contracts.py` prove fail-closed
  behavior and explicit policy declarations.

Current practical result:

- students can only manage their own requests;
- teachers can only manage their own nested records and read assigned sections;
- counselors, staff, and directors control planning resources;
- anonymous and unknown roles fail closed.

### Qualification architecture

Qualification matching is normalized and fail-closed.

Key files:

- `backend/apps/constraints/models/base.py`
- `backend/apps/constraints/models/teacher.py`
- `backend/apps/constraints/models/course.py`
- `backend/apps/constraints/qualification_review.py`
- `backend/apps/constraints/services.py`
- `scheduling_engine/constraint_compiler.py`
- `backend/apps/scheduling/services/engine_adapter.py`

What it does today:

- teacher qualifications are stored as normalized `Qualification` records plus
  provenance fields;
- course requirements split into required and preferred enforcement;
- Grade 11-12 courses fail closed if required normalized eligibility is absent;
- Grade 7-10 courses remain legally permissive, with qualifications acting as
  softer planning evidence;
- qualification review can verify/reject evidence and invalidate ready rosters.

Tests that prove it:

- `backend/tests/test_constraints_api.py`
- `backend/tests/test_engine_adapter.py`
- `scheduling_engine/tests/test_constraint_compiler.py`

### Offering, backup, and combination planning

Offerings are year-specific and can be cancelled, restored, combined, or
separated through audited services in `backend/apps/courses/services/offerings.py`.

What exists today:

- yearly course-offering state;
- explicit cancellation/restoration with reasons;
- approved combination rules and delivery groups;
- safe combination suggestions that do not write automatically;
- a backup-policy workflow that can promote an alternate only when the primary
  course was cancelled and an available backup offering exists.

Section-budget planning and staffing-feasible physical planning are separate:

- `backend/apps/scheduling/services/section_budget_planning.py` allocates
  teacher-independent physical budgets.
- `backend/apps/scheduling/services/staffing_planning.py` proves the selected
  physical counts can be covered by the ready roster.

Tests:

- `backend/tests/test_upstream_planning_workflow.py`
- `backend/tests/test_section_count_recommendations_api.py`
- `scheduling_engine/tests/test_section_budget_planner.py`
- `scheduling_engine/tests/test_staffing_planner.py`

### Section-count planning and reconciliation

Section-count planning is implemented in `backend/apps/scheduling/services/section_planning.py`.

It creates immutable planning runs, stores the exact DTO snapshot used by the
engine, and only approves an explicitly reviewed subset. Approval creates
draft `Section` rows inside one transaction.

Reconciliation is separate and intentionally stronger:

- `backend/apps/scheduling/services/section_reconciliation.py`
- `docs/decisions/section-lifecycle.md`

It allows a newer approved run to reshape existing draft sections while
preserving identity where possible and retiring surplus rows instead of deleting
them.

Important current behavior:

- runs are immutable;
- approvals are immutable;
- reconciliation is append-only;
- active sections remain stable unless a specific reconciliation workflow is
  used;
- retired sections stay in history and are excluded from active solver input.

Tests:

- `backend/tests/test_section_planning_api.py`
- `backend/tests/test_section_reconciliation_api.py`

### Semester and A-D placement

Placement is already implemented as a separate stage.

Relevant files:

- `backend/apps/constraints/conflict_matrix.py`
- `backend/apps/scheduling/services/annual_placement_locks.py`
- `backend/apps/scheduling/services/section_placement.py`
- `scheduling_engine/section_placement.py`
- `docs/decisions/semester-placement-and-staffing-feasibility.md`

Current behavior:

- counselors manage a yearly course-conflict matrix;
- annual placement locks can reserve stable virtual section slots before real
  `Section` rows exist;
- placement works in two modes:
  - `fixed_semester` for already-existing active draft sections;
  - `annual_total` for approved annual totals;
- the engine chooses recurring A-D blocks and proves a hidden staffing witness;
- rooms are deliberately excluded;
- named teacher identities are deliberately excluded from the placement result;
- approval writes timeslot-only `SectionSchedule` rows and materializes annual
  slots when needed.

Tests:

- `backend/tests/test_conflict_matrix_api.py`
- `backend/tests/test_section_placement_service.py`
- `scheduling_engine/tests/test_section_placement.py`

### Teacher readiness and named teacher assignment

Teacher readiness is controlled by a roster plus explicit semester and annual
capacity rows.

Relevant files:

- `backend/apps/scheduling/services/staffing_configuration.py`
- `backend/apps/scheduling/services/teacher_assignment_configuration.py`
- `backend/apps/scheduling/models.py`
- `backend/apps/scheduling/services/teacher_assignment.py`
- `scheduling_engine/teacher_assignment.py`
- `docs/decisions/named-teacher-assignment.md`

Current behavior:

- ready rosters require membership, Semester 1 capacity, Semester 2 capacity,
  and annual capacity for every included teacher;
- annual capacities, course-specific assignment rules, and time preferences are
  planning configuration;
- named assignment is a later reviewed stage that consumes accepted timing;
- the solver respects qualifications, timeslot availability, annual/semester
  workload, exact locked teacher requirements, and course-specific hard bounds;
- approval writes `Section.teacher` plus immutable provenance lines, and it does
  not create rooms or enroll students.

Tests:

- `backend/tests/test_teacher_assignment_service.py`
- `backend/tests/test_upstream_planning_workflow.py`
- `scheduling_engine/tests/test_teacher_assignment.py`

### Engine independence and solver modules

The pure engine is its own package.

Key files:

- `scheduling_engine/dto.py`
- `scheduling_engine/constraint_compiler.py`
- `scheduling_engine/demand_analyzer.py`
- `scheduling_engine/planning_core.py`
- `scheduling_engine/section_budget_planner.py`
- `scheduling_engine/staffing_planner.py`
- `scheduling_engine/section_placement.py`
- `scheduling_engine/teacher_assignment.py`
- `scheduling_engine/tests/test_independence.py`

What this means in practice:

- the engine only receives immutable DTOs;
- it does not read the ORM or Django settings;
- it returns structured recommendation data and diagnostics only;
- `backend/apps/scheduling/services/engine_adapter.py` is the boundary that
  loads ORM state into those DTOs;
- tests enforce the import boundary.

## 7. Documents Present & Why They Matter

| Document | Why it matters |
| --- | --- |
| `README.md` | The current high-level setup and API walkthrough for the repo |
| `scheduling_engine/README.md` | The engine boundary summary and isolated test command |
| `docs/API_Contract_Strategy.md` | The current rule for serializers, policies, and stable diagnostic codes |
| `docs/Architecture_Development_Rules.md` | The current layer ownership and import-boundary rules |
| `docs/Implementation_Roadmap.md` | The re-baselined phase ordering and what is completed, partial, or deferred |
| `docs/Software_Design_Document.md` | The older long-form architecture reference; still useful, but some stage details are stale |
| `docs/decisions/section-lifecycle.md` | The authoritative section identity, retirement, and reconciliation contract |
| `docs/decisions/semester-placement-and-staffing-feasibility.md` | The accepted placement-stage contract that excludes rooms and named teachers |
| `docs/decisions/named-teacher-assignment.md` | The accepted named-teacher stage contract after placement |

If you only read three documents before making changes, read:

1. `docs/Architecture_Development_Rules.md`
2. `docs/decisions/section-lifecycle.md`
3. `docs/Implementation_Roadmap.md`

## 8. Current System From a Counselor's Perspective

Here is what a counselor can do today, in the order that matches the backend
workflow.

1. Review raw demand.

   - Endpoint: `GET /api/demand/summary/?academic_year=<id>`
   - Source: `backend/apps/courses/services/demand.py`
   - Result: per-course primary, alternate, and total request counts.

2. Adjust the course offering for a year.

   - Cancel or restore an offering with `POST /api/planning/course-offerings/`
   - Combine offerings with `POST /api/planning/combine-offerings/`
   - Review safe suggestions with `GET /api/planning/combination-suggestions/`
   - Manage combination rules with `POST/GET/PATCH/DELETE /api/planning/combination-rules/`

   This is where a counselor decides whether low-demand courses should stay
   separate, be cancelled, or be combined into one physical delivery.

3. Run section-count planning.

   - Endpoint: `POST /api/planning/section-count-runs/`
   - Review: `GET /{id}/review/`
   - Approval preview: `POST /{id}/approval-preview/`
   - Approve: `POST /{id}/approve/`
   - Later correction: `POST /{id}/reconciliation-preview/` then `POST /{id}/reconcile/`

   This stage recommends annual and semester counts, then creates draft
   `Section` rows only after approval.

4. Configure staffing readiness.

   - Teacher directory: `GET/POST /api/teachers/`
   - Teacher archive/restore: `POST /api/teachers/{id}/archive/` and
     `POST /api/teachers/{id}/restore/`
   - Teacher qualifications, preferences, current courses, and availability:
     nested teacher routes under `backend/apps/constraints/urls.py`
   - Teacher planning capacities:
     `GET/POST /api/planning/teacher-capacities/`
   - Teacher annual capacities:
     `GET/POST /api/planning/teacher-annual-capacities/`
   - Teacher course assignment rules:
     `GET/POST /api/planning/teacher-course-assignment-rules/`
   - Teacher time preferences:
     `GET/POST /api/planning/teacher-time-preferences/`
   - Teacher roster:
     `GET/POST /api/planning/teacher-rosters/`
     then set members and confirm ready

   This is where the counselor records which teachers are in scope and how
   much they can carry.

5. Run section-budget and staffing-feasible physical planning.

   - Section budget runs:
     `POST /api/planning/section-budget-runs/`
   - Staffing runs:
     `POST /api/planning/staffing-runs/`

   These stages decide how many physical sections the school can support, first
   without teachers, then against the ready roster.

6. Build placement timing.

   - Conflict matrix:
     `POST /api/planning/course-conflict-matrices/`
     plus `GET /{id}/grid/`, `POST /{id}/refresh/`,
     `POST /{id}/conflicts/{conflict_id}/adjust/`
   - Annual placement locks:
     `GET/POST /api/planning/annual-placement-locks/`
   - Placement runs:
     `POST /api/planning/section-placement-runs/`
   - Review/preview/approve:
     `GET /{id}/review/`, `POST /{id}/approval-preview/`, `POST /{id}/approve/`

   This is where the system chooses Semester 1 or 2 and recurring A-D blocks
   for normal instructional sections and approved online-supervision resources,
   and proves that staffing is feasible later.

7. Run named teacher assignment.

   - Annual teacher capacities and course rules are already configured above.
   - Teacher assignment runs:
     `POST /api/planning/teacher-assignment-runs/`
   - Review/preview/approve:
     `GET /{id}/review/`, `POST /{id}/approval-preview/`, `POST /{id}/approve/`

   This is the stage that writes named teachers onto accepted sections and
   workload-safe supervisors onto accepted online-supervision sessions.

8. Use section locks for exact counselor decisions.

   - Endpoint: `GET/PATCH /api/sections/{section_id}/lock/`
   - The lock can cover teacher, timeslot, and room dimensions separately.

Where the workflow stops today:

- the first student-assignment solver/review/approval stage and the controlled
  rerun increment are implemented;
- active/historical enrollment state, six audited student-assignment lock types,
  full and scoped reruns, priority requests, schedule preservation, review and
  read-only what-if behavior, drift checks, and transactional replacement
  provenance are implemented;
- Study and Focus requests are explicit student-time commitments, never
  inferred empty blocks. Co-op is one two-credit A+B/C+D external commitment;
  online courses use a separately planned shared supervision seat; and the
  two configured half-semester courses are sequentially paired where possible.
  Exact/exclusion locks and stable review codes cover each special choice;
- online supervision capacity has its own immutable plan/review/approval,
  then joins the existing placement and named-teacher stages without becoming a
  fake normal instructional section; placement includes a primary online
  co-request feasibility witness so multiple online courses for one student
  receive distinct supervision blocks when a valid placement exists;
- section cancellation with active enrollments fails closed with the affected
  student IDs until a reviewed rerun resolves those enrollments; historical
  enrollments then remain audit evidence while reconciliation may retire the
  section;
- no room assignment solver yet;
- no composed timetable, personal schedule endpoint, conflict analyzer, or
  general enrollment override workflow yet;
- no frontend application yet;
- the approximately 1,400-student / 300-section benchmark has 9,800 required
  requests and 10,500 usable seats, with no course-specific seat shortage. The
  original one-worker lexicographic pass timed out as `unknown` before finding
  a candidate. Step 9 added deterministic CP-SAT-validated initial guidance,
  empty-tier skipping, and valid-incumbent retention; the unchanged fixture now
  completes all 9,800 assignments with no unmet request in 135.126 seconds.
  Two additional one-worker/seed-0 runs produced identical assignment and
  section-load hashes in 136.781 and 134.538 seconds. This is
  representative-fixture evidence, not yet a production qualification for
  real-school data or deployment request limits.
- a separate realistic-condition validation fixture now covers uneven demand
  and capacities, approved backups, missing offerings, prerequisites, A-D
  collision safety, historical/protected enrollments, locks, preservation, and
  scoped reruns. Its uneven 1,400-student/300-section fixture completed all
  9,800 requests in 72.132 and 76.375 seconds with identical assignments. The
  current data model still lacks student-specific course eligibility and an
  optional-request category; transcript completion remains deliberately
  assumed.

## 9. Known Divergences Between the SDD/Roadmap and Actual Code

| Source | What it says | What the code actually does today | Governing truth |
| --- | --- | --- | --- |
| `docs/Software_Design_Document.md` | Several sections still imply a queue-first or older room-coupled workflow | Current solver stages are synchronous and review-first; room assignment is explicitly deferred out of semester/A-D placement and named-teacher assignment | Code plus the accepted decision docs |
| `docs/Implementation_Roadmap.md` | Older wording still refers to Phase 2 as "A-D block and room placement" | The accepted decision is semester/A-D placement with staffing feasibility, and rooms are not part of that stage | `docs/decisions/semester-placement-and-staffing-feasibility.md` |
| `README.md` | Still describes `backend/apps/common/constants.py` as the single source of truth for reusable values | `backend/apps/common/constants.py` is now a compatibility export; owning modules are `school_values`, `people.constants`, `courses.constants`, `constraints.constants`, and `scheduling.constants` | Current code and `docs/Architecture_Development_Rules.md` |
| `README.md` and roadmap text | Older descriptions mention section-count and staffing workflows but not the newer placement and named-teacher stages | Those newer stages are implemented and documented in the decision records | Current code and decision docs |
| `backend/tests/test_upstream_planning_workflow.py::test_combined_delivery_moves_from_ready_roster_to_one_physical_section` | Assumes a ready roster can confirm with only semester capacity rows | Current roster confirmation also requires an annual capacity row for each roster teacher | Current code |

The practical rule is simple: when docs and code disagree, the code and the
accepted decision records govern.

## 10. Invariants and Rules Future Work Must Not Break

- `scheduling_engine` must stay Django-free.
- The adapter in `backend/apps/scheduling/services/engine_adapter.py` remains
  the ORM-to-DTO boundary.
- Every API endpoint must declare a resource policy or named action policy.
- Policy scoping must happen before client query filtering.
- Planning runs, approvals, reconciliations, and audit rows are append-only.
- A new recommendation does not overwrite an accepted one implicitly.
- Retired sections are read-only and excluded from active solver input.
- `Section.is_locked`, `SectionLock`, and `SectionSchedule` are all fixed
  context signals; future code must use the shared section-state helper.
- Annual placement locks are pre-section timing decisions keyed by year,
  delivery group, and stable annual ordinal.
- A staffing witness is not a named teacher assignment.
- Named teacher assignment must not change accepted semester/A-D timing.
- Rooms are excluded from semester/A-D placement and from named-teacher
  assignment.
- Availability is available-by-default; only explicit false availability rows
  remove a slot.
- Grade 11-12 qualification behavior must fail closed when required normalized
  eligibility is missing.
- Project schema development stays migrationless unless the project owner says
  otherwise; use `migrate --run-syncdb` for development rebuilds.
- Stable diagnostic codes from `scheduling_engine/diagnostics.py` are the
  client-facing contract; prose can change, codes should not.

## 11. Verification Baseline

Commands run in this checkout:

```powershell
.\.venv\Scripts\python.exe backend\manage.py check
.\.venv\Scripts\python.exe -m pytest -q scheduling_engine/tests
.\.venv\Scripts\python.exe -m pytest --collect-only -q scheduling_engine/tests
.\.venv\Scripts\python.exe -m pytest -q backend/tests
.\.venv\Scripts\python.exe -m pytest --collect-only -q backend/tests
```

Current results:

| Command | Result |
| --- | --- |
| `backend\manage.py check` | `System check identified no issues (0 silenced).` |
| `pytest -q scheduling_engine/tests` | `44 passed in 2.23s` |
| `pytest --collect-only -q scheduling_engine/tests` | `44 tests collected` |
| `pytest -q backend/tests` | `123 passed, 1 failed in 5m42s` |
| `pytest --collect-only -q backend/tests` | `124 tests collected in 1.01s` |

The one failing backend test is
`backend/tests/test_upstream_planning_workflow.py::test_combined_delivery_moves_from_ready_roster_to_one_physical_section`.
It still expects roster confirmation without the newly required annual capacity
row. The current implementation requires annual capacity for each ready-roster
teacher.

## 12. Recommended Next Phase

The next student-scheduling increment should be a read-only conflict analyzer,
not another mutation workflow. It should explain incomplete student schedules,
unmet requests, capacity and timing issues from accepted state without moving
sections, teachers, or enrollments.

Before transcript/SIS completion evidence is introduced, the implemented
student stage deliberately assumes prior prerequisite completion and enforces
only same-year ordering when both courses are assigned. Controlled locks and
partial reruns are implemented as a separate follow-on decision; general
overrides remain a later separate capability rather than being folded into the
first assignment workflow.

## 13. Open Questions / Explicitly Deferred Decisions

- What is the authoritative data source for prerequisite completion evidence
  when the temporary assumed-prior-completion policy is replaced?
- Should the project publish OpenAPI now that the student-assignment API
  surface exists, or wait until the next scheduling increment?
- Should downstream solver runs get background orchestration only after a
  measured solve-time benchmark justifies it?
- When should room assignment become its own reviewed stage relative to student
  assignment?
- Should the current manual override audit model evolve into a first-class
  override workflow before the frontend starts?
- Should cross-listing remain a later deliberate design decision, as the
  roadmap and decision docs currently imply?

These are genuine product decisions, not missing code details. Future work
should not assume answers that are not yet encoded in the repository.
