# Implementation Roadmap
## Intelligent School Timetabling System — Version 1 Re-baseline

**Document type:** Master implementation roadmap

**Re-baselined:** 2026-08-08

**Primary evidence:** current repository code, tests, README, and Software Design Document (SDD)

**Project context:** solo-developer, long-duration portfolio project

---

## How to Use This Roadmap

This document starts from the software that exists today. It does not treat the project as a greenfield implementation and does not preserve obsolete phases simply because they appeared in an earlier roadmap.

Status labels have precise meanings:

- **Completed** means the capability exists in executable code and has relevant automated coverage.
- **Partially complete** means useful models, services, policies, or engine inputs exist, but the user-facing capability is not complete.
- **Not implemented** means the repository contains no working end-to-end version of the capability. A model or permission placeholder alone does not count as implementation.

The SDD remains the architectural reference for Version 1 intent. The repository is the source of truth for implementation status. Where the two differ, this roadmap names the difference rather than pretending they already match.

The intended product remains a counselor-controlled decision-support system:

`Demand → Section Planning → Counselor Review → Approval → Draft Sections → Further Scheduling → Analysis → Human Adjustment → Replanning`

Every future solver phase must preserve a review checkpoint. Solver output is a recommendation until an authorized person explicitly accepts it.

---

## Current Project Status

### Current Product Boundary

The working system presently reaches this point:

`Course requests → offering cancellation/combination → backup policy → teacher-independent section budget → confirmed teacher roster → staffing-aware physical counts → counselor approval → active sections → semester/A–D placement → optional named-teacher context → student-assignment review → enrollment approval`

The system now supports first-release student-to-section assignment after
accepted semester/A–D placement and a controlled rerun increment with active
enrollment history, six audited lock types, full/scoped runs, review-only
what-if checks, and transactional approval. Room assignment, conflict
analysis, composed timetables, general manual overrides, and frontend work
remain outside the implemented boundary.

### Completed

The following capabilities are implemented in the repository and covered by automated tests.

#### Backend and domain foundation

- Django, Django REST Framework, PostgreSQL configuration, SimpleJWT authentication, pagination, and app-level separation are in place.
- Core models exist for academic years, rooms, courses, sections, students, teachers, counselors, course requests, enrollments, prerequisites, historical demand, constraints, locks, schedules, and translations.
- Shared selectable domain values live in `backend/apps/common/constants.py`, including the permanent four-day A–D rotation.
- Project application migrations are intentionally disabled. Tests and local development synchronize tables directly from current models.
- Django admin registration exists for the principal domain models.

#### Authentication, roles, and access policy

- Student, teacher, counselor, staff, director, and unknown roles are resolved centrally.
- Domain-profile roles take precedence over general role profiles.
- Resource and named-action policies fail closed and separate read scope from write scope.
- Students can manage only their own course requests; teachers can manage only their own nested qualification/preference/availability records; planning roles receive the broader planning access defined by policy.
- Anonymous requests return `401`; authenticated but unauthorized or unknown roles return `403` at tested endpoints.
- Policy filtering is applied before client query filtering.

#### Domain and constraint APIs

- Course, section, and course-request CRUD APIs exist with role-safe filtering and validation.
- Academic-year, room, and A–D timeslot APIs exist with guarded deletion of referenced data.
- Raw course-demand aggregation exists for an academic year.
- Shared hard/soft constraints, counselor weights, course conflicts, room requirements, qualifications, course qualification requirements, and section locks are manageable through APIs.
- Teacher qualifications, preferences, current courses, and availability use teacher-owned nested APIs.
- Direct teacher assignment and teacher locks share the same qualification validation service.

#### Normalized qualification architecture

- The implemented source of truth is `Teacher → TeacherQualification → Qualification` and `Course → CourseQualificationRequirement → Qualification`.
- Qualification kind, canonical subject, division, enforcement, and provenance are represented explicitly.
- Grade 11–12 courses fail closed unless a required normalized Senior teachable is configured and held by the teacher.
- Grade 7–10 mappings are preferences and do not create a hard eligibility barrier.
- Aspen source text and provenance remain outside engine matching.
- The constraint compiler is the pure-engine authority for eligibility; future solvers must consume its compiled sets rather than duplicate matching logic.

#### Pure scheduling-engine boundary

- `scheduling_engine` is independent of Django and accepts immutable DTOs.
- An AST-based test rejects Django or backend imports in production engine modules.
- `backend/apps/scheduling/services/engine_adapter.py` is the Django-to-engine translation boundary.
- The adapter loads a detached academic-year snapshot, normalized qualifications, constraints, existing sections and locks, rooms, timeslots, requests, historical demand, and teacher capacity.
- The engine includes demand analysis, recency-weighted historical conversion, co-request conflict recommendations, the legacy section estimator, and compiled solver indexes.

#### Staffing-aware section planning

- `CapacityProfile` provides ordered hard minimum, soft minimum, target, soft maximum, and hard maximum values.
- Shared capacity profiles and copy-on-write course-specific profiles are supported.
- `CoursePriorityProfile` provides an explicit four-tier priority; priority is not inferred from request flags, course code, grade, or category.
- Courses can be restricted to Semester 1, Semester 2, or either semester.
- `TeacherPlanningCapacity` represents a teacher/year/semester maximum and reserved load.
- Existing assigned or locked sections consume remaining planning capacity.
- What-if teacher adjustments reduce or exclude capacity without changing credentials.
- The OR-Tools CP-SAT section planner produces:
  1. a demand-only annual baseline;
  2. an annual staffing-feasible plan; and
  3. a Semester 1/Semester 2 split.
