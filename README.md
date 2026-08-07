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

### Verify The Data Layer

Run these commands from the repository root:

```bash
python backend/manage.py check
python backend/manage.py migrate
pytest
```

Expected result:

- Django system checks report no issues.
- Migrations apply cleanly for the domain apps.
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

The command creates these local accounts, all with password `password123`:

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
  -d "{\"username\":\"student\",\"password\":\"password123\"}"
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
teacher must hold every qualification required by that section's course; fields
can be cleared individually with `null`.

## Scheduling Engine Foundation

`scheduling_engine/` is a Django-independent package for demand analysis,
section-count recommendations, and solver-ready constraint compilation. It does
not access the ORM or persist recommendations. Run its isolated tests with:

```bash
python -m pytest -c scheduling_engine/pytest.ini scheduling_engine/tests
```
