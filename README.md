# School-Schedule-Optimizer

## Backend Data Layer Setup

This project currently focuses on stabilizing the Django/PostgreSQL data layer for
the Intelligent School Timetabling System. The Software Design Document in
`docs/Software_Design_Document.md` is the architectural source of truth.

### Install Dependencies

Create and activate a virtual environment, then install the root requirements:

```bash
python -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
```

### Environment Variables

Create a `.env` file in the repository root with at least:

```bash
SECRET_KEY=your-local-development-secret
```

The local Django settings expect a PostgreSQL database named `school_scheduler`
available on `localhost:5432` with the credentials currently configured in
`backend/config/settings.py`.

### Local Schema Policy

This pre-production project intentionally does not keep Django migration files.
After recreating the local database, create built-in Django tables and all app
tables directly from the current model definitions with:

```bash
python backend/manage.py migrate --run-syncdb
```

Do not run `makemigrations`; change the models first, then rebuild the local
database when a schema reset is appropriate. Pytest is configured to create its
test database directly from the models as well.

### Verify The Data Layer

Run these commands from the repository root:

```bash
python backend/manage.py check
python backend/manage.py migrate --run-syncdb
pytest
```

Expected result:

- Django system checks report no issues.
- Django synchronizes the domain tables directly from the current models.
- The pytest suite passes, including model constraint, validator, cascade, and
  `SET_NULL` relationship tests.

## Authentication And Local Roles

The backend uses Django REST Framework with SimpleJWT. Domain users are linked
to Django `User` accounts through the `Student`, `Teacher`, and `Counselor`
models. School-level account roles such as `staff`, `director`, and `unknown`
use `UserRoleProfile`.

Supported role strings:

- `student`
- `teacher`
- `counselor`
- `staff`
- `director`
- `unknown`

Create local development users with:

```bash
python backend/manage.py seed_dev_users
```

Set `DEV_USER_PASSWORD` in `.env` before running the command. It creates these
local accounts using that configured password:

```text
counselor / counselor@example.com
teacher / teacher@example.com
student / student@example.com
staff / staff@example.com
director / director@example.com
unknown / unknown@example.com
```

Request a JWT access token:

```bash
curl -X POST http://localhost:8000/api/auth/login/ ^
  -H "Content-Type: application/json" ^
  -d "{\"username\":\"student\",\"password\":\"<DEV_USER_PASSWORD>\"}"
```

Use the access token to inspect the current user:

```bash
curl http://localhost:8000/api/me/ ^
  -H "Authorization: Bearer <access-token>"
```

Expected response shape:

```json
{
  "id": 1,
  "username": "student",
  "email": "student@example.com",
  "role": "student",
  "profile_id": 1
}
```

## Core Domain API

All core endpoints require a JWT access token. List endpoints are paginated with
25 results per page and return DRF's standard `count`, `next`, `previous`, and
`results` fields.

| Route | Student | Teacher | Counselor / staff / director |
| --- | --- | --- | --- |
| `/api/courses/` | Read | Read | Read/write |
| `/api/sections/` | No access | Assigned sections only | Read/write |
| `/api/course-requests/` | Own requests only | No access | Read/write all |
| `/api/demand/summary/?academic_year=<id>` | No access | No access | Read |

## Supporting Reference Data API

All recognized roles can read `/api/academic-years/`, `/api/rooms/`, and
`/api/timeslots/`. Only staff and directors can change them. Deletion is blocked
when a record is already referenced by school or scheduling data.

Timeslots use the permanent A–D block system. For example, create Block A with:

```json
{
  "academic_year": 1,
  "semester": 1,
  "block": "A",
  "is_available": true
}
```

The response includes its fixed four-day rotation as read-only `rotation` data.

## Section Count Recommendations

Counselors, staff, and directors can request a read-only planning recommendation:

```text
GET /api/planning/section-count-recommendations/?academic_year=<id>
```

The endpoint loads Django data into the standalone scheduling engine and returns
recommended counts without creating sections. Historical demand uses all prior
academic years with a three-year recency half-life: data from three years ago
counts half as much as immediately prior-year data. Counselors review the result
and create approved sections through `/api/sections/`.

Create a course as a planning role:

```bash
curl -X POST http://localhost:8000/api/courses/ ^
  -H "Authorization: Bearer <access-token>" ^
  -H "Content-Type: application/json" ^
  -d "{\"name\":\"Calculus and Vectors\",\"grade_level\":12,\"course_code\":\"MCV4U\",\"category\":\"math\",\"capacity_min\":10,\"capacity_max\":30,\"is_online\":false}"
```