- The objective is solved lexicographically, protecting higher course-priority tiers before class-size preferences.
- Positive demand below the hard minimum can produce one review-required provisional section.
- Unstaffable demand remains visible as unmet demand; the planner does not invent sections to fill staff capacity.
- Counselor scenarios support exact annual counts, annual minimums, annual maximums, and teacher-capacity reductions/exclusions.
- Structured diagnostics cover invalid course scenarios, missing eligible teachers, aggregate staffing shortages, course-specific shortages, semester shortages, and shared-pool infeasibility.

#### Immutable planning and approval workflow

- `SectionPlanningRun` stores the scenario, exact engine input snapshot, result, creator, status, timestamp, and solver metadata as an immutable audit record.
- Planning roles can list and retrieve runs, review remaining recommendations, preview an adjusted approval, and approve all or selected courses.
- Approval may change the recommended Semester 1/Semester 2 counts and records both recommended and accepted values.
- `SectionPlanningApproval` and `SectionPlanningApprovalCourse` are immutable.
- Approval creates deterministic, unstaffed, unlocked draft `Section` rows in one transaction.
- Every generated section traces to its per-course approval, approval user, and original planning run.
- Current semester restrictions are revalidated at approval time, and configuration drift is shown to the reviewer.
- Existing sections or repeated approval return `409 Conflict`; the service never replaces existing work implicitly.
- A simulated mid-write failure is tested to roll back sections and all approval audit rows.

#### Course-offering, backup, and section-budget planning

- Every catalog course can have an explicit year-specific `CourseOffering` state. A positive-demand course reaches zero sections only through a planning-role cancellation with a required reason; requests remain intact.
- Approved `CourseCombinationRule` records allow compatible offerings to share one `DeliveryGroup`. Combining and separating are audited and blocked after active physical sections exist.
- Combined offerings keep distinct course codes while consuming exactly one physical section. Their semester availability is the member intersection, capacity comes from the approved combination rule, qualification requirements are combined, and room requirements are unioned.
- Safe combination suggestions are advisory and use forecast primary demand. They never merge courses automatically.
- A student may have at most one alternate per academic year. Alternates are excluded from ordinary forecasts and conflict weights unless an explicit run policy promotes one after a cancellation.
- Budget runs support an exact physical-section total or a ceiling without consulting teachers. Positive active demand cannot silently receive zero sections; combined positive demand receives exactly one.
- Backup promotion uses a primary-only preliminary plan (or a linked approved budget), consumes one alternate at most once, records unresolved gaps, and never rewrites `CourseRequest` rows.
- `SectionBudgetRun`, its approval, normalized per-offering decisions, and per-student request resolutions are immutable. Budget approval deliberately creates no operational sections.

#### Teacher readiness and physical staffing handoff

- Planning roles can manage the teacher directory. Teachers are archived/restored through audited status decisions rather than deleted.
- A `TeacherPlanningRoster` explicitly identifies the teachers used for one academic year. Each included teacher requires both Semester 1 and Semester 2 capacity rows, including explicit zero-capacity rows, before the roster can be confirmed ready.
- Capacity, membership, teacher, or qualification-evidence changes invalidate a ready roster and return it to draft.
- Teacher qualification submissions begin pending. Planning roles verify or reject them, and only verified credentials enter assignment validation or solver eligibility.
- Staffing runs may operate directly or refine a teacher-independent budget approval. A linked run preserves the approved physical total and explains any allocation differences by delivery group.
- The physical staffing solver uses anonymous qualified load only: it proves that a plan can be staffed without making named teacher assignments.
- `StaffingPlanRun` and normalized backup resolutions are immutable. Final approval revalidates counselor adjustments and atomically creates canonical delivery-group `Section` rows with audit provenance. Combined deliveries create one physical row with no misleading single-course identity.

### Partially Complete

These areas contain useful foundations but are not complete Version 1 capabilities.

| Area | What exists | What is still missing |
|---|---|---|
| Demand and offering review | Raw demand, forecasting, explicit cancellation/restoration, audited combination/separation, compatibility rules, and advisory merge suggestions | A frontend review workspace and an API workflow for accepting generated conflict weights |
| Planning input management | Student/teacher/course models, year-specific teacher rosters, readiness confirmation, two-semester capacity enforcement, and teacher/qualification management APIs | Historical-demand management API and one consolidated cross-stage readiness dashboard |
| Staffing reporting | Per-run available/planned/unused capacity, course eligibility counts, and structured shortage diagnostics | The SDD's standalone staffing summary by subject/qualification pool and a stable API intended for cross-run operational reporting |
| Section lifecycle | Draft sections, physical delivery identity, planning provenance, safe refusal to overwrite, and audited reconciliation with retirement/reactivation | A physical-delivery reconciliation path for changing an already-materialized combined group; late combination remains intentionally blocked |
| Timetable data | `TimeSlot`, permanent A–D rotation, `Room`, `SectionSchedule`, room requirements, course conflicts, and section locks | No solver or review workflow assigns a block or room |
| Teacher scheduling foundation | Normalized qualifications, compiled eligibility, availability, preferences, current-course history, workload fields, and planning capacity | No named-teacher assignment solver, recommendation run, approval, or assignment diagnostics |
| Student assignment and controlled reruns | Immutable first-release and replacement runs/approvals, active/historical enrollments, six audited locks, full/scoped reruns, priorities, schedule preservation, what-if checks, and cancellation/reconciliation bridge | No transcript/SIS completion evidence, general manual overrides, personal timetables, or conflict analyzer; the target-scale benchmark is not yet ready |
| Manual controls | `SectionLock`, `Section.is_locked`, `ManualOverride` model, admin registration, and future action-policy names | No enforced synchronization invariant between lock row/flag; no override application service/API, typed action workflow, optimistic concurrency, override history endpoint, or scoped re-solve |
| Timetable visibility | Teachers can read sections already assigned to them | No composed timetable endpoint and no student/teacher personal schedule endpoint |
| Internationalization | `Translation` model and admin registration | No translation API and no user interface consuming translations |
| Scheduling orchestration | Section planning runs synchronously and persist immutable results | No run orchestration/status model for placement/assignment stages and no measured decision yet on background workers |
| Frontend | None | The React role-based application described by the SDD has not been started |
| Delivery hardening | Pytest configuration, extensive tests, README setup, environment-based secrets, and a manual target-scale benchmark script | No visible CI workflow, production settings split, generated API contract, structured operational logging, or acceptable target-scale solve-quality evidence |

