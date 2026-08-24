# Scheduling Observability and Monitoring

This document defines the diagnostic observability contract for expensive
scheduling operations. It describes measurements and their authority boundary;
it does not change scheduling constraints, objectives, solver status, or
approval semantics.

## Purpose

Large CP-SAT runs need enough operational evidence to distinguish model build
cost, solver cost, memory pressure, process-tree behavior, and ordinary
run-to-run variation. The measurements are intended for bounded offline
experiments, production worker operations, and post-run review of resource
behavior.

## Authority boundary

CP-SAT and the existing immutable stage result remain authoritative for
feasibility, assignments, objective values, diagnostics, and approval.
Observability is descriptive only. A missing, stale, unsupported, or failed
metric must never change a solver branch, turn `UNKNOWN` into another status,
or make an otherwise valid result unapprovable.

## Ownership and implementation

The pure engine owns the Django-free `ProcessResourceMonitor` abstraction in
`scheduling_engine/student_assignment/runtime.py`. It is used by diagnostic
student-assignment paths and can be used by future engine diagnostics. Django
services remain responsible for persistence and operational run records;
engine telemetry is returned as result facts through the existing DTO/result
boundary rather than writing to the database from the engine.

`psutil` is the primary cross-platform provider. A small standard-library
fallback remains available for peak memory on hosts where psutil cannot be
imported. The fallback is intentionally partial and reports unsupported values
as `null`.

## Metric meanings

Process metrics may include:

- RSS/working set: resident process memory at a sample;
- USS/private memory: memory attributed privately to the process where the
  operating system exposes it;
- VMS/pagefile: virtual address-space or platform-equivalent committed size;
- CPU user/system seconds and sampled process CPU percent;
- thread count and child-process count;
- process-tree RSS/USS, including safely readable recursive children.

System metrics may include total and available physical memory, system memory
utilization, and logical CPU count. They describe the host at the sample time,
not a reserved capacity guarantee.

The monitor returns compact aggregates: start, representative/maximum, end,
peak timestamp when available, elapsed time, and sample count. Raw samples are
not persisted by the engine result.

## Sampling and failure behavior

Sampling is bounded by a configurable interval and runs on a daemon thread.
The default interval is deliberately low-frequency for long solver calls.
Child processes may disappear between enumeration and measurement; those
children are skipped. Permission errors, unsupported USS metrics, psutil
exceptions, and provider import failures are represented by unavailable or
`null` fields. The monitor is safe to start/stop repeatedly and can be
disabled for A/B semantic checks.

Resource reports must remain JSON serializable and must not contain raw
student/teacher PII. Identifiers such as the current PID are operational
metadata only and should be treated as sensitive deployment information.

## Operation versus local phase

Student assignment keeps two intentionally distinct views:

- the operation report covers model preparation, Stage 1, Stage 2, extraction,
  quality facts, and lock-cost work where enabled;
- local diagnostic iterations may include their own memory report so a
  neighborhood probe can be compared with the surrounding operation.

The local report must not be mistaken for total process cost. A future phase
instrumentation change should preserve this distinction.

The mature-R2 diagnostic also records bounded phase timings for benchmark
loading, checkpoint verification/materialization, model construction, mature
seed validation, neighborhood setup, CP-SAT Solve, candidate validation,
quality/review finalization, and ordinary Stage 2 pass facts when ordinary
Stage 2 is intentionally invoked. The mature-local-only diagnostic path does
not invoke ordinary Stage 2 passes. These timings are compact diagnostic
facts; they are not persisted as scheduling decisions.

On the current Windows target-scale host, the resource monitor samples every
`0.25` seconds (with a minimum interval of `0.05` seconds). A representative
mature-local profile collected `749` whole-operation samples over roughly
`310` seconds and `603` local-phase samples over roughly `257` seconds. The
profile's peak process-tree working set was approximately `3.12 GB`; the
monitor lifetime is reported, but psutil call CPU overhead is not inferred
from that lifetime. Resource sampling remains observational and must be
measured on/off before being treated as a performance cost.

During mature-local descent, the operation monitor and the local diagnostic
monitor may sample during overlapping intervals. This is intentional current
telemetry: the operation report describes the whole engine call, while the
local report isolates neighborhood-search memory. The duplicate sampling is
observational only and does not control CP-SAT, candidate adoption, or
stopping. It remains a measured follow-up opportunity rather than a reason to
remove either report without an equivalent phase-labelled replacement.

An isolated Windows micro-measurement called the same snapshot routine `120`
times in `2.761` seconds (`23.0` ms per snapshot). Calling it through the
monitor's manual sampling method took `3.801` seconds (`31.7` ms per sample),
and starting/stopping the daemon monitor itself took `0.073` seconds. A paired
medium solver run with telemetry disabled/enabled measured `34.83`/`32.55`
seconds, respectively; because CP-SAT took a different search path, that pair
does not establish a solver-scale on/off speedup. The direct micro-measurement
does establish that sampling is measurable per call but is not evidence that it
explains the historical hundreds-of-seconds remainder.

The two long target-scale mature sessions collected overlapping whole-operation
and local-phase samples without affecting solver control. The `65,133` session
recorded `5,803` whole-operation and `5,582` local samples, with a peak process
tree working set of approximately `3.20 GiB` and peak pagefile usage of about
`3.74 GiB`. The `65,077` continuation recorded `7,804` whole-operation and
`7,764` local samples, with a peak process tree working set of approximately
`3.15 GiB` and peak pagefile usage of about `3.72 GiB`. These measurements
confirm resource-safe completion on this host, but do not establish that
overlapping sampling is free; a future phase-labelled or tiered monitor should
be benchmarked before replacing the current reports.

## Worker and process-tree expectations

The Celery scheduling worker remains process-based with one heavy task per
child by default. CP-SAT worker count is an engine configuration and is not
the same as Celery concurrency. Process-tree metrics help validate that
native solver child processes and worker recycling do not create unexpected
resident-memory accumulation; they do not authorize increasing concurrency.

## Testing requirements

Tests assert lifecycle behavior, JSON compatibility, nonnegative numeric
fields, safe unavailable metrics, repeated start/stop, disappearing children,
and monitoring on/off semantic equivalence. Tests must not assert exact RAM,
CPU, or process counts because those are host-dependent.

## Performance and privacy

Monitoring is intentionally sampled rather than event-streamed. It should
remain a small fraction of a multi-minute solve, and compact reports should be
preferred over raw telemetry. Do not add command lines, environment secrets,
student schedules, or teacher identities to resource payloads.

## Non-goals and future work

This contract does not provide cancellation, progress percentages, queue
dashboards, automatic remediation, or a counselor-facing explanation. Future
worker integration may attach compact resource summaries to operational
execution records after an explicit persistence/API decision; that work must
not make monitoring authoritative over scheduling.

Contributors adding a new metric should document its platform meaning,
unavailability behavior, sampling cost, and JSON shape, then add a focused
failure-safe test before using it in a benchmark or production report.