Students create their own course request; the server derives the student from
the JWT and does not trust a submitted student ID:

```json
{
  "academic_year": 1,
  "course": 5,
  "is_mandatory": false,
  "request_type": "primary"
}
```

Demand summary response:

```json
[
  {
    "course_id": 5,
    "course_code": "MCV4U",
    "course_name": "Calculus and Vectors",
    "primary_requests": 70,
    "alternate_requests": 12,
    "total_requests": 82
  }
]
```

## Staffing-Aware Section Planning

The CP-SAT planning layer creates an immutable annual and semester-level
recommendation. A run itself is read-only and creates nothing. After review, a
counselor, staff member, or director can explicitly approve all or part of a
completed run to create unstaffed, unlocked draft `Section` records. Planning
never assigns named teachers, students, rooms, or timetable blocks. Other roles
have no access.

| Route | Purpose |
| --- | --- |
| `/api/planning/capacity-profiles/` | CRUD shared and course-specific class-size policies |
| `/api/planning/course-priority-profiles/` | CRUD named four-tier demand priorities |
| `/api/planning/teacher-capacities/` | CRUD teacher/year/semester section capacity |
| `POST /api/courses/{id}/capacity-policy/` | Attach a shared policy or copy-on-write a custom policy |
| `POST /api/planning/section-count-runs/` | Create a frozen base plan or what-if scenario |
| `GET /api/planning/section-count-runs/` | List frozen planning runs |
| `GET /api/planning/section-count-runs/{id}/` | Retrieve one complete recommendation and explanation |
| `GET /api/planning/section-count-runs/{id}/review/` | Review all remaining recommended course counts and blocking conflicts |
| `POST /api/planning/section-count-runs/{id}/approval-preview/` | Validate selected or adjusted counts without writing sections |
| `POST /api/planning/section-count-runs/{id}/approve/` | Approve selected counts and atomically create draft sections |
| `POST /api/planning/section-count-runs/{id}/reconciliation-preview/` | Compare selected counts with existing active sections and return the exact proposed delta |
| `POST /api/planning/section-count-runs/{id}/reconcile/` | Apply a previously previewed delta and record an immutable reconciliation audit |

## Counselor Upstream Planning Workflow

The recommended workflow separates the school's early section-budget decision
from the later staffing check:

1. Review year-specific course offerings, explicitly cancel low-demand courses,
   and optionally combine courses through an approved compatibility rule.
2. Run `/api/planning/section-budget-runs/` with an exact total or ceiling and a
   backup policy. Approval records the working budget but creates no sections.
3. Maintain `/api/teachers/`, review normalized teacher qualifications, record
   both semester capacities, and confirm `/api/planning/teacher-rosters/`.
4. Run `/api/planning/staffing-runs/` directly or link the budget approval. The
   engine proves qualified capacity without assigning named teachers.
5. Review and approve the staffing run. This final transaction creates
   unstaffed, unlocked physical `Section` records.

| Route | Purpose |
| --- | --- |
| `/api/planning/course-offerings/` | Review, cancel, or restore a course for one year |
| `/api/planning/combination-rules/` | Manage approved course-combination rules |
| `/api/planning/combination-suggestions/` | Review safe, non-writing merge suggestions |
| `POST /api/planning/combine-offerings/` | Combine approved offerings into one physical delivery |
| `/api/planning/section-budget-runs/` | Run and approve teacher-independent exact/ceiling budgets |
| `/api/teachers/` | Manage and archive/restore teachers |
| `/api/teachers/{id}/qualifications/` | Submit qualification evidence for review |
| `/api/planning/teacher-rosters/` | Set year membership and confirm staffing readiness |
| `/api/planning/staffing-runs/` | Prove qualified capacity and approve physical sections |

Unused alternate requests are not counted as ordinary demand. A run can promote
one only when a primary course was explicitly cancelled and the backup already
has an independently available offering. Every affected student and unresolved
gap remains visible in immutable run data.

A capacity profile contains positive `hard_min`, `soft_min`, `target`,
`soft_max`, and `hard_max` values, in that order. New courses receive the
shared **Standard Default** profile. A course can run in `semester_1_only`,
`semester_2_only`, or `either_semester`; the last is the default.

Create a planning run:

```json
{
  "academic_year": 1,
  "course_constraints": [
    {"course_id": 5, "exact_sections": 3}
  ],
  "teacher_capacity_adjustments": [
    {"teacher_id": 12, "semester": 1, "reduce_by": 1}
  ]
}
```