### Not Yet Implemented

- Automated section placement into A–D blocks.
- Automated room assignment.
- Named teacher-to-section assignment.
- Post-solve conflict and issue analysis across the completed timetable.
- General manual override application and immutable history APIs.
- Persistent status tracking for downstream scheduling runs.
- Historical-demand management API coverage required by later workflows.
- A counselor/administrator, teacher, or student frontend.
- Final timetable and personal-schedule APIs.

Models, DTO fields, policy names, or SDD descriptions for these areas are foundations only; they are not evidence that the capability is complete.

### Verification Baseline

The current checkout was verified non-destructively during this re-baseline on 2026-08-08:

- `python backend/manage.py check` — no issues.
- Isolated `scheduling_engine` suite — 26 tests passed without Django.
- Django backend suite — 100 tests passed.
- Combined verified total — 126 tests passed.

These numbers describe the current baseline, not a permanent completion target. Every future phase must add tests for its own contracts while keeping this existing suite green.

---

## Architecture to Preserve

All remaining phases must preserve these established boundaries and conventions:

1. **Django owns identity, authorization, validation, orchestration, and persistence.** DRF views remain thin and delegate multi-model work to services.
2. **The scheduling engine remains pure Python.** No engine module imports Django, performs ORM queries, or writes database state.
3. **The adapter is the translation boundary.** ORM records become immutable DTOs before solving; result persistence occurs in an explicit Django transaction.
4. **Optimization remains staged.** Section counts, placement, teacher assignment, and student assignment are independently reviewable stages, not one monolithic solver.
5. **Human decisions are explicit and auditable.** A recommendation never becomes operational state through an implicit overwrite.
6. **Qualification rules are already designed.** Future work reuses normalized qualifications and compiled eligibility. Qualifications are not consumed and capacity scenarios never modify credentials.
7. **The timetable uses recurring A–D blocks.** A `TimeSlot` is one recurring block in one semester, not an individual calendar-day period.
8. **Authorization fails closed.** Planning roles are counselor, staff, and director where current policy allows; future stage-specific policy may intentionally distinguish who can run a solver from who can monitor it.
9. **Schema development is migrationless for this pre-production repository.** Do not create project migration files unless explicitly requested. An authorized local schema rebuild uses `migrate --run-syncdb`; never reset a database as an incidental roadmap task.
10. **Infrastructure follows measured need.** Do not add Docker, Celery, Redis, or another broker solely because the old roadmap named them. Preserve an orchestration seam and choose deployment infrastructure only after representative solve-time and reliability requirements justify it.

---

## Meaningful SDD Divergences

The SDD still describes the product philosophy and long-term pipeline well, but several concrete implementation assumptions are stale.

| Topic | SDD description | Actual implementation and roadmap treatment |
|---|---|---|
| Section-count planning | Primarily a heuristic using legacy min/max values | Evolved intentionally into a staffing-aware, semester-aware CP-SAT stage using five-point capacity profiles, explicit priorities, and teacher capacity. Treat the implemented planner as current architecture. |
| Counselor confirmation | Counselor manually creates sections through ordinary Section CRUD | Replaced by a stronger review/preview/approval transaction with immutable audit provenance. This is completed work. |
| Database schema | Described as fixed and largely unmodified | The implementation added planning configuration/run/approval models and explicit role profiles while preserving the SDD's intent. Future schema changes should be additive and justified, not prohibited by an outdated statement. |
| Roles | Administrator inferred mainly through Django staff/superuser flags | The repository has explicit staff/director role profiles plus domain profile precedence. Use the implemented role system. |
| Computational execution | All solve stages are presented as asynchronous through a queue | Current section planning is synchronous and bounded enough for existing tests. Downstream stages need a measured orchestration decision; a queue is not yet implemented. |
| Course conflicts | Demand analysis automatically upserts `CourseConflict` | The pure engine computes recommendations, but the current API does not persist them automatically. Preserve human review rather than silently claiming upsert behavior exists. |
| Merge/cancel review | Explicit Stage 2 merge/cancel workflow | Implemented as audited year-specific cancellations and approved course-combination rules over one physical `DeliveryGroup`. Late changes are blocked once active sections exist. |
| Lock state | `Section.is_locked` is described as a synchronized fast flag for `SectionLock` state | The current lock API can create or clear a `SectionLock` without synchronizing `Section.is_locked`, and generic Section CRUD can change the flag independently. Establish one invariant before downstream placement. |
| Manual overrides | Complete override service and scoped feedback loop | Only locks, a basic `ManualOverride` model, and policy scaffolding exist. The general workflow remains future work. |
| Frontend and i18n | React application and translation-backed UI are part of Version 1 | Neither is implemented. They remain required for a complete user-facing Version 1, but should be built against stable stage APIs. |
| Migrations | Conventional Django migrations assumed | Project apps intentionally disable migrations during pre-production development. Roadmap work must follow the documented `run-syncdb` convention. |

Updating the SDD to reflect these implementation evolutions is a documentation task in the final hardening phase. The code should not be redesigned merely to reproduce stale SDD assumptions.

---

## Version 1 Completion Target

Version 1 is coherent when a counselor can:

1. collect and review demand;
2. run, adjust, approve, and later reconcile a section-count plan;
3. review and approve A–D block and room placement;
4. review and approve named teacher assignments;
5. review and approve student assignments;
6. inspect unresolved conflicts and capacity issues;
7. make an audited manual change and re-solve only the affected scope;
8. complete the workflow through a role-appropriate user interface; and
9. understand which run, assumptions, human decision, and override produced every current scheduling result.

