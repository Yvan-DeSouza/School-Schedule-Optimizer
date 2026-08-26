"""Small, Django-free runtime helpers for bounded diagnostic solves."""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import json
import os
import threading
from time import monotonic, monotonic as _resource_monotonic


@dataclass(frozen=True)
class MonotonicDeadline:
    """A single wall-clock budget shared by nested diagnostic operations.

    CP-SAT receives a derived remaining allowance for each solve.  Keeping the
    deadline outside the solver prevents nested probes and validation solves
    from accidentally receiving the full experiment allowance independently.
    The class does not interrupt native solver calls; it makes their requested
    limits and the surrounding operation timing explicit.
    """

    requested_seconds: float
    started_at: float = field(default_factory=monotonic)

    @classmethod
    def start(cls, requested_seconds):
        return cls(max(0.0, float(requested_seconds)))

    @property
    def deadline_at(self):
        return self.started_at + self.requested_seconds

    def elapsed(self):
        return max(0.0, monotonic() - self.started_at)

    def remaining(self):
        return max(0.0, self.deadline_at - monotonic())

    def allowance(self, requested_seconds=None):
        """Return the smaller of a child allowance and global remaining time."""

        remaining = self.remaining()
        if requested_seconds is None:
            return remaining
        return max(0.0, min(remaining, float(requested_seconds)))


class OperationTimer:
    """Accumulate externally measured wall time by named operation."""

    def __init__(self):
        self._values = {}

    def measure(self, name):
        timer = self

        class _Measurement:
            def __enter__(self):
                self.started_at = monotonic()
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                timer._values[name] = timer._values.get(name, 0.0) + (
                    monotonic() - self.started_at
                )
                return False

        return _Measurement()

    def snapshot(self):
        return dict(sorted(self._values.items()))


def _unavailable_resource_snapshot(source="unavailable"):
    """Return a JSON-safe snapshot when a host metric cannot be read."""

    return {
        "available": False,
        "source": source,
        "pid": os.getpid(),
        "working_set_bytes": None,
        "peak_working_set_bytes": None,
        "uss_bytes": None,
        "vms_bytes": None,
        "pagefile_bytes": None,
        "peak_pagefile_bytes": None,
        "cpu_user_seconds": None,
        "cpu_system_seconds": None,
        "process_cpu_percent": None,
        "thread_count": None,
        "child_process_count": None,
        "child_working_set_bytes": None,
        "child_uss_bytes": None,
        "tree_working_set_bytes": None,
        "tree_uss_bytes": None,
        "system_total_memory_bytes": None,
        "system_available_memory_bytes": None,
        "system_memory_percent": None,
        "logical_cpu_count": None,
    }


def _windows_process_memory_fallback():
    """Preserve the pre-psutil Windows working-set fallback."""

    if os.name != "nt":
        return None
    try:
        import ctypes

        class _ProcessMemoryCounters(ctypes.Structure):
            _fields_ = (
                ("cb", ctypes.c_ulong),
                ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            )

        counters = _ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        process_api = ctypes.windll.kernel32.GetCurrentProcess
        process_api.restype = ctypes.c_void_p
        memory_api = ctypes.windll.psapi.GetProcessMemoryInfo
        memory_api.argtypes = (
            ctypes.c_void_p,
            ctypes.POINTER(_ProcessMemoryCounters),
            ctypes.c_ulong,
        )
        memory_api.restype = ctypes.c_int
        if memory_api(process_api(), ctypes.byref(counters), counters.cb):
            snapshot = _unavailable_resource_snapshot(
                "windows_process_memory_counters"
            )
            snapshot.update({
                "available": True,
                "working_set_bytes": int(counters.WorkingSetSize),
                "peak_working_set_bytes": int(counters.PeakWorkingSetSize),
                "pagefile_bytes": int(counters.PagefileUsage),
                "peak_pagefile_bytes": int(counters.PeakPagefileUsage),
            })
            return snapshot
    except Exception:
        return None
    return None