The optimizer first establishes a demand baseline, then a staffing-feasible
annual plan, then a Semester 1/2 split. It uses the existing normalized
qualification compiler only; Grade 11–12 eligibility is hard, while Grade 9–10
uses the established flexible rules. Priorities are explicit administrator
profiles: core graduation, pathway-critical, counselor-designated, and standard
elective. Objectives are optimized lexicographically, so higher-priority unmet
demand always beats class-size preferences.

Positive demand below `hard_min` receives one review-required provisional
section when it is legally staffable. The system does not merge or cancel that
course automatically. If no staffable section is possible, it reports unmet
demand rather than claiming a feasible plan. Unused teacher capacity is reported
as slack; the planner never invents sections merely to fill a workload maximum.

Runs snapshot all effective profiles, demand, qualification eligibility, and
capacity inputs. Structured diagnostics identify course-constraint failures,
qualification gaps, and staffing or semester-capacity shortfalls. Later
configuration changes affect only new runs.

Preview or approve selected course counts with:

```json
{
  "courses": [
    {"course_id": 5, "semester_1_count": 2, "semester_2_count": 1}
  ],
  "reason": "Approved after department review"
}
```

Omit `courses` to approve every recommendation from that run that has not
already been approved. Approval uses the course's current planning capacity
policy for the draft section's compatibility capacity fields. If that policy or
the course's semester restriction changed after the run, the preview reports
the change and current semester restrictions remain enforceable.

Every approval stores the approving user, timestamp, reason, recommended
counts, approved Semester 1/2 counts, and generated section ids. Generated
sections expose their source approval and planning-run ids. Approval is
transactional: either every selected draft section is created or none are.
Existing sections and previously approved courses produce `409 Conflict`; the
endpoint never replaces them implicitly.

### Revising an Existing Section Plan

Scheduling season is iterative. When a later immutable planning run recommends
different counts, use reconciliation instead of the original approval endpoint.
First post the selected Semester 1/2 counts to `reconciliation-preview`. The
response identifies every section that would be kept, moved, retired,
reactivated, or created and returns a `preview_token`. Preview does not write.

Apply exactly that reviewed state with:

```json
{
  "courses": [
    {"course_id": 5, "semester_1_count": 1, "semester_2_count": 2}
  ],
  "preview_token": "<64-character token returned by preview>",
  "reason": "Moved one section after the updated enrollment review"
}
```

Apply requires a nonblank reason. It returns `409 Conflict` if the token is
stale or if the requested count would displace a fixed section. A section is
fixed when it is manual or has a teacher, lock, placement, enrollment, or manual
override. Fixed sections count toward the target and are never silently moved
or retired.

Surplus dependency-free generated drafts are soft-retired, not deleted. The
normal `/api/sections/` list includes only active sections; authorized users can
inspect history with `?lifecycle_status=retired`. Retired sections are read-only
and excluded from scheduling-engine input. If demand returns, reconciliation
reactivates an eligible retired generated section before creating a new row.
Unchanged, moved, and reactivated rows keep their database identity. Historical
section numbers are never reused.

Every reconciliation creates a new immutable approval linked to the newer run,
plus immutable before/after actions for each affected section. Newly created
sections point to that new approval. Reused sections retain their original
creation provenance and gain reconciliation-action history.

## Constraint and Lock API

Counselors, staff, and directors manage shared scheduling constraints. Teachers
may manage only their own qualifications, preferences, current courses, and
availability through `/api/teachers/{id}/qualifications/`, `preferences/`,
`current-courses/`, and `availability/`.

Shared CRUD routes are `/api/qualifications/`, `/api/constraints/hard/`,
`/api/constraints/soft/`, `/api/constraints/preferences/`,
`/api/course-conflicts/`, `/api/course-room-requirements/`, and
`/api/course-qualification-requirements/`. Query parameters narrow only the
role-safe records returned by each list endpoint.

Use `GET` or `PATCH /api/sections/{id}/lock/` to inspect or set a section's
locked teacher, timeslot, and room. The first PATCH creates the lock. A locked
teacher for a Grade 11 or 12 course must hold every required Senior teachable
qualification; fields can be cleared individually with `null`. Grade 7-10
qualification mappings are preferences and never block an assignment or lock.

### Teacher Qualification Data

Qualifications are normalized catalog records, not copied academic-degree text.
For example, use `mathematics-senior` / `Mathematics - Senior` for a senior
mathematics teachable. A credential spanning Intermediate and Senior is stored
as two teacher-qualification records, one for each normalized catalog record.
The individual teacher record preserves its Aspen source text, source id, and
award-date text for auditability.

Create a Senior Mathematics qualification:

```json
{
  "code": "mathematics-senior",
  "name": "Mathematics - Senior",
  "kind": "teachable",
  "subject_code": "mathematics",
  "division": "senior"
}
```