The target is not a schedule that bypasses human judgment. It is a repeatable and auditable workflow that produces a strong draft, explains problems, and preserves accepted work during iteration.

---

## Remaining Phase Order

`Section placement → Teacher assignment → Student assignment and conflict analysis → Manual overrides and scoped re-solve → Frontend → V1 hardening`

This order follows data dependencies. Stable section identity comes before attaching schedules, teachers, and students. Placement comes before availability-aware teacher assignment and conflict-free student assignment. Scoped re-solving comes after there are real solver stages to scope.

---

## Phase 1 — Section Planning Lifecycle Completion and Reconciliation

**Current Status:** **Core reconciliation complete.** Planning roles can preview and atomically apply a newer immutable run to existing sections. Active/retired lifecycle state, stable identity, protected downstream state, stale-preview detection, and immutable per-section audit actions are implemented. Historical-demand/readiness APIs, broader lock cleanup, and the planning-status view remain deferred by scope.

**Goal:** Allow a planning-role user to apply a later approved section plan safely and explicitly, while preserving immutable approvals, stable section identity, downstream work, and a complete audit trail.

**Why It Comes Next:** The current planner and approval workflow are usable for the first plan only. Real scheduling is iterative, so changed demand, staffing, or counselor decisions must eventually alter approved counts. If block, room, teacher, or student assignments are built first, the undefined meaning of “replace these sections” becomes more dangerous and expensive. Establishing lifecycle and reconciliation semantics now gives every downstream stage a stable set of active sections.

**Dependencies:** Existing `SectionPlanningRun`, approval models, generated-section provenance, `Section`, locks, schedules, teacher assignments, and enrollments.

**Deliverables:**

- A documented lifecycle for planned sections, including active draft and retired/superseded states or an equivalent non-destructive representation.
- A read-only reconciliation preview that compares a completed planning run or approval with the current active sections for each course/year.
- An explicit reconciliation apply action requiring an authorized user and reason.
- A deterministic delta containing unchanged, created, semester-moved, and retired/blocked sections.
- Immutable reconciliation audit records linking the prior state, new planning run/approval, actor, reason, and resulting section IDs.
- Approval-level rationale plus course-level recommended/approved counts and per-section consequences. A separate per-course free-text rationale remains deferred.
- An explicit counselor outcome for offering, offering below minimum, or accepting zero sections; this is a human decision and does not automatically merge courses.
- Planning-role historical-demand management and a read-only input-readiness report remain a later, separate API increment.
- Reconciliation conservatively treats either `Section.is_locked` or a `SectionLock` row as fixed; consolidating those two representations remains later cleanup.
- A consolidated planning-status/staffing view remains deferred; reconciliation preview currently exposes active counts and the concrete delta.
- Transactional and concurrency-safe reconciliation with structured `400` validation and `409` state conflicts.
- API/service/model tests for increases, decreases, semester moves, unchanged plans, retries, rollback, and protected downstream state.

**Major Tasks:**

1. Write a short architecture decision record defining section identity and legal lifecycle transitions.
2. Define how manually created sections differ from approval-generated sections during reconciliation.
3. Implement a pure comparison service that produces the proposed delta before any write.
4. Preserve existing section rows when they remain valid; create only the additional rows required by an increase.
5. Never cascade-delete accepted work. A section with a lock, placement, teacher, enrollment, or manual override must be reported as blocked or handled through a separately confirmed transition.
6. Apply accepted deltas in one transaction with row locks and deterministic ordering.
7. Keep planning runs and prior approvals immutable; reconciliation adds history rather than rewriting it.
8. Add review/apply actions and return enough state for a future counselor UI to explain every consequence.
9. Later increment: add the historical-demand/readiness API boundary needed to explain forecast quality and incomplete planning configuration.
10. Later cleanup: define whether the structured `SectionLock` row or `Section.is_locked` is authoritative; reconciliation currently protects either representation.
11. Add tests proving a failed reconciliation leaves sections and audit records unchanged.
12. Treat cross-listing as a separate domain decision. Do not use it as an automatic answer to low-demand courses in this phase.

**Suggested Folder/Module Structure:**

```text
backend/apps/scheduling/
  models.py                         # additive reconciliation/lifecycle audit models
  serializers.py                   # preview/apply request and audit representations
  views.py                         # thin run reconciliation actions
  services/
    section_reconciliation.py      # compare, validate, and transactionally apply deltas
    planning_readiness.py           # read-only configuration/input diagnostics
backend/apps/common/
  serializers.py                   # historical-demand representation
  views.py                         # planning-role historical-demand CRUD
  urls.py
backend/tests/
  test_section_reconciliation_api.py
  test_planning_readiness_api.py
docs/decisions/
  section-lifecycle.md
```

If lifecycle state belongs on `Section`, add it deliberately to `backend/apps/courses/models.py`. Follow the project's migrationless schema convention; do not create migration files automatically.

**Estimated Difficulty:** 7/10

**Common Mistakes to Avoid:**

- Deleting sections and relying on cascades, which can erase schedules, enrollments, locks, overrides, and provenance.
- Recreating every section and changing stable IDs when only one count changed.
- Editing an old planning run or approval instead of recording a new reconciliation fact.
- Treating all existing sections as if they were generated by the same approval.
- Moving an already placed or staffed section between semesters silently.
- Automatically cross-listing or merging low-demand courses without an explicit domain design and counselor decision.

**Definition of Done:**

