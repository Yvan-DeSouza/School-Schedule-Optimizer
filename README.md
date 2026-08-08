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

The CP-SAT planning layer creates an immutable, read-only annual and
semester-level recommendation. It never creates `Section` records or assigns
named teachers, students, rooms, or timetable blocks. Counselors, staff, and
directors can manage its configuration and create runs; other roles have no
access.

| Route | Purpose |
| --- | --- |
| `/api/planning/capacity-profiles/` | CRUD shared and course-specific class-size policies |
| `/api/planning/course-priority-profiles/` | CRUD named four-tier demand priorities |
| `/api/planning/teacher-capacities/` | CRUD teacher/year/semester section capacity |
| `POST /api/courses/{id}/capacity-policy/` | Attach a shared policy or copy-on-write a custom policy |
| `POST /api/planning/section-count-runs/` | Create a frozen base plan or what-if scenario |
| `GET /api/planning/section-count-runs/` | List frozen planning runs |
| `GET /api/planning/section-count-runs/{id}/` | Retrieve one complete recommendation and explanation |

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
capacity inputs. Later configuration changes affect only new runs.

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
fixed rotation is defined in `backend/apps/common/constants.py`:

| Block | Day 1 | Day 2 | Day 3 | Day 4 |
| --- | --- | --- | --- | --- |
| A | Period 1 | Period 3 | Period 2 | Period 4 |
| B | Period 2 | Period 4 | Period 1 | Period 3 |
| C | Period 3 | Period 1 | Period 4 | Period 2 |
| D | Period 4 | Period 2 | Period 3 | Period 1 |

## Shared Domain Constants

`backend/apps/common/constants.py` is the single source of truth for reusable
selectable values: grade levels, room types, course categories, semesters,
course-request types, A-D blocks and rotation positions, and application role
values. It also defines qualification kinds, canonical teachable subjects,
divisions, enforcement levels, and source systems. Add or change a supported
option there; Django models, services, API tests, and the adapter import the
relevant named constant rather than defining their own copy.

## Local Schema Rebuild

Project apps intentionally do not use migration files. After pulling model
changes, recreate the local development database through pgAdmin or PostgreSQL,
then synchronize the schema:

```powershell
.\.venv\Scripts\python.exe backend\manage.py migrate --run-syncdb
```
