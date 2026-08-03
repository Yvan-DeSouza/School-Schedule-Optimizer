# Implementation Roadmap
## Intelligent School Timetabling System — Version 1

**Document Type:** Implementation Roadmap (companion to the Software Design Document)
**Author Role:** Lead Software Architect
**Scope:** 10 major phases, current state (finalized architecture + schema) → working Version 1 system
**Context:** Solo developer, long-duration portfolio project

---

## How to Use This Document

Each phase below is a complete unit of engineering work — not a sprint, not a day, but a coherent chunk that should be finished (per its Definition of Done) before the next phase begins in earnest. Phases are ordered by priority, and priority here means **"what does everything after this depend on,"** not "what's most exciting to build." This is deliberate: for a solo, long-running project, the biggest risk isn't lack of skill, it's silent architectural drift and rework caused by skipping foundations — so foundational phases are front-loaded even though they are the least visually impressive.

Every phase assumes the Software Design Document (SDD) is final and unmodified. No phase below revisits the architecture; each one *implements* a specific section of it.

---

## Phase 1 — Project Foundation & Developer Environment

**Goal:** Establish a reproducible, tested, continuously-integrated development environment before any domain code is written.

**Why this phase comes next:** Every other phase assumes a working local environment, a real Postgres/Redis instance, a test runner, and a CI pipeline that catches regressions. Skipping this in favor of "just start coding" is the single most common way solo long-duration projects rot — inconsistent formatting, undetected regressions, and "works on my machine" bugs accumulate silently until they become expensive to fix. This phase is cheap now and increasingly expensive to retrofit later.

**Dependencies:** None — only the finalized SDD.

**Deliverables:**
- Git repository with a documented branching strategy (e.g., trunk-based with short-lived feature branches).
- Docker Compose configuration running PostgreSQL and Redis locally.
- Django project skeleton with split settings (`base.py`, `dev.py`, `prod.py`).
- Dependency management via Poetry (or pip-tools) with a locked dependency file.
- `pytest` + `pytest-django` configured and running (even against a trivial placeholder test).
- Pre-commit hooks: `black`, `isort`, `ruff` (or `flake8`), optionally `mypy`.
- GitHub Actions (or equivalent) CI pipeline: lint → test on every push/PR.
- A README with exact setup instructions a future-you (returning after a gap) can follow cold.

**Major Tasks:**
1. Initialize the repository and choose the dependency manager.
2. Write `docker-compose.yml` for Postgres + Redis.
3. Scaffold the Django project with split settings and environment-variable-driven configuration (12-factor style).
4. Configure `pytest`/`pytest-django`, confirm a trivial test passes.
5. Install and configure pre-commit hooks; run once against the empty project.
6. Write the CI workflow file; confirm it runs green on a throwaway commit.
7. Write the README (setup, common commands, project layout pointer to the SDD).

**Suggested Folder/Module Structure:**
```
repo-root/
  backend/
    config/            # Django project package (settings/, urls.py, wsgi.py, asgi.py)
    apps/               # (empty for now — populated in Phase 2)
    manage.py
  docker/
    docker-compose.yml
  .github/workflows/ci.yml
  docs/
    SDD.md
    ROADMAP.md
  pyproject.toml
  README.md
```

**Estimated Difficulty:** 3/10

**Common Mistakes to Avoid:**
- Deferring CI "until there's real code to test" — the value of CI is catching regressions from day one, not validating a finished product.
- Not containerizing Postgres/Redis early, leading to environment drift between machines or after long breaks from the project.
- Over-engineering settings/config for scenarios (multi-region, multi-tenant) that aren't in V1 scope per the SDD.

**Definition of Done:**
- `docker-compose up` brings up Postgres and Redis with no manual steps.
- `pytest` runs successfully both locally and in CI on every push.
- Pre-commit hooks block a badly-formatted commit locally.
- A fresh `git clone` followed by the README's instructions results in a running dev server within a few minutes.

---

## Phase 2 — Data Layer Implementation (Version 1 Schema as Django Models)

**Goal:** Faithfully translate the finalized Version 1 schema (SDD Section 16) into Django models, organized into apps per SDD Section 19.2, with migrations and baseline tests.

**Why this phase comes next:** Every API, service, and solver in every later phase reads or writes this schema. It must exist, migrated, and verified against its own constraints (uniqueness, cascade behavior) before anything is built on top of it.