- Starting from an approved plan, a counselor can create a later run, preview the exact section delta, and explicitly apply it.
- Unchanged sections retain their IDs and provenance.
- New sections are traceable to the new decision; superseded sections remain historically explainable.
- Sections with downstream dependencies are never silently deleted or moved.
- Duplicate or stale requests return a structured conflict and do not partially write.
- Both existing lock representations are safely treated as fixed context; consolidation and planning-readiness reporting are explicitly deferred.
- Transaction rollback and concurrency behavior are covered by automated tests.
- No placement, teacher, or student solver is introduced in this phase.

---

## Phase 2 — Counselor-Reviewed A–D Block and Room Placement

**Current Status:** **Partially complete foundation.** Section, room, timeslot, room-requirement, conflict, lock, DTO, compiler, and `SectionSchedule` structures exist. No placement solver or placement-run API exists.

**Goal:** Produce an inspectable candidate assignment of every active draft section to one semester-consistent A–D block and compatible room, then let a counselor approve that candidate into `SectionSchedule` records.

**Why It Comes Next:** Once the active section set has explicit lifecycle rules, block and room placement is the next dependency for both teacher availability checks and student conflict checks. It advances the product from “how many sections?” to “when and where can they run?” without yet conflating teacher or student assignment.

**Dependencies:** Phase 1; current A–D constants and `TimeSlot`; rooms and room requirements; active sections; section locks; co-request conflict evidence; pure constraint compiler.

**Deliverables:**

- A pure `section_placement` CP-SAT module with input/result DTOs and no Django imports.
- Immutable placement-run input snapshots, candidate results, solver status, objective data, and counselor-readable diagnostics.
- Hard enforcement of academic year, semester, available A–D blocks, room compatibility, room/slot exclusivity, and locked timeslot/room context.
- Soft optimization using counselor-reviewed course-conflict weights and distribution of multiple sections of a course across blocks.
- A review/preview/approval workflow that writes `SectionSchedule` atomically and never overwrites accepted placements implicitly.
- An explicit API workflow to review generated co-request conflict recommendations before they update `CourseConflict`; no silent automatic upsert.
- Bounded solver execution and representative benchmark results.
- A documented decision on synchronous versus background execution based on measured runtime and deployment reliability, not the old roadmap's assumptions.
- Pure-engine tests plus adapter, API, transaction, lock, and authorization tests.

**Major Tasks:**

1. Define placement DTOs and a stable result/diagnostic contract before writing CP-SAT variables.
2. Load only active sections as decision candidates while loading locked or out-of-scope placements as fixed context.
3. Model the four recurring blocks correctly; do not expand A–D into unrelated daily timeslots.
4. Generate feasible room/block candidates from semester, availability, room requirements, and the adopted room-capacity rule.
5. Enforce room/block uniqueness and all locked values as hard constraints.
6. Minimize weighted co-request collisions and avoid concentrating every section of the same course in one block.
7. Return actionable diagnostics for sections with no compatible room, unavailable semester blocks, contradictory locks, and aggregate room shortages.
8. Persist an immutable recommendation first; require explicit approval before changing `SectionSchedule`.
9. Add a time limit and report optimal, feasible/suboptimal, infeasible, and failed outcomes distinctly.
10. Benchmark a realistic fixture. Introduce a worker/broker only if the measured API execution contract requires one, behind the scheduling service boundary.

**Suggested Folder/Module Structure:**

```text
scheduling_engine/
  solvers/
    section_placement.py
  dto.py
backend/apps/scheduling/
  models.py                         # placement run/approval audit records
  serializers.py
  views.py
  services/
    placement_planning.py           # orchestration and transactional approval
    engine_adapter.py               # ORM ↔ placement DTO mapping
scheduling_engine/tests/
  test_section_placement.py
backend/tests/
  test_section_placement_api.py
```

Prefer a stage-specific run model unless concrete duplication justifies a carefully designed generic scheduling-run abstraction.

**Estimated Difficulty:** 9/10

**Common Mistakes to Avoid:**

- Assigning named teachers or students in the placement model.
- Querying Django models from the engine.
- Treating anonymous staffing witnesses from section planning as teacher assignments.
- Ignoring existing locks or loading locked sections as ordinary decision variables.
- Interpreting A–D blocks as one-off calendar periods.
- Writing placements directly from the solve without a review checkpoint.
- Adding Celery/Redis before solving time and reliability have been measured.

**Definition of Done:**

- A counselor can run, inspect, and approve a placement candidate for active draft sections.
- Approved sections have at most one current `SectionSchedule` and no room/block collision.
- Semester restrictions, room rules, available blocks, and locks are respected.
- Infeasible inputs produce structured, course/section-level diagnostics.
- A failed approval writes no partial placements.
- The pure solver passes without Django, and a representative benchmark records runtime and solve quality.

---

## Phase 3 — Counselor-Reviewed Named Teacher Assignment

**Current Status:** **Not implemented; data and policy foundations exist.** Qualifications, eligibility, availability, preferences, workload fields, planning capacities, and `Section`/`SectionSchedule`/lock model structures are available.

**Goal:** Recommend a named teacher for each eligible placed section while respecting legal qualifications, availability, timetable conflicts, workload limits, accepted locks, and explicit soft preferences.

**Why It Comes Next:** Teacher availability is meaningful only after sections have blocks. This stage converts the section planner's anonymous proof of staffing capacity into actual assignments without confusing the two concepts.

**Dependencies:** Phase 2 placement; normalized qualification compiler; teacher availability; teacher capacity/workload policy; preferences/current-course history; locked teachers.

**Deliverables:**

- A pure teacher-assignment CP-SAT solver consuming compiled eligibility.
- A documented operational source of truth for semester and annual workload limits, including how planning capacity, existing assignments, and locks interact.
- Immutable teacher-assignment runs with frozen inputs, recommendations, diagnostics, review, and explicit approval.
- Hard constraints for Grade 11–12 eligibility, availability at the placed block, no teacher overlap, semester/annual load, and locks.
- Explicitly weighted soft objectives for teacher preferences, current-course continuity, workload balance, and any approved seniority rule.
- Transactional writes to `Section.teacher` with overwrite/conflict protection.
- Qualification-gap, availability, overload, and shared-qualified-pool diagnostics.
- A planning-role teacher roster/workload API suitable for selecting, reviewing, and explaining assignment inputs without exposing unrelated sensitive data.
- Pure-engine, adapter, API, policy, audit, and rollback tests.

