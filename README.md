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
