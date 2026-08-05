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
