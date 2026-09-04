# Scheduling Workers

This is the canonical operational owner for the Celery scheduling-worker
boundary and queue behavior. Resource metrics are owned by the linked
observability document.

This document describes the asynchronous scheduling execution architecture
implemented in the repository. It is intentionally operational documentation,
not an aspirational deployment diagram.

Resource measurement for long-running worker and diagnostic operations is
defined in [Observability and Monitoring](OBSERVABILITY_AND_MONITORING.md).
That document is the canonical source for psutil-backed process/system
telemetry, fallback behavior, sampling, and the rule that measurements never
override CP-SAT or immutable run results.

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

## Research-only parallel policy trials

The offline policy-study runner is a separate process arrangement and must not
be confused with production Celery concurrency:

| Layer | Meaning in the research study |
| --- | --- |
| Celery concurrency | Remains `1` for production scheduling tasks. It is not changed by this study. |
| Research trial slots | The number of independently supervised policy processes launched by the study coordinator. |
| CP-SAT workers per trial | Exactly `1`, so a trial does not internally multiply CPU usage. |

The four-cell comparison consists of `adaptive_balanced`,
`adaptive_student_pressure_biased`, `adaptive_utilization_biased`, and the
existing `fixed_cycle` control. It is diagnostic-only, uses the detached v2
benchmark inputs and complete source-seed fingerprints, and never writes
canonical checkpoints, Django rows, approvals, or production policy settings.
The two biased names change only adaptive role allocation; all four cells use
the same balanced Objective Semantics v2 profile, operator portfolio, hard
constraints, full-model validation, and bounded trial budgets.

The coordinator starts with two independent one-worker supervised trials. It
may qualify a third and then a fourth slot only after a completed batch has
provided measured evidence. One additional slot is permitted only when the
lowest observed available memory, less the largest observed per-trial process
tree RSS, leaves at least `2 GiB`, and the batch also proves clean child
cleanup, no resource-guard/deadline termination, no abnormal pagefile growth,
and no material CPU contention. If those facts are not available or the gate
fails, the coordinator remains at the previous safe concurrency. It never
silently starts above two without a recorded qualification decision.

Each result has a unique scenario/policy/seed path. Trial execution is
performed by supervised child processes; only the parent coordinator publishes
result JSON and updates the study manifest. This parent-owned write rule is
required because the historical single-cell runner appends a shared manifest
and is not safe to call concurrently.

For a multi-cell policy batch, the coordinator first prepares each distinct
scenario/source/profile cohort once. That preparation materializes the
detached branch and runs the authoritative full-model validation exactly once
for that cohort, then records the input, source, profile, validation facts, and
prepared-context fingerprint. Every child receives the same read-only branch
and verifies those fingerprints before policy execution. Candidate validation
inside each child is never skipped or shared: each newly proposed candidate
still must pass the existing full-model authority boundary before adoption.
If the coordinator process restarts, its in-memory prepared contexts are lost
and the source incumbent is validated again; a persisted validation flag alone
is not trusted as proof.

From the repository root, a reproducible research run is:

```bash
python -m scheduling_engine.benchmark_policy_generalization \
  --study-directory scheduling_engine/benchmarks/student_assignment/parallel_policy_study \
  --initialize-parallel
python -m scheduling_engine.benchmark_policy_generalization \
  --study-directory scheduling_engine/benchmarks/student_assignment/parallel_policy_study \
  --run-parallel --max-parallel-trials 4
python -m scheduling_engine.benchmark_policy_generalization \
  --study-directory scheduling_engine/benchmarks/student_assignment/parallel_policy_study \
  --summarize
```

The first command creates the immutable study manifest. The second runs the
24-cell matrix in progressively qualified batches; `4` is a ceiling, not an
instruction to launch four processes immediately. The summary command only
verifies artifacts and reports results. Parallel wall time is throughput
evidence, not a fair per-policy runtime ranking; repeatable sequential
confirmation is required before comparing policy speed or considering any
policy for promotion.

For the evidence-guided cohort, sequential confirmation is available through
the same parent-owned study boundary:

```bash
python -m scheduling_engine.benchmark_policy_generalization \
  --study-directory C:/research_runs/adaptive-validation-hardening-YYYYMMDD \
  --initialize-evidence-guided
python -m scheduling_engine.benchmark_policy_generalization \
  --study-directory C:/research_runs/adaptive-validation-hardening-YYYYMMDD \
  --run-sequential --scenario reference_target --seed 101
python -m scheduling_engine.benchmark_policy_generalization \
  --study-directory C:/research_runs/adaptive-validation-hardening-YYYYMMDD \
  --summarize
```

The sequential command runs the selected pending cells one at a time. It
prepares and validates one source context per scenario for the selected
cohort, passes that read-only context to each supervised child, and lets only
the parent write result artifacts and manifest hashes. `--policy` may be
provided to run one policy; omitting it runs every pending policy for the
selected scenario and seed. This mode is research-only and does not alter
production Celery concurrency, ordinary scheduling, canonical benchmark
state, Django rows, or approvals.

The student-assignment diagnostic paths expose compact operation and local
probe resource facts when measurement is enabled. These facts can include RSS,
USS/private memory where supported, VMS, CPU, thread/process-tree, and host
memory context. They are intended to validate worker capacity and process
recycling; they are not progress signals, solver decisions, or approval facts.

For evidence-guided sequential policy cohorts, the research coordinator may
select several policies for one scenario/seed invocation with `--policies`.
The coordinator prepares and fully validates one detached source context for
that exact cohort, then runs one supervised child per policy in sequence. The
parent alone publishes result files and manifest hashes; candidate validation
still occurs independently inside every child. A source-validation `UNKNOWN`
or transient validation error receives at most one fresh retry. Structural
fingerprint, completeness, unmet-request, or materialization failures gate the
whole cohort and are recorded as source-validation failures rather than policy
failures.

This is research-only behavior. It does not change production Celery
concurrency, ordinary scheduling persistence, approvals, canonical benchmark
state, or the candidate-authority rule. Sequential child runtimes are the
appropriate facts for policy-speed comparison; parallel study slots are only
throughput/resource experiments.

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