**Dependencies:** Phase 1 (Django project, Postgres container, pytest).

**Deliverables:**
- Django apps: `people`, `courses`, `scheduling_control`, `constraints`, `i18n` (per SDD 19.2).
- All models from the SDD schema, ported without redesign.
- Initial migrations, applied cleanly from an empty database.
- Django admin registration for every model (a fast internal CRUD tool useful throughout development).
- `factory_boy` factories for every model, for use in all future tests.
- Unit tests verifying every unique constraint, nullable/blank rule, and cascade behavior defined in the schema.

**Major Tasks:**
1. Create the five Django apps per the SDD's suggested mapping.
2. Port every model file verbatim, adding only non-breaking conveniences (`__str__`, `Meta.ordering`) — no field changes, no new constraints beyond what's specified.
3. Generate and apply migrations against a clean database.
4. Register all models in `admin.py` for each app.
5. Write `factory_boy` factories for every model.
6. Write tests that deliberately violate each unique constraint and assert the expected `IntegrityError`/validation behavior.

**Suggested Folder/Module Structure:**
```
backend/apps/
  people/{models.py, admin.py, migrations/, factories.py, tests/}
  courses/{models.py, admin.py, migrations/, factories.py, tests/}
  scheduling_control/{models.py, admin.py, migrations/, factories.py, tests/}
  constraints/{models.py, admin.py, migrations/, factories.py, tests/}
  i18n/{models.py, admin.py, migrations/, tests/}
```

**Estimated Difficulty:** 4/10

**Common Mistakes to Avoid:**
- Quietly "improving" the schema while porting it — the SDD explicitly treats the schema as finalized; any perceived improvement should be raised as a proposal, not silently implemented.
- Skipping the composite indexes called out in SDD Section 25 (`(section, timeslot)`, `(student, section)`) — retrofitting indexes later is far more disruptive than adding them at initial migration time.
- Under-testing constraints, only to discover a violated assumption later while debugging a solver in Phase 7 or 8, far from the actual cause.

**Definition of Done:**
- `python manage.py migrate` runs cleanly against an empty database with zero errors or warnings.
- Every model is visible and editable through Django admin.
- The test suite includes at least one test per unique/foreign-key constraint in the schema, and all pass in CI.

---

## Phase 3 — Authentication, RBAC & Auth-Profile Linkage

**Goal:** Implement the auth linkage assumption from SDD Section 11 (a `User` linked optionally to `Student`/`Teacher`/`Counselor`) and reusable role-based permission classes.

**Why this phase comes next:** Nearly every endpoint built from Phase 4 onward needs real authorization. Building CRUD endpoints first and "adding security later" is a well-known anti-pattern — it means every endpoint gets revisited a second time, and it's easy to miss one.

**Dependencies:** Phase 2 (`Student`, `Teacher`, `Counselor` models must exist to link to).

**Deliverables:**
- Auth linkage between Django's `User` and the domain profile models.
- JWT authentication (`djangorestframework-simplejwt` or equivalent) with login/refresh endpoints.
- Reusable DRF permission classes: `IsCounselor`, `IsTeacher`, `IsStudent`, `IsOwnerOrCounselor`.
- A `seed_dev_users` management command creating one of each role for local development.
- Tests proving each permission class correctly allows/denies access, including the critical case of one student attempting to read another student's data.

