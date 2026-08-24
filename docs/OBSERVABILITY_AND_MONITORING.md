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
