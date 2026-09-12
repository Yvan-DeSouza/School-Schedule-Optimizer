"""Explicit target-scale Paul-Desmarais production-pipeline qualification.

This file is intentionally outside pytest's default ``test_*.py`` discovery
pattern.  It is a release qualification entry point, not a fast test.  Run it
explicitly with:

    pytest --create-db -q -s backend/tests/paul_desmarais_production_pipeline_qualification.py

The source setup stops before operational scheduling resources. Sections,
section timing, online supervision sessions, named teachers, and enrollments
are created only by the reviewed production services under test.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, replace
from datetime import date
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
from time import perf_counter

from django.contrib.auth.models import User
import pytest

from backend.apps.common.constants import (
    COURSE_REQUEST_TYPE_PRIMARY,
    GRADE_LEVEL_9,
    GRADE_LEVEL_10,
    GRADE_LEVEL_11,
    GRADE_LEVEL_12,
    QUALIFICATION_DIVISION_SENIOR,
    QUALIFICATION_REVIEW_VERIFIED,
)
from backend.apps.common.models import AcademicYear
from backend.apps.constraints.conflict_matrix import create_course_conflict_matrix
from backend.apps.constraints.models import (
    CourseQualificationRequirement,
    Qualification,
    TeacherQualification,
)
from backend.apps.courses.constants import (
    COURSE_DELIVERY_KIND_CO_OP,
    COURSE_DELIVERY_KIND_ONLINE,
    COURSE_DURATION_HALF_SEMESTER,
)
from backend.apps.courses.models import (
    Course,
    CoursePrerequisite,
    CourseRequest,
    CourseSequencePreference,
    DeliveryGroup,
    Enrollment,
    HalfSemesterCoursePair,
    HalfSemesterSectionPair,
    Section,
    StudentScheduleCommitmentRequest,
)
from backend.apps.courses.services.offerings import ensure_academic_year_offerings
from backend.apps.people.models import Student, Teacher
from backend.apps.scheduling.constants import STUDENT_ASSIGNMENT_STAFFING_MODE_FINAL_STAFFING
from backend.apps.scheduling.models import (
    CapacityProfile,
    CoursePriorityProfile,
    OnlineSupervisionConfiguration,
    OnlineSupervisionSession,
    SectionSchedule,
    TeacherPlanningAnnualCapacity,
    TeacherPlanningCapacity,
    TeacherPlanningRoster,
    TimeSlot,
)
from backend.apps.scheduling.services.engine_adapter import (
    load_student_assignment_input,
)
from backend.apps.scheduling.services.online_supervision import (
    approve_online_supervision_plan_run,
    create_online_supervision_plan_run,
)
from backend.apps.scheduling.services.section_budget_planning import (
    approve_section_budget_run,
    create_section_budget_run,
)
from backend.apps.scheduling.services.section_placement import (
    approve_section_placement_run,
    create_section_placement_run,
)
from backend.apps.scheduling.services.staffing_configuration import (
    confirm_roster_ready,
    set_roster_members,
)
from backend.apps.scheduling.services.staffing_planning import create_staffing_plan_run
from backend.apps.scheduling.services.teacher_assignment import (
    approve_teacher_assignment_run,
    create_teacher_assignment_run,
)
from scheduling_engine.benchmark_global_feasibility import (
    capacity_only_matching,
    ordinary_and_half_pair_diagnostic,
    reduced_collision_diagnostic,
)
from scheduling_engine.benchmark_individual_feasibility import preflight_individual_feasibility
from scheduling_engine.paul_desmarais_production_pipeline_source import (
    GRADES,
    LINEAGE_ID,
    SOURCE_SEED,
    SOURCE_VERSION,
    SourceSpec,
    build_source_spec,
    source_summary,
)
from scheduling_engine.student_assignment import core as student_assignment_core
from scheduling_engine.student_assignment.runtime import semantic_student_assignment_input_fingerprint


SOFT_IMPORTANCE = {
    "section_utilization_balance": "important",
    "student_semester_balance": "important",
    "course_sequence_preferences": "important",
    "difficulty_balance": "important",
    "course_category_diversity": "important",
}
STAGE1_LIMIT_SECONDS = 120.0
STAGE1_WORKERS = 8
STAGE1_VALIDATION_LIMIT_SECONDS = 60.0
STAGE1_VALIDATION_WORKERS = 8
QUALIFICATION_ATTEMPT_ID = "post_half_pair_placement_fix_20260912_r2"
ARTIFACT_ROOT = (
    Path("scheduling_engine/benchmarks/production_pipeline")
    / LINEAGE_ID / "attempts" / QUALIFICATION_ATTEMPT_ID
)


class QualificationStopped(RuntimeError):
    """A required production qualification gate did not pass."""

    def __init__(self, stage, evidence):
        super().__init__(f"qualification stopped at {stage}")
        self.stage = stage
        self.evidence = evidence


def _timed(stage_times, name, callback):
    started = perf_counter()
    value = callback()
    stage_times[name] = round(perf_counter() - started, 3)
    print(f"[paul-d-production] {name}: {stage_times[name]:.3f}s", flush=True)
    return value


def _json_default(value):
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "value"):
        return value.value
    return str(value)


def _write_artifact(name, payload):
    ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
    path = ARTIFACT_ROOT / name
    path.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=_json_default),
        encoding="utf-8",
    )
    return str(path)


def _half_pair_topology(academic_year):
    """Audit the accepted physical pair contract before student assignment."""

    pairs = list(HalfSemesterSectionPair.objects.filter(
        first_section__academic_year=academic_year,
    ).select_related(
        "first_section__course", "second_section__course",
    ).order_by("id"))
    schedules = {
        row.section_id: row.timeslot_id
        for row in SectionSchedule.objects.filter(
            section_id__in=[
                section_id for pair in pairs
                for section_id in (pair.first_section_id, pair.second_section_id)
            ],
        )
    }
    co_timed = [
        pair for pair in pairs
        if (
            pair.first_section.semester == pair.second_section.semester
            and schedules.get(pair.first_section_id) == schedules.get(pair.second_section_id)
            and pair.first_section.capacity_max == pair.second_section.capacity_max
        )
    ]
    return {
        "chv_section_count": Section.objects.filter(
            academic_year=academic_year, course__course_code="CHV2O",
        ).count(),
        "glc_section_count": Section.objects.filter(
            academic_year=academic_year, course__course_code="GLC2O",
        ).count(),
        "materialized_pair_count": len(pairs),
        "co_timed_pair_count": len(co_timed),
        "split_pair_count": len(pairs) - len(co_timed),
        "valid_pair_capacity": sum(pair.first_section.capacity_max for pair in co_timed),
    }


def _downstream_counts(academic_year):
    sections = Section.objects.filter(academic_year=academic_year)
    return {
        "sections": sections.count(),
        "section_schedules": SectionSchedule.objects.filter(
            section__academic_year=academic_year,
        ).count(),
        "online_sessions": OnlineSupervisionSession.objects.filter(
            academic_year=academic_year,
        ).count(),
        "named_section_teachers": sections.filter(teacher__isnull=False).count(),
        "named_online_supervisors": OnlineSupervisionSession.objects.filter(
            academic_year=academic_year,
            supervisor__isnull=False,
        ).count(),
        "enrollments": Enrollment.objects.filter(
            section__academic_year=academic_year,
        ).count(),
    }


def _materialize_source(spec: SourceSpec, counselor_user):
    academic_year = AcademicYear.objects.create(name=spec.academic_year_name)
    normal_profile = CapacityProfile.objects.create(
        name=f"{LINEAGE_ID} normal 40/35",
        hard_min=1,
        soft_min=24,
        target=35,
        soft_max=38,
        hard_max=40,
    )
    sequence_profile = CapacityProfile.objects.create(
        name=f"{LINEAGE_ID} sequence 30/28",
        hard_min=1,
        soft_min=20,
        target=28,
        soft_max=29,
        hard_max=30,
    )
    online_profile = CapacityProfile.objects.create(
        name=f"{LINEAGE_ID} online 16/14",
        hard_min=1,
        soft_min=10,
        target=14,
        soft_max=15,
        hard_max=16,
    )
    priority = CoursePriorityProfile.objects.create(
        name=f"{LINEAGE_ID} primary",
        tier=1,
    )
    qualifications = {
        f"qual:{index}": Qualification.objects.create(
            code=f"{LINEAGE_ID}-qualification-{index}",
            name=f"{LINEAGE_ID} qualification {index}",
            kind="teachable",
            subject_code="paul_desmarais_shaped",
            division=QUALIFICATION_DIVISION_SENIOR,
        )
        for index in range(11)
    }
    courses = {}
    for source_course in spec.courses:
        profile = sequence_profile if source_course.source_course_id in {17, 18} else normal_profile
        course = Course.objects.create(
            name=f"{source_course.name_fr} / {source_course.name_en}",
            grade_level=source_course.grade_level,
            course_code=source_course.code,
            category=source_course.category,
            delivery_kind=source_course.delivery_kind,
            duration=source_course.duration,
            credit_value=Decimal(str(source_course.credit_value)),
            capacity_profile=profile,
            priority_profile=priority,
            capacity_min=profile.hard_min,
            capacity_max=profile.hard_max,
            allowed_semester=source_course.allowed_semester,
        )
        courses[source_course.key] = course
        qualification = qualifications.get(source_course.qualification_key)
        if qualification is not None:
            CourseQualificationRequirement.objects.create(
                course=course,
                qualification=qualification,
            )
    HalfSemesterCoursePair.objects.create(
        first_course=courses["course:15"],
        second_course=courses["course:16"],
    )
    # The soft ordering example is intentionally distinct from official
    # prerequisites. The broader prerequisite catalog/waiver workflow remains
    # outside this qualification.
    CourseSequencePreference.objects.create(
        earlier_course=courses["course:17"],
        later_course=courses["course:18"],
        created_by=counselor_user,
    )

    students = Student.objects.bulk_create([
        Student(
            student_number=f"PDPL-{student_number:04d}",
            email=f"{LINEAGE_ID}-{student_number:04d}@example.test",
            first_name="Paul",
            last_name=f"Desmarais {student_number:04d}",
            date_of_birth=date(2008 - max(0, grade - 9), 1, 1),
            grade_level=grade,
            academic_year=academic_year,
        )
        for student_number, grade in spec.students
    ], batch_size=500)
    student_by_number = {
        int(student.student_number.rsplit("-", 1)[1]): student
        for student in students
    }
    CourseRequest.objects.bulk_create([
        CourseRequest(
            student=student_by_number[item.student_number],
            academic_year=academic_year,
            course=courses[item.course_key],
            request_type=COURSE_REQUEST_TYPE_PRIMARY,
            is_mandatory=item.is_mandatory,
        )
        for item in spec.requests
    ], batch_size=1000)
    StudentScheduleCommitmentRequest.objects.bulk_create([
        StudentScheduleCommitmentRequest(
            student=student_by_number[item.student_number],
            academic_year=academic_year,
            commitment_type=item.commitment_type,
            request_index=item.request_index,
        )
        for item in spec.commitment_requests
    ], batch_size=500)

    timeslots = {
        (semester, block): TimeSlot.objects.create(
            academic_year=academic_year,
            semester=semester,
            block=block,
        )
        for semester in (1, 2)
        for block in ("A", "B", "C", "D")
    }
    teachers = []
    for index in range(60):
        teachers.append(Teacher.objects.create(
            first_name="Paul",
            last_name=f"Pipeline Teacher {index:03d}",
            email=f"{LINEAGE_ID}-teacher-{index:03d}@example.test",
            department="Grades 9-12 production qualification",
            max_courses_per_semester=5,
            max_courses_total=10,
        ))
    for index, teacher in enumerate(teachers[5:]):
        TeacherQualification.objects.create(
            teacher=teacher,
            qualification=qualifications[f"qual:{index // 5}"],
            review_status=QUALIFICATION_REVIEW_VERIFIED,
        )
    for teacher in teachers:
        for semester in (1, 2):
            TeacherPlanningCapacity.objects.create(
                teacher=teacher,
                academic_year=academic_year,
                semester=semester,
                maximum_sections=5,
            )
        TeacherPlanningAnnualCapacity.objects.create(
            teacher=teacher,
            academic_year=academic_year,
            maximum_sections=10,
        )
    roster = TeacherPlanningRoster.objects.create(academic_year=academic_year)
    set_roster_members(roster, teacher_ids=[teacher.id for teacher in teachers], actor=counselor_user)
    confirm_roster_ready(roster, actor=counselor_user)
    OnlineSupervisionConfiguration.objects.create(
        academic_year=academic_year,
        capacity_profile=online_profile,
        updated_by=counselor_user,
    )
    return {
        "academic_year": academic_year,
        "courses": courses,
        "students": students,
        "teachers": teachers,
        "timeslots": timeslots,
        "normal_profile": normal_profile,
        "sequence_profile": sequence_profile,
        "online_profile": online_profile,
        "roster": roster,
    }


def _assert_source_boundary(academic_year):
    counts = _downstream_counts(academic_year)
    expected = {
        "sections": 0,
        "section_schedules": 0,
        "online_sessions": 0,
        "named_section_teachers": 0,
        "named_online_supervisors": 0,
        "enrollments": 0,
    }
    if counts != expected:
        raise QualificationStopped("source_boundary", {"counts": counts, "expected": expected})
    return counts


def _budget_summary(run):
    offerings = run.result.get("offerings", [])
    return {
        "status": run.status,
        "result_status": run.result.get("status"),
        "used_sections": run.result.get("used_sections"),
        "section_budget": run.section_budget,
        "offering_count": len(offerings),
        "positive_offering_count": sum(item.get("annual_count", 0) > 0 for item in offerings),
        "mandatory_unmet_demand": sum(item.get("unmet_demand", 0) for item in offerings),
        "offerings": [
            {
                "codes": item.get("member_course_codes", []),
                "demand": item.get("predicted_enrollment", 0),
                "annual_count": item.get("annual_count", 0),
                "semester_1_count": item.get("semester_1_count", 0),
                "semester_2_count": item.get("semester_2_count", 0),
                "capacity": item.get("capacity_policy", {}),
            }
            for item in offerings
        ],
    }


def _accepted_placement_artifact(academic_year, placement_run, source_fingerprint, roster):
    assignments = []
    delivery_group_codes = {
        group.id: "+".join(sorted(
            offering.course.course_code
            for offering in group.offerings.select_related("course").all()
        ))
        for group in DeliveryGroup.objects.filter(
            id__in=[
                item["delivery_group_id"]
                for item in placement_run.result.get("assignments", [])
                if item.get("delivery_group_id")
            ]
        ).prefetch_related("offerings__course")
    }
    for item in placement_run.result.get("assignments", []):
        if item.get("online_supervision_session_id") is not None:
            session = OnlineSupervisionSession.objects.get(pk=item["online_supervision_session_id"])
            identity = f"online:{session.session_number}"
            capacity = session.capacity_max
        else:
            section = None
            if item.get("delivery_group_id"):
                section = Section.objects.filter(
                    academic_year=academic_year,
                    delivery_group_id=item["delivery_group_id"],
                ).first()
            identity = (
                f"delivery:{delivery_group_codes[item['delivery_group_id']]}:annual:{item.get('annual_index')}"
            )
            capacity = section.capacity_max if section else None
        timeslot = TimeSlot.objects.get(pk=item["timeslot_id"])
        assignments.append({
            "identity": identity,
            "semester": int(item["semester"]),
            "block": timeslot.block,
            "capacity_max": capacity,
        })
    payload = {
        "lineage_id": LINEAGE_ID,
        "source_fingerprint": source_fingerprint,
        "placement_run_id": placement_run.id,
        "solver_outcome": placement_run.result.get("solver_outcome"),
        "staffing_witness": placement_run.result.get("staffing_summary", {}),
        "assignments": sorted(assignments, key=lambda item: (item["identity"], item["semester"], item["block"])),
        "roster_id": roster.id,
    }
    payload["artifact_hash"] = sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()
    return payload


def _run_stage1_only(student_input):
    requested_limit = STAGE1_LIMIT_SECONDS
    input_limit = float(student_input.time_limit_seconds)
    effective_limit = max(input_limit, requested_limit)
    if effective_limit != STAGE1_LIMIT_SECONDS:
        raise QualificationStopped("stage1_budget_contract", {
            "requested_limit": requested_limit,
            "input_limit": input_limit,
            "effective_limit": effective_limit,
        })
    print(
        f"[paul-d-production] stage1 ready: requested={requested_limit} input={input_limit} "
        f"effective={effective_limit} workers={STAGE1_WORKERS} mode=local_only",
        flush=True,
    )
    certification_input = replace(
        student_input,
        time_limit_seconds=requested_limit,
    )
    result = student_assignment_core._solve_student_assignment(
        certification_input,
        include_lock_costs=False,
        include_candidate_ledger=False,
        use_hard_feasibility_bootstrap=True,
        local_only=True,
        hard_feasibility_time_limit_seconds=STAGE1_LIMIT_SECONDS,
        hard_feasibility_worker_count=STAGE1_WORKERS,
        hard_feasibility_validation_time_limit_seconds=STAGE1_VALIDATION_LIMIT_SECONDS,
        hard_feasibility_validation_worker_count=STAGE1_VALIDATION_WORKERS,
    )
    facts = result.optimization_facts.get("stage_1", {})
    stage2 = result.optimization_facts.get("stage_2", {})
    if result.optimization_facts.get("optimization_passes"):
        raise QualificationStopped("stage1_only_boundary", {
            "optimization_passes": result.optimization_facts["optimization_passes"],
        })
    return result, {
        "stage1_reached": True,
        "effective_time_limit_seconds": effective_limit,
        "worker_count": facts.get("timings", {}).get("seed_worker_count", STAGE1_WORKERS),
        "raw_solver_outcome": facts.get("solver_outcome"),
        "result_status": result.status,
        "result_solver_outcome": result.solver_outcome,
        "complete_seed_produced": facts.get("complete_seed_produced"),
        "seed_validated_against_full_model": facts.get("seed_validated_against_full_model"),
        "runtime_seconds": facts.get("timings", {}).get("operation_wall_time_seconds"),
        "assignment_count": len(result.assignments),
        "completion_group_count": facts.get("required_decision_group_count"),
        "unmet_mandatory_count": sum(item.is_mandatory for item in result.unmet_requests),
        "special_commitment_count": len(result.commitment_assignments),
        "stage2_optimization_pass_count": len(result.optimization_facts.get("optimization_passes", ())),
        "stage2_solver_outcome": stage2.get("solver_outcome"),
    }


def run_qualification(*, counselor_user):
    stage_times = {}
    spec = build_source_spec()
    source = source_summary(spec)
    academic_year = None
    report = {
        "lineage_id": LINEAGE_ID,
        "source_version": SOURCE_VERSION,
        "source_seed": SOURCE_SEED,
        "source_fingerprint": spec.fingerprint,
        "source": source,
        "provenance": dict((key, provenance) for key, _value, provenance in spec.assumptions),
        "stage_times": stage_times,
        "stage2_ran": False,
        "objective_v3_ran": False,
    }
    try:
        context = _timed(stage_times, "source_materialization", lambda: _materialize_source(spec, counselor_user))
        academic_year = context["academic_year"]
        boundary = _assert_source_boundary(academic_year)
        report["source_boundary"] = boundary
        report["source_setup_direct_output_counts"] = boundary
        report["artifacts"] = [_write_artifact("source_spec_manifest.json", {
            "lineage_id": LINEAGE_ID,
            "version": SOURCE_VERSION,
            "seed": SOURCE_SEED,
            "fingerprint": spec.fingerprint,
            "summary": source,
            "assumptions": [asdict(item) if hasattr(item, "__dataclass_fields__") else item for item in spec.assumptions],
            "source_boundary": boundary,
        })]

        offerings = _timed(stage_times, "offering_setup", lambda: ensure_academic_year_offerings(
            academic_year, actor=counselor_user,
        ))
        report["offering_count"] = len(offerings)

        online_run = _timed(stage_times, "online_supervision_planning", lambda: create_online_supervision_plan_run(
            academic_year=academic_year,
            created_by=counselor_user,
        ))
        report["online_plan"] = {
            "status": online_run.status,
            "result": online_run.result,
            "input_fingerprint": online_run.input_snapshot.get("fingerprint"),
        }
        if online_run.status != "complete" or online_run.result.get("status") != "complete":
            raise QualificationStopped("online_supervision_planning", report["online_plan"])
        online_approval = _timed(stage_times, "online_supervision_approval", lambda: approve_online_supervision_plan_run(
            online_run,
            approved_by=counselor_user,
            reason="Approve deterministic synthetic online-supervision demand capacity.",
        ))
        report["online_plan"]["approval_id"] = online_approval.id
        report["online_plan"]["session_count"] = OnlineSupervisionSession.objects.filter(
            academic_year=academic_year,
        ).count()
        report["artifacts"].append(_write_artifact("online_plan_manifest.json", report["online_plan"]))

        budget_run = _timed(stage_times, "section_budget_planning", lambda: create_section_budget_run(
            academic_year=academic_year,
            created_by=counselor_user,
            budget_type="ceiling",
            section_budget=400,
            backup_policy="ignore",
            backup_overrides=(),
            offering_constraints=(),
        ))
        report["budget_plan"] = _budget_summary(budget_run)
        if budget_run.status != "complete" or budget_run.result.get("status") != "complete":
            raise QualificationStopped("section_budget_planning", report["budget_plan"])
        if report["budget_plan"]["mandatory_unmet_demand"]:
            raise QualificationStopped("section_budget_mandatory_demand", report["budget_plan"])
        budget_approval = _timed(stage_times, "section_budget_approval", lambda: approve_section_budget_run(
            budget_run,
            approved_by=counselor_user,
            reason="Approve demand-derived annual physical delivery counts.",
        ))
        report["budget_plan"]["approval_id"] = budget_approval.id
        report["artifacts"].append(_write_artifact("section_budget_manifest.json", report["budget_plan"]))

        # The approved budget is already the reviewed annual count/split. Feed
        # those exact decisions back through the real staffing planner as
        # constraints so this preflight verifies qualified teacher capacity
        # without launching a second unconstrained re-optimization of every
        # possible count/split combination.
        staffing_constraints = [
            {
                "offering_id": item.delivery_group_id,
                "exact_sections": item.approved_annual_count,
                "semester_1_count": item.approved_semester_1_count,
                "semester_2_count": item.approved_semester_2_count,
            }
            for item in budget_approval.offering_approvals.all()
        ]
        staffing_run = _timed(stage_times, "staffing_count_preflight", lambda: create_staffing_plan_run(
            academic_year=academic_year,
            created_by=counselor_user,
            budget_approval=budget_approval,
            backup_policy="ignore",
            backup_overrides=(),
            offering_constraints=staffing_constraints,
            teacher_capacity_adjustments=(),
        ))
        report["staffing_preflight"] = {
            "status": staffing_run.status,
            "result_status": staffing_run.result.get("status"),
            "result": staffing_run.result,
            "approval_performed": False,
            "reviewed_budget_constraints": staffing_constraints,
        }
        if staffing_run.status != "complete" or staffing_run.result.get("status") != "complete":
            raise QualificationStopped("staffing_count_preflight", report["staffing_preflight"])
        report["artifacts"].append(_write_artifact("staffing_preflight_manifest.json", report["staffing_preflight"]))

        matrix = _timed(stage_times, "conflict_matrix", lambda: create_course_conflict_matrix(
            academic_year=academic_year,
            initialization_mode="fresh_current_demand",
            actor=counselor_user,
        ))
        report["conflict_matrix"] = {
            "id": matrix.id,
            "revision": matrix.revision,
            "request_fingerprint": matrix.request_fingerprint,
            "relationship_count": matrix.conflicts.count(),
            "initialization_mode": matrix.initialization_mode,
            "overridden_count": matrix.conflicts.filter(is_overridden=True).count(),
        }
        report["artifacts"].append(_write_artifact("conflict_matrix_manifest.json", report["conflict_matrix"]))

        placement_run = _timed(stage_times, "annual_section_placement", lambda: create_section_placement_run(
            academic_year_id=academic_year.id,
            input_mode="annual_total",
            budget_approval=budget_approval,
            created_by=counselor_user,
        ))
        placement_result = placement_run.result
        report["placement"] = {
            "status": placement_run.status,
            "solver_outcome": placement_result.get("solver_outcome"),
            "result_status": placement_result.get("status"),
            "assignment_count": len(placement_result.get("assignments", [])),
            "diagnostic_count": len(placement_result.get("diagnostics", [])),
            "staffing_witness": placement_result.get("staffing_summary", {}),
            "run_id": placement_run.id,
        }
        if placement_result.get("solver_outcome") not in {"feasible", "optimal"}:
            raise QualificationStopped("annual_section_placement", report["placement"])
        if placement_run.status != "complete" or placement_result.get("status") != "complete":
            raise QualificationStopped("annual_section_placement", report["placement"])
        if not placement_result.get("staffing_summary", {}).get("witness_proven", False):
            raise QualificationStopped("anonymous_staffing_witness", report["placement"])
        placement_approval = _timed(stage_times, "placement_approval", lambda: approve_section_placement_run(
            placement_run,
            approved_by=counselor_user,
            reason="Approve the reviewed annual semester and A-D placement.",
        ))
        report["placement"]["approval_id"] = placement_approval.id
        report["placement"]["materialized_counts"] = _downstream_counts(academic_year)
        report["placement"]["half_pair_topology"] = _half_pair_topology(academic_year)
        report["artifacts"].append(_write_artifact(
            "accepted_half_pair_topology.json",
            report["placement"]["half_pair_topology"],
        ))
        if report["placement"]["half_pair_topology"]["split_pair_count"]:
            raise QualificationStopped(
                "accepted_half_pair_topology",
                report["placement"]["half_pair_topology"],
            )
        report["placement"]["artifact"] = _accepted_placement_artifact(
            academic_year, placement_run, spec.fingerprint, context["roster"],
        )
        report["artifacts"].append(_write_artifact(
            "accepted_production_placement.json",
            report["placement"]["artifact"],
        ))

        teacher_run = _timed(stage_times, "named_teacher_assignment", lambda: create_teacher_assignment_run(
            academic_year_id=academic_year.id,
            created_by=counselor_user,
        ))
        report["teacher_assignment"] = {
            "status": teacher_run.status,
            "result_status": teacher_run.result.get("status"),
            "assignment_count": len(teacher_run.result.get("assignments", [])),
            "run_id": teacher_run.id,
            "result": teacher_run.result,
        }
        if teacher_run.status != "complete" or teacher_run.result.get("status") != "complete":
            raise QualificationStopped("named_teacher_assignment", report["teacher_assignment"])
        teacher_approval = _timed(stage_times, "named_teacher_approval", lambda: approve_teacher_assignment_run(
            teacher_run,
            approved_by=counselor_user,
            reason="Approve named teachers and online supervisors.",
        ))
        report["teacher_assignment"]["approval_id"] = teacher_approval.id
        report["teacher_assignment"]["coverage"] = _downstream_counts(academic_year)
        report["artifacts"].append(_write_artifact("named_teacher_assignment_manifest.json", report["teacher_assignment"]))

        student_input, staffing_context = _timed(stage_times, "final_staffing_adapter", lambda: load_student_assignment_input(
            academic_year_id=academic_year.id,
            staffing_mode=STUDENT_ASSIGNMENT_STAFFING_MODE_FINAL_STAFFING,
            soft_constraint_importance=SOFT_IMPORTANCE,
        ))
        input_fingerprint = semantic_student_assignment_input_fingerprint(student_input)
        report["final_staffing_input"] = {
            "fingerprint": input_fingerprint,
            "student_count": len({item.student_id for item in student_input.requests}),
            "request_count": len(student_input.requests),
            "section_count": len(student_input.sections),
            "online_session_count": len(student_input.online_supervision_sessions),
            "completion_group_count": len(student_input.requests) + len(student_input.schedule_commitment_requests),
            "special_commitment_request_count": len(student_input.schedule_commitment_requests),
            "staffing_context": staffing_context,
        }
        report["artifacts"].append(_write_artifact("final_staffing_input_manifest.json", report["final_staffing_input"]))

        isolated = _timed(stage_times, "isolated_student_feasibility", lambda: preflight_individual_feasibility(student_input))
        report["isolated_feasibility"] = isolated
        if isolated.get("infeasible_count") or isolated.get("unresolved_count") or isolated.get("feasible_count") != 1400:
            raise QualificationStopped("isolated_student_feasibility", isolated)

        capacity = _timed(stage_times, "capacity_only_matching", lambda: capacity_only_matching(student_input))
        report["capacity_only_matching"] = capacity
        if not capacity.get("feasible"):
            raise QualificationStopped("capacity_only_matching", capacity)

        collision = _timed(stage_times, "reduced_collision_diagnostic", lambda: reduced_collision_diagnostic(
            student_input,
            time_limit_seconds=120.0,
        ))
        report["reduced_collision_diagnostic"] = collision
        if collision.get("status") not in {"optimal", "feasible"} or not collision.get("feasible", False):
            raise QualificationStopped("reduced_collision_diagnostic", collision)

        half_pair = _timed(stage_times, "ordinary_and_half_pair_diagnostic", lambda: ordinary_and_half_pair_diagnostic(
            student_input,
            time_limit_seconds=120.0,
        ))
        report["ordinary_and_half_pair_diagnostic"] = half_pair
        report["artifacts"].append(_write_artifact(
            "ordinary_and_half_pair_diagnostic.json", half_pair,
        ))
        if half_pair.get("status") not in {"optimal", "feasible"} or not half_pair.get("feasible", False):
            raise QualificationStopped("ordinary_and_half_pair_diagnostic", half_pair)

        stage1_result, stage1 = _timed(stage_times, "student_assignment_stage1", lambda: _run_stage1_only(student_input))
        report["stage1"] = stage1
        if stage1["raw_solver_outcome"] not in {"feasible", "optimal"}:
            raise QualificationStopped("student_assignment_stage1", stage1)
        if not stage1["complete_seed_produced"] or not stage1["seed_validated_against_full_model"]:
            raise QualificationStopped("stage1_validation", stage1)
        if stage1["unmet_mandatory_count"] or stage1["assignment_count"] <= 0:
            raise QualificationStopped("stage1_completeness", stage1)
        report["stage1_seed"] = {
            "source_fingerprint": input_fingerprint,
            "assignment_count": stage1["assignment_count"],
            "completion_group_count": stage1["completion_group_count"],
            "unmet_mandatory_count": stage1["unmet_mandatory_count"],
            "special_commitment_count": stage1["special_commitment_count"],
            "raw_solver_outcome": stage1["raw_solver_outcome"],
            "independent_full_model_validation": stage1["seed_validated_against_full_model"],
        }
        report["artifacts"].append(_write_artifact("validated_stage1_seed_manifest.json", report["stage1_seed"]))
        report["globally_feasibility_certified"] = True
        report["pipeline_status"] = "complete"
        report["stopped_at"] = None
        _write_artifact("qualification_report.json", report)
        print(json.dumps({
            "lineage_id": LINEAGE_ID,
            "pipeline_status": report["pipeline_status"],
            "globally_feasibility_certified": True,
            "source_fingerprint": spec.fingerprint,
            "stage1": report["stage1"],
            "stage_times": report["stage_times"],
        }, sort_keys=True, separators=(",", ":"), default=_json_default), flush=True)
        return report
    except QualificationStopped as stopped:
        report["pipeline_status"] = "blocked"
        report["stopped_at"] = stopped.stage
        report["failure_evidence"] = stopped.evidence
        _write_artifact("qualification_failure.json", report)
        print(json.dumps({
            "lineage_id": LINEAGE_ID,
            "pipeline_status": "blocked",
            "stopped_at": stopped.stage,
            "evidence": stopped.evidence,
            "stage_times": report["stage_times"],
        }, sort_keys=True, separators=(",", ":"), default=_json_default), flush=True)
        raise


@pytest.mark.django_db(transaction=True)
def test_paul_desmarais_production_pipeline_qualification(counselor_user):
    """Run the one explicit target-scale pipeline qualification."""

    run_qualification(counselor_user=counselor_user)


if __name__ == "__main__":
    raise SystemExit(
        "Invoke through pytest so the isolated Django database and counselor fixture are configured."
    )