def _process_memory_snapshot():
    """Read one resource snapshot, preferring psutil with safe fallbacks.

    This function is deliberately best-effort.  It is called from a daemon
    monitor thread and must never turn an unavailable or disappearing process
    metric into a scheduling failure.
    """

    try:
        import psutil

        process = psutil.Process(os.getpid())
        memory = process.memory_info()
        try:
            full_memory = process.memory_full_info()
        except (AttributeError, psutil.Error, OSError):
            full_memory = None
        try:
            children = process.children(recursive=True)
        except (psutil.Error, OSError):
            children = []
        child_working_set = 0
        child_uss = 0
        live_children = 0
        for child in children:
            try:
                child_memory = child.memory_info()
                child_working_set += int(getattr(child_memory, "rss", 0) or 0)
                if full_memory is not None:
                    child_full = child.memory_full_info()
                    child_uss += int(getattr(child_full, "uss", 0) or 0)
                live_children += 1
            except (psutil.Error, OSError):
                continue
        try:
            cpu_times = process.cpu_times()
            cpu_user = float(getattr(cpu_times, "user", 0.0))
            cpu_system = float(getattr(cpu_times, "system", 0.0))
        except (psutil.Error, OSError):
            cpu_user = cpu_system = None
        try:
            cpu_percent = float(process.cpu_percent(interval=None))
        except (psutil.Error, OSError):
            cpu_percent = None
        try:
            system_memory = psutil.virtual_memory()
            system_total = int(system_memory.total)
            system_available = int(system_memory.available)
            system_percent = float(system_memory.percent)
        except (psutil.Error, OSError, AttributeError):
            system_total = system_available = system_percent = None
        return {
            "available": True,
            "source": "psutil",
            "pid": os.getpid(),
            "working_set_bytes": int(memory.rss),
            "peak_working_set_bytes": int(memory.rss),
            "uss_bytes": (
                int(getattr(full_memory, "uss"))
                if full_memory is not None and getattr(full_memory, "uss", None) is not None
                else None
            ),
            "vms_bytes": int(memory.vms),
            "pagefile_bytes": int(memory.vms),
            "peak_pagefile_bytes": int(memory.vms),
            "cpu_user_seconds": cpu_user,
            "cpu_system_seconds": cpu_system,
            "process_cpu_percent": cpu_percent,
            "thread_count": int(process.num_threads()),
            "child_process_count": live_children,
            "child_working_set_bytes": child_working_set,
            "child_uss_bytes": child_uss if full_memory is not None else None,
            "tree_working_set_bytes": int(memory.rss) + child_working_set,
            "tree_uss_bytes": (
                int(getattr(full_memory, "uss", 0) or 0) + child_uss
                if full_memory is not None else None
            ),
            "system_total_memory_bytes": system_total,
            "system_available_memory_bytes": system_available,
            "system_memory_percent": system_percent,
            "logical_cpu_count": psutil.cpu_count(logical=True),
        }
    except Exception:
        native_fallback = _windows_process_memory_fallback()
        if native_fallback is not None:
            return native_fallback

    try:
        import resource

        peak = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        if os.name != "nt" and getattr(os, "uname", lambda: None)().sysname == "Linux":
            peak *= 1024
        snapshot = _unavailable_resource_snapshot("resource_maxrss")
        snapshot.update({
            "available": True,
            "peak_working_set_bytes": peak,
        })
        return snapshot
    except (AttributeError, ImportError, OSError, ValueError):
        return _unavailable_resource_snapshot()