**Major Tasks:**
1. Add the profile-linkage relationship (additive migration, per SDD Section 11's assumption).
2. Install and configure JWT auth; build login/refresh views.
3. Write the four permission classes as standalone, reusable, independently tested units.
4. Write the `seed_dev_users` command.
5. Write permission tests across a representative endpoint for every role combination, including negative cases.

**Suggested Folder/Module Structure:**
```
backend/apps/people/
  auth.py            # profile-linkage model/logic
  permissions.py     # IsCounselor, IsTeacher, IsStudent, IsOwnerOrCounselor
  management/commands/seed_dev_users.py
backend/apps/api/
  auth_views.py       # login/refresh
  urls.py
```

**Estimated Difficulty:** 5/10

**Common Mistakes to Avoid:**
- Conflating Django's `is_staff`/`is_superuser` with the domain's Counselor/Administrator distinction instead of treating them as a layered privilege (per SDD Section 11) — this leads to confusing, hard-to-audit permission checks later.
- Hardcoding `if request.user.role == "teacher"` checks inline in views instead of reusable permission classes — duplicated logic is where security bugs hide.
- Testing only the "happy path" (a counselor can access everything) and skipping the negative path (a student cannot access another student's data) — the negative path is the one that actually matters for security.

**Definition of Done:**
- Login returns a valid JWT; protected endpoints correctly return 401 (unauthenticated) or 403 (unauthorized) as appropriate.
- Every permission class has a passing test for both an allowed and a denied case.
- A test explicitly proves Student A cannot read Student B's `Enrollment`/`CourseRequest` data even while authenticated.

---

## Phase 4 — Core Domain CRUD API (Courses, Sections, Requests)

**Goal:** Implement the Core Domain API surface from SDD Sections 17.1–17.2: course catalog management and course-request intake (Stages 1–2 of the pipeline).

**Why this phase comes next:** This is the first end-to-end vertical slice of real product value — the first place a counselor or student can actually *do something* useful — and it's the first place the data layer (Phase 2) and auth layer (Phase 3) get exercised together under realistic conditions.

**Dependencies:** Phases 2 and 3.

**Deliverables:**
- `/api/courses/`, `/api/course-requests/`, `/api/demand/summary/` endpoints (SDD 17.1–17.2).
- Serializers with explicit validation surfacing constraint violations as friendly 400s, not raw `IntegrityError` 500s.
- Demand-summary aggregation implemented as a testable service function, not inline view logic.
- Auto-generated API documentation (`drf-spectacular` or equivalent).
- Integration tests covering permissions, validation, and aggregation correctness.

**Major Tasks:**
1. Build serializers and viewsets for `Course` and `CourseRequest`, wired to the Phase 3 permission classes.
2. Implement the demand-summary aggregation as a standalone service function callable independently of the view (this same logic groundwork feeds the `CourseConflict` population strategy in Phase 6).
3. Add pagination/filtering to list endpoints.
4. Configure and verify OpenAPI schema generation.
5. Write integration tests: happy path, validation failures, and permission boundaries.

**Suggested Folder/Module Structure:**
```
backend/apps/courses/
  serializers.py
  views.py
  urls.py
  services/
    demand.py         # demand-summary aggregation logic
  tests/
backend/apps/api/
  urls.py              # root router aggregating all app urls
```

**Estimated Difficulty:** 5/10

**Common Mistakes to Avoid:**
- Writing the demand-aggregation logic directly inside the DRF view — this violates the thin-view/service-layer separation the SDD specifies (Section 19.3) and makes the logic hard to reuse or unit-test later.
- Letting a duplicate `CourseRequest` submission surface as an unhandled 500 instead of a clear validation error referencing the existing `unique_student_course_request` constraint.

**Definition of Done:**
- A counselor can create a `Course` and its `CoursePrerequisite`s via the API.
- A student can submit primary and alternate `CourseRequest`s, with duplicates correctly rejected with a clear error.
- The demand-summary endpoint returns correct aggregates against a seeded dataset, verified by test.
- OpenAPI docs render and accurately describe every endpoint in this phase.

---

## Phase 5 — Constraint & Lock Management API

**Goal:** Implement full CRUD for the entire Constraints group (SDD Sections 17.4–17.5): qualifications, teacher preferences/availability, hard/soft constraints, section locks, and course conflicts.

**Why this phase comes next:** The scheduling engine (Phases 6–8) is meaningless without real constraint data to operate on. This phase also completes *all* of the "data collection mode" functionality described in SDD Section 6 before a single line of solver code is written — meaning the entire non-optimization half of the application can be validated end-to-end on its own first.

**Dependencies:** Phase 4 (`Section` records must exist to be locked/constrained against).

**Deliverables:**
- Full CRUD endpoints for every model in the Constraints group.
- A real, importable **Constraint & Lock Service** module (SDD Section 19.1) — not just thin viewsets — since this exact logic will be reused by the Engine Adapter in Phase 7.
- Server-side business-rule validation (e.g., rejecting a `SectionLock` to a teacher who lacks the required `CourseQualificationRequirement`), per SDD Section 22.
- Tests covering every endpoint plus the qualification-lock business rule specifically.

**Major Tasks:**
1. Build serializers/viewsets for `TeacherQualification`, `TeacherCoursePreference`, `TeacherAvailability`, `TeacherCurrentCourse`, `HardConstraint`, `SoftConstraint`, `CounselorConstraintPreference`, `SectionLock`, `CourseConflict`, `CourseRoomRequirement`, `CourseQualificationRequirement`.
2. Implement the Constraint & Lock Service as a standalone module containing the actual validation and business logic, called by (but not embedded in) the viewsets.
3. Write the qualification-lock validation rule and its test.
4. Write a full suite of endpoint tests.

**Suggested Folder/Module Structure:**
```
backend/apps/constraints/
  serializers.py
  views.py
  services.py          # Constraint & Lock Service — reused in Phase 7
  tests/
```

**Estimated Difficulty:** 6/10

**Common Mistakes to Avoid:**
- Implementing business-rule validation only in the frontend (a future phase) and skipping server-side enforcement — a data-integrity and security risk regardless of frontend correctness.
- Writing validation logic directly inside viewsets now, then being forced to extract it into a reusable service later when the Engine Adapter needs the same logic in Phase 7 — build the service module now, even though only the API uses it today.

**Definition of Done:**
- Every constraint table is fully manageable via API with correct validation.
- The "lock without required qualification" rule is enforced and covered by a passing test.
- A complete manual-locks dataset can be constructed via API alone for a seeded fixture school, ready to feed the scheduling engine in the next phases.

---

## Phase 6 — Scheduling Engine Package: Demand Estimator + Constraint Compiler

**Goal:** Build the standalone `scheduling_engine` package (SDD Section 14) as a Django-independent library, implementing its non-solver modules first: the Demand Analyzer, Section Count Estimator, and Constraint Compiler.

**Why this phase comes next:** This package is the architectural core of the entire project and the SDD's most important design constraint — zero Django dependency. Building it in true isolation *before* any CP-SAT solver exists forces the DTO boundary between Django and the engine to be real and enforced from day one, rather than becoming an aspiration that erodes once solver deadlines create pressure to take shortcuts.

**Dependencies:** Phase 5 (constraint data model must be finalized so DTOs can accurately mirror it), though the package itself can and should be developed and tested against hand-built fixtures with no database involved.

**Deliverables:**
- An installable internal package (or clearly separated top-level directory) containing dataclass-based DTOs, the Demand Analyzer, the Section Count Estimator, and the Constraint Compiler.
- A test suite that runs with **no database and no Django settings module loaded at all** — pure Python, pure fixtures.
- A package-level README documenting and asserting the independence guarantee (e.g., a CI check that fails if `django` ever appears in this package's imports).

**Major Tasks:**
1. Define DTOs (`CourseDTO`, `SectionDTO`, `ConstraintSetDTO`, etc.) shaped around what the solvers actually need, not around ORM field names.
2. Implement the Demand Analyzer (Pandas-based aggregation of course-request volume vs. historical trends).
3. Implement the Section Count Estimator heuristic (SDD Section 20.1).
4. Implement the Constraint Compiler, merging every constraint DTO type into one in-memory structure consumed by every downstream solver.
5. Write a comprehensive fixture-based test suite covering every constraint type in the schema.
6. Add a CI check (a simple import-lint rule) that fails the build if `django` is ever imported inside this package.

**Suggested Folder/Module Structure:**
```
scheduling_engine/
  dto.py
  demand_analyzer.py
  section_estimator.py
  constraint_compiler.py
  tests/
    fixtures.py
    test_demand_analyzer.py
    test_section_estimator.py
    test_constraint_compiler.py
  README.md            # documents the independence guarantee
```

**Estimated Difficulty:** 7/10

**Common Mistakes to Avoid:**
- Importing a Django model "just this once, for convenience" — this is the single most damaging shortcut available in this phase, because it silently breaks the SDD's core architectural guarantee and is easy to miss in review.
- Under-testing the Constraint Compiler specifically — every solver phase that follows depends on it, so a subtle bug here propagates invisibly into every later phase's results.
- Shaping DTOs to mirror ORM fields one-to-one instead of what the solver logic actually needs, creating unnecessary coupling to the schema's exact representation.

**Definition of Done:**
- The `scheduling_engine` package contains zero references to `django` anywhere in its source, enforced by an automated check.
- Its test suite runs and passes without a database connection or Django settings loaded.
- The Constraint Compiler correctly merges a hand-built fixture set exercising every constraint type defined in the SDD schema.

---

## Phase 7 — Section Placement Solver + Engine Adapter + Async Job Infrastructure

**Goal:** Implement the first CP-SAT solver (SDD Section 20.2), the Engine Adapter that bridges Django and the engine package (SDD Section 19.4), and the Celery/Redis async job infrastructure described in SDD Section 7.

**Why this phase comes next:** This is the first true end-to-end automation slice, and it proves out the riskiest piece of new infrastructure — the async job queue, the adapter's load/solve/write-back cycle, and a real CP-SAT model — all at once, but scoped to exactly one solver. Proving this pattern once, carefully, before building two more solvers on top of it is far cheaper than discovering an infrastructure flaw after all three solvers exist.

**Dependencies:** Phase 6 (Constraint Compiler and DTOs), Phase 1 (Redis container already running).

**Deliverables:**
- The Section Placement Solver, implemented in OR-Tools CP-SAT per SDD Section 20.2.
- The Engine Adapter: load (ORM → DTOs), invoke solver, write back inside a single atomic transaction.
- A configured Celery application with a `run_scheduling_stage` task.
- `POST /api/scheduling/runs/` and `GET /api/scheduling/runs/{job_id}/` endpoints (SDD Section 17.6).
- An end-to-end integration test: seed a small fixture school, trigger a run, poll to completion, assert a valid, conflict-reduced `SectionSchedule`.

**Major Tasks:**
1. Implement the CP-SAT model for section placement, including all hard constraints and the conflict-minimization objective from SDD Section 20.2.
2. Build the Engine Adapter's load and save functions, with the save step wrapped in a single transaction (SDD Section 22).
3. Configure Celery with Redis as the broker; define the `run_scheduling_stage` task.
4. Build the two job-related API endpoints and their serializers.
5. Enforce a bounded solver time limit (SDD Section 25) and write a test confirming it is respected.
6. Write a full end-to-end integration test covering the seed → trigger → poll → assert cycle.

**Suggested Folder/Module Structure:**
```
scheduling_engine/solvers/
  section_placement.py
backend/apps/scheduling_jobs/
  adapter.py            # the ONLY module allowed to import both Django and scheduling_engine
  tasks.py
  views.py
  serializers.py
  tests/
```

**Estimated Difficulty:** 8/10

**Common Mistakes to Avoid:**
- Letting the CP-SAT model "peek" at a Django object for convenience — this breaks the boundary Phase 6 worked to establish; all engine code must operate strictly on DTOs.
- Leaving the solver's time limit unbounded during local development and forgetting to re-bound it before considering the phase complete — an unbounded solve on a larger fixture can silently hang for a very long time.
- Writing `SectionSchedule` results outside a single atomic transaction, risking a partially-applied schedule if the worker crashes mid-write (SDD Section 22).

**Definition of Done:**
- Triggering a run against a realistic seeded dataset (30–50 sections) produces a valid `SectionSchedule` with no double-booked room/timeslot pairs and full respect for all seeded `SectionLock`s.
- The job status endpoint correctly reports queued/running/completed/failed states.
- A test confirms the solver's time limit is enforced and that a timed-out run is marked `completed_suboptimal` rather than left in an ambiguous state.

---

## Phase 8 — Teacher & Student Assignment Solvers + Conflict Analyzer

**Goal:** Implement the remaining two CP-SAT solvers (SDD Sections 20.3, 20.4) and the Conflict Analyzer (SDD Section 20.5), completing the fully automated portion of the scheduling pipeline.

**Why this phase comes next:** With the adapter pattern and job infrastructure proven in Phase 7, these two solvers carry lower *infrastructural* risk but higher *domain-logic* volume — this is the natural next step once the riskiest plumbing is de-risked. Completing this phase finishes Stages 7–10 of the pipeline described in SDD Section 13.

**Dependencies:** Phase 7 (adapter and job infrastructure, reused as-is).

**Deliverables:**
- The Teacher Assignment Solver (SDD Section 20.3).
- The Student Assignment Solver (SDD Section 20.4).
- The Conflict Analyzer read-only report (SDD Section 20.5).
- `GET /api/conflicts/report/` and `GET /api/timetable/` endpoints (SDD Section 17.6–17.7).
- A full-pipeline integration test: run all three solver stages in sequence against a seeded fixture school and assert a coherent, fully-scheduled result.

**Major Tasks:**
1. Implement the Teacher Assignment CP-SAT model, including qualification, availability, workload, and preference constraints.
2. Implement the Student Assignment CP-SAT model, including prerequisites, capacity, and per-student conflict constraints, correctly **not** depending on teacher assignment having occurred (per SDD Section 20.4's explicit note).
3. Implement the Conflict Analyzer's aggregation logic.
4. Wire both new solvers into the existing task/adapter pattern from Phase 7.
5. Build the composed timetable endpoint and the conflict-report endpoint.
6. Write a full Stage 7→8→9 sequential integration test, not just isolated per-stage tests.

**Suggested Folder/Module Structure:**
```
scheduling_engine/solvers/
  teacher_assignment.py
  student_assignment.py
scheduling_engine/
  conflict_analyzer.py
backend/apps/scheduling_jobs/
  # extend existing views.py / tasks.py from Phase 7
```

**Estimated Difficulty:** 8/10

**Common Mistakes to Avoid:**
- Accidentally introducing a hard dependency on teacher assignment inside the Student Assignment Solver's capacity logic — the SDD explicitly notes this dependency should not exist; a subtle bug here would silently produce an over-constrained or under-constrained model.
- Testing each solver only in isolation and skipping the full-sequence integration test — the most likely real-world bug is a mismatch between one stage's output shape and the next stage's expected input, which isolated tests cannot catch.

**Definition of Done:**
- Running all three stages sequentially against a ~250-section fixture produces a complete timetable satisfying every hard constraint defined in the SDD.
- The conflict report correctly flags intentionally-seeded "bad" scenarios (an over-enrolled section, an unqualified-teacher assignment attempt) in a dedicated test.

---

## Phase 9 — Manual Override Workflow + Scoped Re-Solve

**Goal:** Implement the Manual Override Workflow (SDD Section 21) and the scope parameter (SDD Section 20.6) across all three solvers, closing the Stage 11 feedback loop from SDD Section 13.

**Why this phase comes next:** Without this phase, the system can only produce a one-shot "first draft" schedule — it cannot support the iterative, months-long review-and-adjustment process that is the documented reality of school timetabling (SDD Section 6). This is the entire reason the project exists as a *decision-support* tool rather than a one-shot generator, which makes this phase non-optional despite arriving relatively late.

**Dependencies:** Phase 8 (all three solvers must exist to be scoped and re-run).

**Deliverables:**
- `POST /api/overrides/` and `POST /api/overrides/{id}/apply/` endpoints (SDD Section 17.7).
- A `scope` parameter accepted and genuinely honored by all three solver tasks — meaning only in-scope entities become decision variables, while everything else is loaded as fixed context.
- The backing endpoint for the Override History page.
- Tests proving a scoped re-run never alters a locked or out-of-scope entity, verified by direct comparison, not inference.

**Major Tasks:**
1. Build the Override Service (SDD Section 19.1), pairing every `ManualOverride` write with the corresponding `SectionLock` upsert.
2. Extend all three solver invocations to accept a `scope` argument and reduce their decision-variable sets accordingly, while still loading out-of-scope entities as fixed context for conflict/capacity checks.
3. Implement the "regenerate only affected portions" logic in the Scheduling Orchestration Service.
4. Write a test that locks most of a fixture's schedule, applies one override, triggers a scoped re-solve, and asserts every out-of-scope row is byte-for-byte unchanged.

**Suggested Folder/Module Structure:**
```
backend/apps/scheduling_control/
  services.py           # Override Service
  views.py
scheduling_engine/solvers/
  # extend section_placement.py, teacher_assignment.py, student_assignment.py
  # to uniformly accept a `scope` argument
```

**Estimated Difficulty:** 7/10

**Common Mistakes to Avoid:**
- Implementing "scope" as a post-hoc filter applied after a full solve, rather than as a genuine reduction of the decision-variable set — this does not guarantee untouched entities actually stay untouched, defeating the entire purpose of scoped re-solving.
- Forgetting that out-of-scope/locked entities still need to be loaded as *context* (for conflict and capacity checks against in-scope entities) even though they are not themselves decision variables.

**Definition of Done:**
- A scoped re-run against a fixture with 300 sections, where only 5 are in scope, provably leaves the other 295 `SectionSchedule`/`Section.teacher`/`Enrollment` records byte-for-byte unchanged, verified by an automated test comparison — not manual inspection.
- The Override History endpoint correctly and completely reflects every override action taken.

---

## Phase 10 — Frontend Application (React, All Role-Based Pages)

**Goal:** Build the complete React frontend (SDD Section 18) against the now fully-functional API, including the Timetable Grid, Scheduling Control Center, and role-based navigation for Counselors, Teachers, and Students.

**Why this phase comes next (and not earlier):** The frontend is the most visible part of the project but carries the least architectural risk once the API is stable — building it earlier would mean repeatedly reshaping UI around a backend that was still changing shape through Phases 4–9, which is one of the most common sources of wasted effort in solo full-stack projects. Building it last, against a finished and tested API, means the frontend work is almost entirely UI/UX craftsmanship rather than architecture-under-uncertainty.

**Dependencies:** Phases 3 through 9 — a functionally complete, tested API is this phase's only real dependency.

**Deliverables:**
- The full React application, structured exactly per SDD Section 18.4.
- React Query integration for all server state, per SDD Section 18.3's explicit recommendation.
- Role-gated routing per SDD Section 18.2.
- The Timetable Grid, Scheduling Control Center (with job-status polling), Conflict & Issue Report, and Override History pages.
- Bilingual (EN/FR) text resolution against `/api/translations/`.

**Major Tasks:**
1. Scaffold the app (Vite + React) and set up the typed API client layer covering every endpoint from SDD Section 17.
2. Implement React Query hooks per feature area (courses, constraints, scheduling runs, overrides).
3. Build the central route/permission configuration described in SDD Section 18.2 and every page listed in SDD Section 18.1.
4. Implement the Timetable Grid visualization and the Scheduling Control Center's job-polling UI.
5. Implement i18n resolution against the translation endpoint.
6. Conduct a full manual walkthrough of an entire scheduling cycle through the UI alone.

**Suggested Folder/Module Structure:** (exactly per SDD Section 18.4)
```
src/
  api/
  components/{common, timetable, constraints, scheduling}
  pages/
  hooks/
  state/
  i18n/
  routes/
```

**Estimated Difficulty:** 6/10

**Common Mistakes to Avoid:**
- Building against hand-mocked data before the real API is stable and never fully reconciling the two — this produces a frontend that looks finished in isolation but doesn't actually work against the real backend.
- Re-implementing manual caching/loading-state logic instead of using React Query as specified in SDD Section 18.3, recreating exactly the maintenance burden that section was written to avoid.

**Definition of Done:**
- A counselor can complete an entire scheduling cycle through the UI alone — enter data, trigger all three solver stages, review the conflict report, apply an override, and see the timetable update — without needing any API tooling.
- Teacher and Student read-only views correctly show only their own data, matching the permission tests written in Phase 3.

---

## Which Phase to Start Immediately

**Start with Phase 1 — Project Foundation & Developer Environment.**

This isn't a default answer — it's the highest-leverage phase in the entire roadmap for three specific reasons tied to this project's constraints:

1. **Every later phase's Definition of Done depends on it existing first.** Nearly every DoD in this document ends in some version of "and all tests pass in CI." That criterion is meaningless until Phase 1's test runner and CI pipeline exist. Skipping or shortcutting Phase 1 doesn't remove this work — it just defers it, disguised, into every subsequent phase, where it's more expensive to retrofit.

2. **This is a long-duration, solo project — continuity is the actual constraint, not raw coding speed.** The biggest risk to a project like this isn't any single hard algorithm (the CP-SAT modeling in Phases 7–8, while difficult, is well-bounded and well-documented territory). The biggest risk is *returning to the project after a multi-week gap* and losing time to environment drift, forgotten setup steps, or silently-broken tests. A solid README, working Docker Compose, and a green CI pipeline are what make Phase 6's return-after-a-break trivial instead of a half-day of environment archaeology.

3. **It is the cheapest phase, by a wide margin, and the cost of skipping it compounds.** At 3/10 difficulty, Phase 1 is the easiest phase on this list — and the cost of *not* doing it properly grows with every phase that follows, since more code means more surface area for the exact problems (inconsistent formatting, undetected regressions, irreproducible environments) it exists to prevent.

In short: Phase 1 has the highest ratio of "how much future pain this prevents" to "how much effort this takes," which is the definition of leverage in a project of this shape and duration.

---

*End of Implementation Roadmap.*