**Major Tasks:**

1. Reuse `qualified_teacher_ids_by_course`; do not create another qualification matcher.
2. Decide and document which existing workload fields are authoritative for assignment after planning.
3. Expose the teacher roster and workload inputs required by the counselor review surface.
4. Load accepted placements and locks as fixed context.
5. Create teacher/section assignment variables only for legally eligible and available pairs.
6. Enforce no overlapping section assignments and all workload ceilings.
7. Make every soft objective and its weight visible in the run result.
8. Separate solver recommendation from counselor approval and support partial review only if its consistency rules are explicit.
9. Revalidate current qualification, availability, and placement state inside the approval transaction.
10. Return structured explanations when a course has no eligible teacher or when qualified teachers collide in the same blocks.
11. Benchmark against a realistic teacher/section fixture.

**Suggested Folder/Module Structure:**

```text
scheduling_engine/solvers/
  teacher_assignment.py
backend/apps/scheduling/services/
  teacher_assignment.py
  engine_adapter.py
backend/apps/people/
  serializers.py
  views.py                          # planning-role teacher roster/workload API
  urls.py
scheduling_engine/tests/
  test_teacher_assignment.py
backend/tests/
  test_teacher_assignment_api.py
```

**Estimated Difficulty:** 9/10

**Common Mistakes to Avoid:**

- Parsing Aspen strings or redefining teachable subjects in the solver.
- Treating a qualification as consumed after one assignment.
- Treating Grade 9–10 preferences as Grade 11–12 legal requirements.
- Applying what-if capacity reductions by modifying qualifications.
- Persisting the anonymous section-planning load witness as a named assignment.
- Making preferences hard constraints or hiding their weights from counselors.
- Silently replacing a manually assigned or locked teacher.

**Definition of Done:**

- Every approved named assignment is legally eligible, available, non-overlapping, within workload policy, and consistent with locks.
- Counselor review clearly distinguishes hard feasibility from soft preference quality.
- Unassigned sections and the exact cause remain visible when no complete assignment exists.
- Approval is atomic and traceable to a run and user.
- Existing qualification and access-policy tests remain green with no duplicated matching logic.

---

## Phase 4 — Student Assignment and Conflict Analysis

**Current Status:** **Student assignment and controlled reruns implemented;
conflict analysis remains deferred.** The first counselor-reviewed
student-assignment release and the follow-on active-enrollment history, six
lock types, full/scoped reruns, priorities, schedule preservation, what-if
checks, drift checks, and cancellation bridge are implemented. Transcript/SIS
evidence, general manual overrides, composed timetables, and personal schedule
endpoints remain deferred.

**Implemented first release:**

- A Django-free `scheduling_engine/student_assignment.py` solver and detached
  DTO input snapshot.
- Immutable `StudentAssignmentRun`, `StudentAssignmentApproval`, and
  per-enrollment provenance rows; approval creates new `Enrollment` rows in
  one transaction and never replaces existing ones.
- Planning-only roster, hard-prerequisite, and soft-sequence configuration
  APIs, plus four counselor-visible staffing-assumption modes.
- Fixed active placed sections, capacity, shared combined-section capacity,
  existing enrollments, student A–D collisions, and same-year hard
  prerequisite sequencing.
- Mandatory and primary fulfillment before approved backups and selected soft
  objectives for sequencing, utilization, and semester-load balance.
- The temporary explicit policy that prior prerequisite completion is assumed;
  the system does not validate transcripts, grades, credits, CSV files, or SIS
  data in this release.

The accepted scope and deliberate exclusions are recorded in
`docs/decisions/student-assignment-first-release.md`.

**Next increment:** Add a read-only conflict analyzer and, separately, decide
whether to introduce transcript/SIS completion evidence before changing the
temporary prerequisite assumption. General manual overrides, room assignment,
and personal schedules remain separate reviewed capabilities.

---

## Phase 5 — Audited Manual Overrides and Genuine Scoped Re-solving

**Current Status:** **Partially complete foundation.** Student-assignment
locks, scoped student reruns, active/historical enrollment replacement, and
their action policies are implemented. `SectionLock`, a basic
`ManualOverride` model, and cross-stage policy foundations also exist, but no
general override service, cross-stage scope contract, or cross-stage solver
feedback loop exists.

**Goal:** Let counselors make explicit manual changes to placements, teachers, and student assignments, preserve those decisions as hard context, and re-run only the affected portion without changing accepted out-of-scope work.

**Why It Comes Next:** The student stage now has a deliberately narrower
scope/lock contract. The remaining phase is the broader, cross-stage manual
override workflow for placement, teachers, and student assignments, not a
replacement for the student rerun capability already implemented.

**Dependencies:** Phases 2–4; accepted stage outputs; locks; immutable run histories; conflict analyzer.

**Deliverables:**

- A typed override action contract with validated targets and before/after values.
- An append-only override history API containing actor, reason, timestamp, previous value, new value, and affected scope.
- Atomic pairing of a human change with the current lock/fixed-context representation where appropriate.
- Optimistic concurrency or equivalent stale-write detection returning `409 Conflict`.
- A shared cross-stage scope DTO/contract for sections, courses, teachers, and
  students beyond the implemented student-assignment scope.
- Placement and teacher solvers that limit decision variables to scope while
  loading all other accepted state as fixed context; the student solver's
  narrower scope behavior is already implemented.