class ProcessResourceMonitor:
    """Best-effort process/system observability for diagnostic operations.

    The monitor samples at a bounded interval and returns only compact
    aggregates.  It is intentionally non-authoritative: psutil errors,
    unsupported metrics, and disappearing child processes become ``None`` or
    zero-valued facts and never affect CP-SAT control flow.
    """

    def __init__(self, interval_seconds=0.25, *, enabled=True):
        self.interval_seconds = max(0.05, float(interval_seconds))
        self.enabled = bool(enabled)
        self._stop_event = threading.Event()
        self._thread = None
        self._samples = []
        self._started_at = None

    def sample(self):
        if not self.enabled:
            return {"available": False, "source": "disabled", "sample_count": 0}
        try:
            snapshot = _process_memory_snapshot()
        except Exception:  # pragma: no cover - defensive observability boundary
            snapshot = _unavailable_resource_snapshot("monitor_error")
        snapshot["sampled_at_monotonic"] = _resource_monotonic()
        self._samples.append(snapshot)
        return dict(snapshot)

    def _sample_loop(self):
        while not self._stop_event.wait(self.interval_seconds):
            self.sample()

    def start(self):
        if not self.enabled:
            return self
        if self._stop_event.is_set():
            self._stop_event = threading.Event()
            self._samples = []
        self._started_at = _resource_monotonic()
        self.sample()
        self._thread = threading.Thread(
            target=self._sample_loop,
            name="student-assignment-resource-monitor",
            daemon=True,
        )
        self._thread.start()
        return self

    def stop(self):
        if not self.enabled:
            return {
                "available": False,
                "source": "disabled",
                "sample_count": 0,
                "elapsed_seconds": 0.0,
            }
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self.interval_seconds * 4))
        self.sample()
        samples = tuple(self._samples)
        first = samples[0] if samples else _unavailable_resource_snapshot()
        last = samples[-1] if samples else first

        def values(key):
            return [item[key] for item in samples if item.get(key) is not None]

        working_set = values("working_set_bytes")
        uss = values("uss_bytes")
        tree_working_set = values("tree_working_set_bytes")
        tree_uss = values("tree_uss_bytes")
        cpu_percent = values("process_cpu_percent")
        peak_working_set = values("peak_working_set_bytes") + working_set
        peak_pagefile = values("peak_pagefile_bytes")
        peak_timestamp = None
        if peak_working_set:
            peak_value = max(peak_working_set)
            for sample in samples:
                if sample.get("working_set_bytes") == peak_value:
                    peak_timestamp = sample.get("sampled_at_monotonic")
                    break
        report = {
            "available": any(item.get("available") for item in samples),
            "source": last.get("source", first.get("source")),
            "pid": os.getpid(),
            "sample_count": len(samples),
            "elapsed_seconds": max(
                0.0,
                _resource_monotonic()
                - (self._started_at or _resource_monotonic()),
            ),
            "starting_working_set_bytes": first.get("working_set_bytes"),
            "representative_working_set_bytes": max(working_set, default=None),
            "peak_working_set_bytes": max(peak_working_set, default=None),
            "peak_working_set_timestamp_monotonic": peak_timestamp,
            "ending_working_set_bytes": last.get("working_set_bytes"),
            "starting_uss_bytes": first.get("uss_bytes"),
            "peak_uss_bytes": max(uss, default=None),
            "ending_uss_bytes": last.get("uss_bytes"),
            "peak_tree_working_set_bytes": max(tree_working_set, default=None),
            "peak_tree_uss_bytes": max(tree_uss, default=None),
            "starting_pagefile_bytes": first.get("pagefile_bytes"),
            "ending_pagefile_bytes": last.get("pagefile_bytes"),
            "peak_pagefile_bytes": max(peak_pagefile, default=None),
            "starting_cpu_user_seconds": first.get("cpu_user_seconds"),
            "ending_cpu_user_seconds": last.get("cpu_user_seconds"),
            "starting_cpu_system_seconds": first.get("cpu_system_seconds"),
            "ending_cpu_system_seconds": last.get("cpu_system_seconds"),
            "representative_process_cpu_percent": max(cpu_percent, default=None),
            "peak_thread_count": max(values("thread_count"), default=None),
            "peak_child_process_count": max(values("child_process_count"), default=0),
            "system_total_memory_bytes": last.get("system_total_memory_bytes"),
            "system_available_memory_bytes": last.get("system_available_memory_bytes"),
            "system_memory_percent": last.get("system_memory_percent"),
            "logical_cpu_count": last.get("logical_cpu_count"),
        }
        return report


class ProcessMemoryMonitor(ProcessResourceMonitor):
    """Backward-compatible name for the richer resource monitor."""