Map it as legally required for a Grade 11 or 12 course with
`POST /api/course-qualification-requirements/` and
`{"course": 5, "qualification": 3, "enforcement": "required"}`. Grade
7-10 mappings must instead use `"enforcement": "preferred"`.

## Scheduling Engine Foundation

`scheduling_engine/` is a Django-independent package for demand analysis,
section-count recommendations, and solver-ready constraint compilation. It does
not access the ORM or persist recommendations. Run its isolated tests with:

```bash
python -m pytest -c scheduling_engine/pytest.ini scheduling_engine/tests
```

## Four-Day Timetable Rotation

Each semester uses four recurring timetable blocks: `A`, `B`, `C`, and `D`.
A `TimeSlot` represents one block, not a single calendar-day occurrence. The
fixed rotation is defined in `backend/apps/common/school_values.py`:

| Block | Day 1 | Day 2 | Day 3 | Day 4 |
| --- | --- | --- | --- | --- |
| A | Period 1 | Period 3 | Period 2 | Period 4 |
| B | Period 2 | Period 4 | Period 1 | Period 3 |
| C | Period 3 | Period 1 | Period 4 | Period 2 |
| D | Period 4 | Period 2 | Period 3 | Period 1 |

## Shared Domain Constants

Authoritative reusable constants live in the owning domain modules:

- `backend/apps/common/school_values.py` for school-wide values such as grade
  levels, room types, semesters, A-D blocks, and rotation positions;
- `backend/apps/people/constants.py` for application role values;
- `backend/apps/courses/constants.py` for course, request, offering, and
  delivery-group values;
- `backend/apps/constraints/constants.py` for qualification and constraint
  values;
- `backend/apps/scheduling/constants.py` for planning, roster, backup, and
  section-lifecycle values.

`backend/apps/common/constants.py` is a compatibility export for older imports.
Add or change a supported option in the domain module that owns it; Django
models, services, API tests, and the adapter should import that named constant
rather than defining their own copy.

## Semester and A-D Placement API

Placement is a counselor-reviewed timing stage. It chooses semesters when an
approved budget supplies annual totals, places sections in recurring A-D blocks,
and proves anonymous staffing feasibility. It does not assign rooms, named
teachers, or students.

- `POST /api/planning/course-conflict-matrices/` creates the one annual
  primary-request conflict matrix. Use `GET /{id}/grid/`, `POST /{id}/refresh/`,
  and `POST /{id}/conflicts/{conflict_id}/adjust/` to inspect, refresh, or
  reason-track a counselor score override.
- `GET/POST /api/planning/annual-placement-locks/` and
  `GET/PATCH/DELETE /api/planning/annual-placement-locks/{id}/` manage locks for
  stable annual virtual sections before real Section rows exist.
- `POST /api/planning/section-placement-runs/` accepts either
  `{"academic_year": 1, "input_mode": "fixed_semester"}` or
  `{"academic_year": 1, "input_mode": "annual_total", "budget_approval": 14}`.
- Use `GET /api/planning/section-placement-runs/{id}/review/`, then
  `POST /approval-preview/` or `POST /approve/` with a nonblank `reason`.
  Approval materializes annual virtual slots if needed and writes only a
  timeslot to `SectionSchedule`; it never changes an unreviewed candidate.

Counselor/director users may create and approve runs. Counselor/staff/director
users may list, inspect, review, and preview them. A run is rejected at approval
if the matrix, roster, lock, qualification, capacity, schedule, or other input
facts changed after the reviewed snapshot.

## Named Teacher Assignment API

Named teacher assignment uses already accepted semester/A-D section timing. It
assigns no rooms and no students. Configure annual capacity, hard course bounds,
and soft time preferences with:

- `GET/POST /api/planning/teacher-annual-capacities/`
- `GET/POST /api/planning/teacher-course-assignment-rules/`
- `GET/POST /api/planning/teacher-time-preferences/`

Create a reviewed candidate with `POST /api/planning/teacher-assignment-runs/`
and `{"academic_year": 1}`. Use `GET /{id}/review/`, then
`POST /approval-preview/` or `POST /approve/` with a nonblank reason. Only a
complete, unchanged run is approvable; approval writes `Section.teacher` and
immutable assignment provenance, never a room or enrollment.

## Local Schema Rebuild

Project apps intentionally do not use migration files. After pulling model
changes, recreate the local development database through pgAdmin or PostgreSQL,
then synchronize the schema:

```powershell
.\.venv\Scripts\python.exe backend\manage.py migrate --run-syncdb
```