- A dependency service that computes the smallest safe affected scope and previews it to the counselor.
- Automated proof that scoped runs do not change locked or out-of-scope records.

**Major Tasks:**

1. Audit whether the current section-only `ManualOverride` foreign key can represent teacher and enrollment changes honestly; make any additive audit-model change explicit.
2. Define canonical override actions rather than accepting arbitrary action strings.
3. Implement preview and apply services with validation, concurrency checks, and one transaction.
4. Pair persistent current-state locks with append-only history without making the audit row editable.
5. Add scope to each pure solver's input and distinguish decision variables from fixed context.
6. Compute downstream impact: a moved section may affect its teacher and enrolled students; a student move should not automatically reopen unrelated placement.
7. Compare complete before/after snapshots in tests to prove out-of-scope stability.
8. Integrate conflict analysis so counselors see the consequence before choosing another re-solve.

**Suggested Folder/Module Structure:**

```text
backend/apps/control/
  models.py
  serializers.py
  views.py
  services/
    overrides.py
    scope.py
backend/apps/scheduling/services/
  orchestration.py
scheduling_engine/
  scope.py
backend/tests/
  test_override_api.py
  test_scoped_resolve.py
```

**Estimated Difficulty:** 9/10

**Common Mistakes to Avoid:**

- Implementing scope as a filter after a full solve.
- Omitting out-of-scope state from capacity/conflict context.
- Mutating or deleting audit history.
- Treating every manual change as a full-year re-solve.
- Applying a change before showing its downstream scope.
- Storing ambiguous, unvalidated free-text values as the only machine-readable record.

**Definition of Done:**

- A counselor can preview and apply a manual change with a required reason.
- The corresponding fixed constraint is available to every later solve.
- The system can re-solve an affected subset while all locked and out-of-scope records remain unchanged.
- Stale concurrent edits produce a mergeable `409` response rather than last-write-wins data loss.
- Override history is append-only, queryable, and complete.

---

## Phase 6 — Role-Based Counselor, Teacher, and Student Frontend

**Current Status:** **Not implemented.** There is no frontend directory or client application in the repository.

**Goal:** Provide a role-safe web application for the complete human-reviewed scheduling workflow, beginning with the counselor planning experience and ending with read-only teacher/student schedules.

**Why It Comes Next:** The backend stage contracts and override semantics should be stable before a solo developer invests heavily in UI. At this point the frontend can expose real workflows instead of relying on mocks that repeatedly drift from the API.

**Dependencies:** Stable APIs from Phases 1–5; JWT authentication; a documented API schema; translation endpoint.

**Deliverables:**

- A React client consistent with the SDD unless a separate architecture decision justifies another stack.
- A typed API layer and centralized role/route configuration.
- Counselor pages for demand, section-plan scenarios, run comparison, review/approval, reconciliation, placement, teacher assignment, student assignment, conflicts, overrides, and run status.
- Teacher pages for preferences, availability, qualifications, and accepted personal schedule.
- Student pages for course requests and accepted personal schedule.
- A timetable grid representing semesters and recurring A–D blocks correctly.
- Translation API and English/French text resolution.
- Accessible loading, infeasible, warning, stale-state, and conflict experiences.
- Frontend unit/component tests and end-to-end tests for the critical counselor flow.

**Major Tasks:**

1. Publish a stable machine-readable API contract before generating or writing the client layer.
2. Implement authentication/token refresh and central role-gated routing.
3. Build the counselor planning/reconciliation screens first because they exercise the current product's strongest workflow.
4. Add each later solver review surface in pipeline order.
5. Visualize recommended versus approved values and never hide diagnostics behind a generic failure message.
6. Add override preview and affected-scope confirmation before apply.
7. Build teacher/student read-only schedule views using server-enforced ownership.
8. Add translation lookup and fallback behavior without moving scheduling logic into the browser.

**Suggested Folder/Module Structure:**

```text
frontend/
  src/
    api/
    components/
      common/
      planning/
      timetable/
      diagnostics/
    features/
      demand/
      section-planning/
      placement/
      teacher-assignment/
      student-assignment/
      overrides/
    pages/
    routes/
    i18n/
    tests/
```

**Estimated Difficulty:** 8/10

**Common Mistakes to Avoid:**

- Reimplementing permissions, demand calculations, or scheduling logic client-side.
- Assuming hidden navigation is authorization; the API remains authoritative.
- Flattening warnings, diagnostics, and conflicts into one error banner.
- Building only a polished timetable grid while omitting the review and approval workflow.
- Using mocked response shapes after real APIs are available.
- Treating A–D blocks as Monday–Thursday periods.

**Definition of Done:**

- A counselor can complete the Version 1 workflow without direct API tooling.
- Every solver stage visibly separates recommendation, warning, adjustment, approval, and accepted state.
- Teacher and student routes show only their own server-authorized data.
- English/French text resolves through the supported translation contract.
- Critical workflows have automated browser-level coverage and accessible keyboard/error behavior.

---

## Phase 7 — Version 1 Hardening, Scale Validation, and Documentation Alignment

**Current Status:** **Partially complete.** The project has a meaningful automated test suite and documented local setup, but it is not yet production- or portfolio-release hardened.

**Goal:** Validate the completed system at realistic scale, close security/operational gaps, automate regression checks, and synchronize documentation with the implemented architecture.

**Why It Comes Next:** Hardening must happen throughout development, but final performance, reliability, security, and documentation decisions depend on the actual placement/assignment workloads and stable API/UI contracts.

**Dependencies:** Phases 1–6.

**Deliverables:**