def semantic_student_assignment_input_fingerprint(
    data,
    *,
    include_extended_facts=True,
):
    """Return a diagnostic fingerprint that omits opaque database identities.

    The detached student-assignment DTO does not carry catalog codes or
    natural student numbers.  This fingerprint therefore canonicalizes the
    identifier namespaces by sorted semantic occurrence and records the
    scheduling facts that those identifiers connect.  It is intended to
    compare equivalent fixture builds, not to replace the immutable run
    snapshot or to identify persisted records.
    """

    def rank(values):
        return {value: index for index, value in enumerate(sorted(set(values)))}

    student_rank = rank(
        [item.student_id for item in data.requests]
        + [item.student_id for item in data.fixed_enrollments]
        + [item.student_id for item in data.schedule_commitment_requests]
        + [item.student_id for item in data.fixed_schedule_commitments]
        + list(data.student_ids_with_alternate_requests)
        + [student_id for student_id, _grade_level in data.student_grades]
    )
    course_values = (
        [item.course_id for item in data.requests]
        + [course_id for item in data.sections for course_id in item.member_course_ids]
        + [item.course_id for item in data.course_difficulties]
        + [item.course_id for item in data.fixed_enrollments]
        + [item.course_id for item in data.fixed_schedule_commitments if item.course_id is not None]
    )
    course_rank = rank(course_values)
    offering_rank = rank(
        [item.course_offering_id for item in data.requests]
        + [offering_id for item in data.sections for offering_id in item.member_course_offering_ids]
        + [item.course_offering_id for item in data.fixed_enrollments]
        + [item.course_offering_id for item in data.fixed_schedule_commitments if item.course_offering_id is not None]
    )
    teacher_rank = rank(
        [item.teacher_id for item in data.sections if item.teacher_id is not None]
        + [item.supervisor_id for item in data.online_supervision_sessions if item.supervisor_id is not None]
        + [item.teacher_id for item in data.student_assignment_locks if item.teacher_id is not None]
    )
    timeslot_rank = {
        item.id: index
        for index, item in enumerate(
            sorted(
                data.timeslots,
                key=lambda item: (item.semester, item.block, item.is_available, item.id),
            )
        )
    }
    section_records = [
        (
            tuple(sorted(offering_rank[value] for value in item.member_course_offering_ids)),
            tuple(sorted(course_rank[value] for value in item.member_course_ids)),
            item.semester,
            timeslot_rank.get(item.timeslot_id),
            item.capacity_max,
            item.target_capacity,
            item.half_semester_segment,
            item.half_semester_pair_key,
            teacher_rank.get(item.teacher_id),
            item.delivery_group_id,
            item.section_id,
        )
        for item in data.sections
    ]
    delivery_group_rank = rank(item[9] for item in section_records)
    section_rank = {
        item[10]: index
        for index, item in enumerate(
            sorted(
                section_records,
                key=lambda item: (
                    repr(item[:9]),
                    delivery_group_rank[item[9]],
                    item[10],
                ),
            )
        )
    }
    request_records = [
        (
            student_rank[item.student_id],
            course_rank[item.course_id],
            offering_rank[item.course_offering_id],
            item.is_primary,
            item.is_mandatory,
            item.priority_tier,
            item.assignment_basis,
            item.delivery_kind,
            item.duration,
            item.credit_value,
            item.half_semester_segment,
            course_rank.get(item.paired_half_course_id),
        )
        for item in data.requests
    ]
    request_rank = {
        request.request_id: index
        for index, (request, _record) in enumerate(
            sorted(
                zip(data.requests, request_records),
                key=lambda pair: (pair[1], pair[0].request_id),
            )
        )
    }
    commitment_rank = {
        item.request_id: index
        for index, item in enumerate(
            sorted(
                data.schedule_commitment_requests,
                key=lambda item: (
                    student_rank[item.student_id],
                    item.commitment_type,
                    item.is_in_scope,
                    item.request_id,
                ),
            )
        )
    }

    payload = {
        "request_records": sorted(request_records),
        "section_records": [
            (
                tuple(sorted(offering_rank[value] for value in item.member_course_offering_ids)),
                tuple(sorted(course_rank[value] for value in item.member_course_ids)),
                item.semester,
                timeslot_rank.get(item.timeslot_id),
                item.capacity_max,
                item.target_capacity,
                item.half_semester_segment,
                item.half_semester_pair_key,
                teacher_rank.get(item.teacher_id),
                delivery_group_rank[item.delivery_group_id],
            )
            for item in data.sections
        ],
        "timeslots": sorted(
            (item.semester, item.block, item.is_available)
            for item in data.timeslots
        ),
        "online_supervision_sessions": sorted(
            (
                item.semester,
                timeslot_rank.get(item.timeslot_id),
                item.capacity_max,
                item.target_capacity,
                teacher_rank.get(item.supervisor_id),
                item.is_in_scope,
            )
            for item in data.online_supervision_sessions
        ),
        "ordinary_locks": sorted(
            (
                item.lock_type,
                student_rank.get(item.student_id),
                section_rank.get(item.section_id),
                course_rank.get(item.course_id),
                teacher_rank.get(item.teacher_id),
                tuple(sorted(student_rank.get(student_id) for student_id in item.member_student_ids)),
                item.is_active,
            )
            for item in data.student_assignment_locks
        ),
        "fixed_enrollments": sorted(
            (
                student_rank[item.student_id],
                section_rank.get(item.section_id),
                offering_rank[item.course_offering_id],
                course_rank[item.course_id],
                item.semester,
                timeslot_rank.get(item.timeslot_id),
                item.is_active,
                item.is_locked,
                item.is_historical,
                item.is_in_scope,
                item.half_semester_segment,
                item.credit_value,
                item.delivery_kind,
            )
            for item in data.fixed_enrollments
        ),
        "commitment_requests": sorted(
            (
                student_rank[item.student_id],
                item.commitment_type,
                item.is_in_scope,
            )
            for item in data.schedule_commitment_requests
        ),
        "special_locks": sorted(
            (
                item.lock_type,
                item.lock_mode,
                commitment_rank.get(item.schedule_commitment_request_id),
                request_rank.get(item.course_request_id),
                timeslot_rank.get(item.timeslot_id),
                item.semester,
            )
            for item in data.special_commitment_locks
        ),
        "fixed_commitments": sorted(
            (
                student_rank[item.student_id],
                item.commitment_kind,
                commitment_rank.get(item.schedule_commitment_request_id),
                request_rank.get(item.course_request_id),
                tuple(sorted((timeslot_rank.get(slot), segment) for slot, segment in item.occupancy)),
                item.is_active,
                item.is_locked,
                item.is_historical,
                item.is_in_scope,
            )
            for item in data.fixed_schedule_commitments
        ),
        "configuration": (
            data.section_utilization_balance_importance,
            data.student_semester_balance_importance,
            data.course_sequence_preferences_importance,
            data.difficulty_balance_importance,
            data.course_category_diversity_importance,
            data.schedule_preservation_level,
        ),
    }
    if data.student_grades:
        payload["student_grades"] = sorted(
            (student_rank[int(student_id)], int(grade_level))
            for student_id, grade_level in data.student_grades
        )
    if include_extended_facts:
        # Sequence edges are immutable run facts, not merely a global
        # importance setting.  Include the directed relation itself so an
        # edit to counselor sequencing configuration cannot pass approval
        # drift checks under an unchanged configuration label.
        payload.update({
            "hard_prerequisites": sorted(
                (
                    course_rank.get(item.course_id),
                    course_rank.get(item.prerequisite_id),
                )
                for item in data.hard_prerequisites
            ),
            "soft_sequence_preferences": sorted(
                (
                    course_rank.get(item.earlier_course_id),
                    course_rank.get(item.later_course_id),
                )
                for item in data.soft_sequence_preferences
            ),
            "objective_semantics": (
                data.objective_semantics_version,
                tuple(sorted(data.objective_importance_scores.items())),
            ),
        })
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return sha256(encoded.encode("utf-8")).hexdigest()
