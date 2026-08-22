# Scheduling Workers

This document describes the asynchronous scheduling execution architecture
implemented in the repository. It is intentionally operational documentation,
not an aspirational deployment diagram.

## Why scheduling runs in a worker

Placement, named-teacher assignment, and student assignment are offline CP-SAT
operations. The representative 1,400-student validation workload can occupy a
process for tens of minutes. Earlier production-scale testing also showed that
large OR-Tools executions can retain substantial native memory in a reused
Python process. Two independent runs completed successfully in clean
processes, while a repeated same-process test later returned `UNKNOWN` under
resource pressure.

The HTTP request therefore persists a small execution record and enqueues work
instead of holding a request open. The worker executes the existing reviewed
service and creates the same immutable run/result as a synchronous caller.

## API-to-engine execution path

Before this worker boundary, each expensive create endpoint called its
stage-specific Django service directly in the HTTP process. The service then
loaded an immutable DTO snapshot through `engine_adapter`, called the pure
engine solver, and persisted the immutable stage run. Placement used
`create_section_placement_run`, named staffing used
`create_teacher_assignment_run`, and student assignment (including scoped
reruns) used `create_student_assignment_run`.

The current path preserves those service and engine boundaries:

```text
DRF create endpoint
  -> SchedulingExecution transaction
  -> on_commit Celery task with execution ID
  -> existing stage service
  -> engine adapter / immutable DTO snapshot
  -> CP-SAT engine
  -> immutable stage run
  -> execution result reference
  -> existing review and approval endpoints
```

The worker is orchestration only. It does not build solver models, assign
teachers, approve runs, or write operational scheduling state.

## Responsibilities

| Component | Responsibility |
| --- | --- |
| Django/DRF | Authentication, authorization, request validation, execution creation, status/result retrieval, review, and explicit approval. |
| PostgreSQL | Authoritative business data, queued/running/failed execution state, immutable solver runs, snapshots, approvals, and audit history. |
| Redis | Celery task transport only. It is not the source of scheduling truth. |
| Celery | Delivers expensive scheduling operations to the dedicated scheduling queue. |
| Scheduling worker | Loads one persisted execution payload and calls the existing Django service. |
| CP-SAT | Remains the scheduling authority. No heuristic or worker layer creates assignments. |

The pure `scheduling_engine` package remains Django-independent. Celery tasks
are backend orchestration and do not cross the engine boundary with ORM objects.

## Asynchronous operations

The following API create operations enqueue work and return HTTP `202` with a
`SchedulingExecution` identifier:

- `POST /api/planning/section-placement-runs/`
- `POST /api/planning/teacher-assignment-runs/`
- `POST /api/planning/student-assignment-runs/`

Student scoped reruns use the same student-assignment endpoint and therefore
use the same queue boundary. The payload contains stable IDs and request
settings only; it does not contain DTO graphs, solver models, or ORM objects.

The following remain synchronous because they are ordinary data/configuration,
review, or approval operations, or were not shown by the audit to require this
worker boundary:

- CRUD and configuration endpoints;
- run review, explanation, what-if, and approval endpoints;
- placement, teacher, and student approval write-backs;
- direct service calls used by tests and management code;
- section-count/staffing/online-supervision planning endpoints unless a future
  measured requirement moves them behind the same execution contract.

Celery executes a solve; it never approves a run.

## Execution lifecycle

`SchedulingExecution` is separate from the immutable stage-specific run:

```text
queued -> running -> completed
                  \-> failed
```

The execution record contains the operation, creator, stable JSON payload,
timestamps, Celery task ID, failure code/detail, and the eventual result model
and ID. The immutable `SectionPlacementRun`, `TeacherAssignmentRun`, or
`StudentAssignmentRun` remains the source of solver status, diagnostics,
snapshot, and review data.

Poll:

```text
GET /api/planning/executions/<execution-id>/
```

The response exposes both `status` (delivery state) and `solver_status` (the
stage result, when a run exists). Consequently:

- worker failure is not reported as solver infeasibility;
- `UNKNOWN` is not converted to `INFEASIBLE`;
- partial and infeasible solver results remain stage results that can be
  reviewed according to the existing approval rules;
- only a successful worker task with a persisted run is `completed`.

The execution task has no automatic retry. A worker or broker failure is
persisted as a failed execution when it can be observed by the task boundary;
re-initiation is deliberate because blindly retrying expensive scheduling can
create duplicate recommendations and misleading audit history.

## Transactions and enqueue timing