- A deterministic realistic fixture or approved anonymized dataset representing roughly 1,400 students, 80 teachers, and 250–350 sections.
- Performance profiles and bounded solve-time targets for every CP-SAT stage.
- A final execution decision for long-running solvers. If background processing is justified, a minimal reliable worker/status implementation with idempotency and persistent run state.
- Continuous integration for checks, pure-engine tests, Django tests, and frontend tests.
- Production-safe settings, CORS/host configuration, secret handling, secure transport assumptions, and dependency review.
- Structured request, audit, run-lifecycle, and solver diagnostic logging without secrets or student-sensitive payload leakage.
- Stable API documentation and examples.
- Updated README and SDD that match the actual run, approval, reconciliation, qualification, role, and migrationless-development architecture.
- A complete Version 1 acceptance checklist and portfolio demonstration dataset/script.

**Major Tasks:**

1. Build realistic fixtures and benchmark each solver independently and in sequence.
2. Confirm time limits return a usable feasible/suboptimal result or actionable infeasibility state.
3. Decide whether synchronous execution meets the deployment contract. If not, introduce only the smallest justified queue/worker architecture behind existing services.
4. Add persistent run status, idempotent retry behavior, and failure recovery where required.
5. Add CI without requiring local infrastructure that the project does not otherwise use.
6. Split development and production settings and run a security review of authorization, PII exposure, and secrets.
7. Add structured logging and audit correlation IDs.
8. Reconcile the SDD with implemented section planning and approval evolution.
9. Verify setup from a fresh clone using the documented migrationless schema procedure.
10. Run the full acceptance flow and archive the expected results for regression testing.

**Suggested Folder/Module Structure:**

```text
.github/workflows/
  test.yml
backend/config/
  settings/
    base.py
    development.py
    production.py
backend/tests/fixtures/
  realistic_school.py
docs/
  Software_Design_Document.md
  operations.md
  acceptance-test.md
```

Exact deployment files should follow the chosen hosting environment; do not add Docker or a particular broker without a concrete deployment need.

**Estimated Difficulty:** 8/10

**Common Mistakes to Avoid:**

- Treating a small synthetic solver test as proof of target-scale performance.
- Adding operational components without ownership, retry, and failure semantics.
- Logging secrets, raw tokens, qualification source text, or student PII.
- Creating Django migration files contrary to the current project convention.
- Running destructive database resets as an automated verification step.
- Updating the README while leaving the SDD's major implementation divergences unaddressed.

**Definition of Done:**

- CI reproduces all backend, pure-engine, and frontend checks from a clean environment.
- Each solver has a measured target-scale runtime, configured time bound, and documented degraded/suboptimal behavior.
- Production settings pass Django deployment checks with environment-provided secrets.
- Run failures and retries cannot partially apply or duplicate schedule state.
- Documentation consistently describes the actual architecture and schema workflow.
- The complete Version 1 counselor flow passes an automated or repeatable acceptance test.

---

## Explicitly Deferred Beyond Version 1

The following should not interrupt the dependency path above unless stakeholder evidence changes Version 1 scope:

- Automatic cross-listing or course merging. The domain needs explicit rules for shared enrollment, teacher load, section identity, credits, rooms, and counselor approval before this can be modeled safely.
- Automatic HR/SIS qualification import. Normalized qualifications and provenance already provide the correct integration boundary.
- NLP parsing of teacher preferences.
- Machine-learning demand forecasting.
- A monolithic joint placement/teacher/student solver.
- Multi-school tenancy and board-wide analytics.
- Real-time collaborative editing.
- AI-generated scheduling decisions that bypass counselor review.

These are not excuses to leave Version 1 gaps unresolved. They are boundaries protecting the current staged, auditable plan from scope expansion.

---

## Recommended Immediate Next Phase

> **Superseded implementation update (2026-08-08):** Phase 2 is now
> **Counselor-Reviewed Semester and A-D Placement With Staffing Feasibility**.
> See `docs/decisions/semester-placement-and-staffing-feasibility.md` for the
> accepted contract. It is implemented synchronously as a review-first stage.
> It places timing only, proves anonymous staffing feasibility, and explicitly
> defers rooms, named teacher assignments, and student assignments. Any older
> paragraph in this roadmap that says Phase 2 assigns rooms or requires a queue
> is historical planning context, not the current implementation contract.

> **Superseded implementation update (2026-08-08):** Phase 3 is now
> **Counselor-Reviewed Named Teacher Assignment**. See
> `docs/decisions/named-teacher-assignment.md`. It runs synchronously after
> accepted timing, respects qualifications, availability, annual/semester load,
> locks, and counselor course rules, and writes named teachers only after
> approval. Rooms and students remain separate later stages.

Begin **Phase 2 — Counselor-Reviewed A–D Block and Room Placement** after final integration review of the completed core reconciliation workflow.

The system now makes staffing-aware section recommendations, records immutable runs, creates traceable drafts after approval, and safely reconciles later plans without deleting operational history. Counselors can see the exact keep/move/create/retire/reactivate consequences before applying them, while protected sections remain fixed.

The next product step can therefore use the stable active section set to decide when and where each section runs. That work should retain the same immutable run, preview, approval, and conflict-protection pattern established here.

Reconciliation has unlocked the next scheduling stage cleanly: the placement solver can operate on a well-defined set of active sections and attach `SectionSchedule` and room/block decisions without relying on ambiguous replacement semantics.

Other plausible phases should wait:

- **A–D block/room placement** is now the recommended next implementation phase because section identity and retirement rules are stable.
- **Named teacher assignment** depends on placed blocks for availability and overlap checks.
- **Student assignment** depends on placed sections and a prerequisite-evidence decision.
- **General scoped re-solving** needs real downstream solver stages and accepted outputs to scope.
- **Frontend work** will have much less API churn after reconciliation and downstream review contracts are defined.
- **Cross-listing** is not a safe automatic escape hatch for low demand and requires a separate domain design.

The immediate implementation boundary is now counselor-reviewed A–D block and room placement. Historical-demand readiness, lock-model consolidation, and cross-listing remain separate deliberate increments.

---

*End of Implementation Roadmap.*