Execution creation is transactional. Celery publication is registered with
`transaction.on_commit`, so a worker cannot start against an execution row that
was rolled back or is not yet visible in PostgreSQL. The worker receives only
the committed execution ID and reloads its payload from PostgreSQL.

An optional `Idempotency-Key` header is supported on the three asynchronous
create endpoints. Repeating the same operation and payload with the same key
returns the original execution. Reusing that key with a changed payload is a
stable conflict rather than a second solve.

## Worker and queue configuration

The initial queue is deliberately conservative:

- queue: `scheduling` (configurable with `CELERY_SCHEDULING_QUEUE`);
- broker: `CELERY_BROKER_URL`, default `redis://localhost:6379/0`;
- Celery pool: process-based `prefork`;
- Celery concurrency: `1` heavy scheduling task at a time;
- prefetch multiplier: `1`;
- child recycling: `CELERY_WORKER_MAX_TASKS_PER_CHILD=1`;
- automatic task retries: disabled;
- Celery result backend: disabled; PostgreSQL execution/run rows are the
  result store.

Start the worker from a Linux/WSL environment with the repository on its
`PYTHONPATH`:

```bash
cd /mnt/c/Users/desou/OneDrive/Desktop/SchoolScheduleOptimizer
export DJANGO_SETTINGS_MODULE=config.settings
export PYTHONPATH=backend
export CELERY_BROKER_URL=redis://localhost:6379/0
python -m celery -A config.celery worker \
  --pool=prefork --concurrency=1 --max-tasks-per-child=1 \
  --prefetch-multiplier=1 -Q scheduling --loglevel=INFO
```

`CELERY_WORKER_MAX_TASKS_PER_CHILD=1` is the clean-process guarantee: after a
heavy task completes, Celery terminates that child and starts a replacement.
The operating system then reclaims CP-SAT native allocations associated with
the old child. Celery concurrency is not the same as CP-SAT worker count. A
single scheduling task may still use the established internal CP-SAT workers:

- placement: 4;
- named teacher assignment: 4;
- Stage 1 student feasibility: 8;
- Stage 2 student optimization: 8, with a configurable offline optimization
  horizon. The current 1,800-second value is the default historical benchmark,
  not a hard product timeout; longer horizons remain bounded configuration
  choices when quality measurements justify them.

Increasing Celery concurrency without a memory/CPU capacity review would run
multiple large CP-SAT models simultaneously and is intentionally not the
default.

## Windows and WSL development

Redis connectivity from the Windows environment was verified with the Python
Redis client and returned `True`. WSL Ubuntu is installed and `redis-cli ping`
returns `PONG`; a raw TCP check to `localhost:6379` also succeeds.

The supported process-recycling worker is Linux-compatible WSL/production
Celery prefork. A direct Windows prefork probe failed with a Billiard
`PermissionError`, so Windows should not be treated as the equivalent worker
runtime. WSL needs its own Linux Python environment with the dependencies from
`requirements.txt`; the Windows `.venv\Scripts\python.exe` is not that
environment.

The repository is accessible from WSL at `/mnt/c/Users/desou/OneDrive/Desktop/SchoolScheduleOptimizer`.
PostgreSQL must also be reachable from the WSL worker. If PostgreSQL is bound
only to Windows localhost, set `DB_HOST` to the Windows host address visible
from WSL and configure PostgreSQL/Windows firewall rules manually as required;
no password is stored or required by this repository change.

Start Django separately, for example:

```bash
python backend/manage.py runserver
```

Stop the worker with `Ctrl-C` in its terminal. Verify the worker and queue with
the worker startup log and Redis connectivity:

```bash
redis-cli ping
python -m celery -A config.celery inspect ping
```

The process-recycling behavior was directly verified in WSL with two tasks:
the recorded child PIDs were different (`2197` and `2199`) under
`--max-tasks-per-child=1`.

## Schema and local setup

`SchedulingExecution` is a new Django model and follows the repository's
migrationless policy. No migration file was generated. After a local schema
rebuild or synchronization, use:

```bash
python backend/manage.py migrate --run-syncdb
```

Do not run `makemigrations`. Pytest creates the test database from the current
models using the repository's `--no-migrations` convention.

## Known limitations and future work

- There is no automatic retry or stale-running execution reconciler yet.
- Queue monitoring, cancellation, progress percentages, and a frontend remain
  future work.
- Section-count, staffing-plan, and online-supervision-plan execution has not
  been moved to this queue because this increment is limited to the audited
  expensive downstream solves.
- Production deployment still needs a Linux service manager, least-privilege
  worker credentials, TLS/host configuration, structured task logging, and
  PostgreSQL operational tuning.
- Docker and container orchestration are intentionally deferred.
