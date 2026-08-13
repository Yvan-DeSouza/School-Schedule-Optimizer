"""Realistic integration coverage for special student schedule commitments.

The test deliberately invokes the reviewed planning services in their normal
order.  It uses the Django test database, never the developer's scheduling
data, so it can expose integration gaps without manufacturing final sections
or enrollments by hand.
"""

from __future__ import annotations

from collections import defaultdict
from django.contrib.auth.models import User
import pytest

from backend.apps.common.constants import (
    COURSE_REQUEST_TYPE_PRIMARY,
    GRADE_LEVEL_12,
    QUALIFICATION_DIVISION_SENIOR,
    QUALIFICATION_REVIEW_VERIFIED,
)
from backend.apps.common.models import AcademicYear
from backend.apps.constraints.models import (
    CourseQualificationRequirement,
    Qualification,
    TeacherAvailability,
    TeacherQualification,
)
from backend.apps.constraints.conflict_matrix import create_course_conflict_matrix
from backend.apps.courses.models import (
    Course,
    CourseRequest,
    Enrollment,
    HalfSemesterCoursePair,
    HalfSemesterSectionPair,
    Section,
    StudentScheduleCommitmentRequest,
)
from backend.apps.courses.services.offerings import ensure_academic_year_offerings
from backend.apps.people.models import Student, Teacher
from backend.apps.scheduling.models import (
    CapacityProfile,
    CoursePriorityProfile,
    OnlineEnrollment,
    OnlineSupervisionConfiguration,
    OnlineSupervisionSession,
    SectionSchedule,
    StudentScheduleCommitment,
    StudentScheduleCommitmentOccupancy,
    TeacherPlanningAnnualCapacity,
    TeacherPlanningCapacity,
    TeacherPlanningRoster,
    TimeSlot,
)
from backend.apps.scheduling.constants import STUDENT_ASSIGNMENT_RUN_SCOPE_SCOPED
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
from backend.apps.scheduling.services.staffing_planning import (
    approve_staffing_plan_run,
    create_staffing_plan_run,
)
from backend.apps.scheduling.services.student_assignment import (
    approve_student_assignment_run,
    create_student_assignment_run,
    preview_student_assignment_approval,
)
from backend.apps.scheduling.services.student_special_commitment_locks import (
    create_student_special_commitment_lock,
)
from backend.apps.scheduling.services.teacher_assignment import (
    approve_teacher_assignment_run,
    create_teacher_assignment_run,
)


SOFT_IMPORTANCE = {
    "section_utilization_balance": "important",
    "student_semester_balance": "important",
    "course_sequence_preferences": "important",
    "difficulty_balance": "important",
    "course_category_diversity": "important",
}


def _student(*, academic_year, index):
    """Create a minimal local student identity for this isolated scenario."""

    user = User.objects.create_user(username=f"pipeline-student-{index}")
    return Student.objects.create(
        user=user,
        student_number=f"PIPE-{index:03d}",
        email=f"pipeline-student-{index}@example.test",
        first_name="Pipeline",
        last_name=f"Student {index:03d}",
        date_of_birth="2008-01-01",
        grade_level=GRADE_LEVEL_12,
        academic_year=academic_year,
    )


def _teacher(*, index, qualified):
    """Create a teacher; the online supervisor intentionally has no teachable."""

    user = User.objects.create_user(username=f"pipeline-teacher-{index}")
    teacher = Teacher.objects.create(
        user=user,
        first_name="Pipeline",
        last_name=f"Teacher {index:03d}",
        email=f"pipeline-teacher-{index}@example.test",
        department="Scheduling validation",
        max_courses_per_semester=8,
        max_courses_total=16,
    )
    for qualification in qualified:
        TeacherQualification.objects.create(
            teacher=teacher,
            qualification=qualification,
            review_status=QUALIFICATION_REVIEW_VERIFIED,
        )
    return teacher


def _course(*, code, name, category, profile, priority, **extra):
    """Keep the scenario's catalog explicit instead of relying on test defaults."""

    return Course.objects.create(
        course_code=code,
        name=name,
        grade_level=GRADE_LEVEL_12,
        category=category,
        capacity_profile=profile,
        priority_profile=priority,
        capacity_min=profile.hard_min,
        capacity_max=profile.hard_max,
        **extra,
    )


@pytest.mark.django_db
def test_realistic_special_commitments_flow_through_reviewed_pipeline(counselor_user):
    """Exercise demand through student approval with a small real-school mixture.

    The scenario contains ordinary pupils, requested Study, shared online
    supervision, Co-op, locked Focus, the school's two normal trimestre
    courses, and one online half-semester request. It intentionally leaves one
    student with spare time and no Study request: approval must never invent a
    Study commitment for that pupil and must instead expose factual review.
    """

    academic_year = AcademicYear.objects.create(name="2031-2032")
    profile = CapacityProfile.objects.create(
        name="Pipeline small class",
        hard_min=1,
        soft_min=2,
        target=4,
        soft_max=5,
        hard_max=6,
    )
    online_profile = CapacityProfile.objects.create(
        name="Pipeline online supervision",
        hard_min=1,
        soft_min=3,
        target=4,
        soft_max=5,
        hard_max=6,
    )
    priority = CoursePriorityProfile.objects.create(name="Pipeline core", tier=1)

    math_qualification = Qualification.objects.create(
        code="pipeline-math",
        name="Pipeline mathematics",
        kind="teachable",
        subject_code="mathematics",
        division=QUALIFICATION_DIVISION_SENIOR,
    )
    humanities_qualification = Qualification.objects.create(
        code="pipeline-humanities",
        name="Pipeline humanities",
        kind="teachable",
        subject_code="english",
        division=QUALIFICATION_DIVISION_SENIOR,
    )

    math = _course(code="PMAT4U", name="Mathematics", category="math", profile=profile, priority=priority)
    science = _course(code="PSCI4U", name="Science", category="science", profile=profile, priority=priority)
    english = _course(code="PENG4U", name="English", category="english", profile=profile, priority=priority)
    history = _course(code="PHIS4U", name="History", category="social_sciences", profile=profile, priority=priority)
    focus_semester_two_math = _course(
        code="PFM4U", name="Focus return Mathematics", category="math", profile=profile,
        priority=priority, allowed_semester="semester_2_only",
    )
    focus_semester_one_science = _course(
        code="PFS4U", name="Focus departure Science", category="science", profile=profile,
        priority=priority, allowed_semester="semester_1_only",
    )
    wellness = _course(
        code="PHEM4U", name="Wellness first half", category="health_and_physical_education",
        profile=profile, priority=priority, duration="half_semester", credit_value="0.5",
    )
    arts = _course(
        code="PAVI4U", name="Visual Arts second half", category="arts",
        profile=profile, priority=priority, duration="half_semester", credit_value="0.5",
    )
    online_math = _course(
        code="PMAT4V", name="Online Mathematics", category="math", profile=profile,
        priority=priority, delivery_kind="online",
    )
    online_english = _course(
        code="PENG4V", name="Online English", category="english", profile=profile,
        priority=priority, delivery_kind="online",
    )
    online_wellness = _course(
        code="PHEM4V", name="Online Wellness first half",
        category="health_and_physical_education", profile=profile, priority=priority,
        delivery_kind="online", duration="half_semester", credit_value="0.5",
    )
    online_arts = _course(
        code="PAVI4V", name="Online Visual Arts second half", category="arts",
        profile=profile, priority=priority, delivery_kind="online",
        duration="half_semester", credit_value="0.5",
    )
    co_op = _course(
        code="PCOP4X", name="Co-op", category="", profile=profile, priority=priority,
        delivery_kind="co_op", credit_value="2.0",
    )
    HalfSemesterCoursePair.objects.create(first_course=wellness, second_course=arts)
    # The real school has only two normal trimestre courses. This separate
    # online pair exists only to validate the defined online-half interaction;
    # one side is requested so the unused supervision half remains reviewable.
    HalfSemesterCoursePair.objects.create(
        first_course=online_wellness,
        second_course=online_arts,
    )

    for course in (math, science, wellness, focus_semester_two_math, focus_semester_one_science):
        CourseQualificationRequirement.objects.create(course=course, qualification=math_qualification)
    for course in (english, history, arts):
        CourseQualificationRequirement.objects.create(course=course, qualification=humanities_qualification)

    # The first teacher deliberately has no qualification.  This lets the
    # named-assignment stage prove that online supervision needs workload and
    # availability, but not a subject-specific teachable.
    supervisor = _teacher(index=1, qualified=())
    math_teacher = _teacher(index=2, qualified=(math_qualification,))
    humanities_teacher = _teacher(index=3, qualified=(humanities_qualification,))
    flexible_teacher = _teacher(index=4, qualified=(math_qualification, humanities_qualification))
    teachers = (supervisor, math_teacher, humanities_teacher, flexible_teacher)

    slots = {
        (semester, block): TimeSlot.objects.create(
            academic_year=academic_year, semester=semester, block=block,
        )
        for semester in (1, 2)
        for block in ("A", "B", "C", "D")
    }
    # This is a genuine availability fact rather than a solver shortcut.  The
    # other qualified teacher makes the scenario feasible while exercising the
    # availability constraint at both staffing stages.
    TeacherAvailability.objects.create(
        teacher=math_teacher,
        timeslot=slots[(1, "A")],
        is_available=False,
    )
    for teacher in teachers:
        for semester in (1, 2):
            TeacherPlanningCapacity.objects.create(
                teacher=teacher,
                academic_year=academic_year,
                semester=semester,
                maximum_sections=8,
            )
        TeacherPlanningAnnualCapacity.objects.create(
            teacher=teacher,
            academic_year=academic_year,
            maximum_sections=16,
        )
    roster = TeacherPlanningRoster.objects.create(academic_year=academic_year)
    set_roster_members(roster, teacher_ids=[teacher.id for teacher in teachers], actor=counselor_user)
    confirm_roster_ready(roster, actor=counselor_user)

    # The small cohort mirrors common counselor cases without turning the
    # fixture into an artificial catalogue of edge cases.
    normal_student = _student(academic_year=academic_year, index=1)
    one_study_student = _student(academic_year=academic_year, index=2)
    two_study_student = _student(academic_year=academic_year, index=3)
    online_student = _student(academic_year=academic_year, index=4)
    second_online_student = _student(academic_year=academic_year, index=5)
    co_op_student = _student(academic_year=academic_year, index=6)
    focus_one_student = _student(academic_year=academic_year, index=7)
    focus_two_student = _student(academic_year=academic_year, index=8)
    paired_half_student = _student(academic_year=academic_year, index=9)
    first_half_only_student = _student(academic_year=academic_year, index=10)
    second_half_only_student = _student(academic_year=academic_year, index=11)
    online_half_student = _student(academic_year=academic_year, index=12)

    requests = {
        normal_student: (math, science, english, online_math),
        one_study_student: (math, english, science, online_english),
        two_study_student: (math,),
        online_student: (online_math, english, science),
        # This student is the integration-level regression for the generic
        # supervision block-diversity witness: both online courses must land
        # in distinct physical session times, without dedicated course rooms.
        second_online_student: (online_math, online_english, math),
        co_op_student: (co_op, english, math, online_math),
        focus_one_student: (focus_semester_two_math,),
        focus_two_student: (focus_semester_one_science,),
        paired_half_student: (wellness, arts, math, english),
        first_half_only_student: (wellness, science, english),
        second_half_only_student: (arts, math, history),
        online_half_student: (online_wellness, history),
    }
    request_by_student_course = {}
    for student, courses in requests.items():
        for course in courses:
            request_by_student_course[student.id, course.id] = CourseRequest.objects.create(
                student=student,
                academic_year=academic_year,
                course=course,
                request_type=COURSE_REQUEST_TYPE_PRIMARY,
                is_mandatory=True,
            )

    one_study_request = StudentScheduleCommitmentRequest.objects.create(
        student=one_study_student, academic_year=academic_year, commitment_type="study", request_index=1,
    )
    first_two_study_request = StudentScheduleCommitmentRequest.objects.create(
        student=two_study_student, academic_year=academic_year, commitment_type="study", request_index=1,
    )
    second_two_study_request = StudentScheduleCommitmentRequest.objects.create(
        student=two_study_student, academic_year=academic_year, commitment_type="study", request_index=2,
    )
    focus_one_request = StudentScheduleCommitmentRequest.objects.create(
        student=focus_one_student, academic_year=academic_year, commitment_type="focus", request_index=1,
    )
    focus_two_request = StudentScheduleCommitmentRequest.objects.create(
        student=focus_two_student, academic_year=academic_year, commitment_type="focus", request_index=1,
    )

    ensure_academic_year_offerings(academic_year, actor=counselor_user)
    offerings = {item.course_id: item for item in academic_year.courseoffering_set.all()}
    # The online planner owns a separate capacity policy. Seven online requests
    # against a target of four must create more than one shared session.
    OnlineSupervisionConfiguration.objects.create(
        academic_year=academic_year,
        capacity_profile=online_profile,
        updated_by=counselor_user,
    )
    online_plan = create_online_supervision_plan_run(academic_year=academic_year, created_by=counselor_user)
    assert len(online_plan.result["sessions"]) == 2
    approve_online_supervision_plan_run(
        online_plan,
        approved_by=counselor_user,
        reason="Approve reviewed shared online supervision capacity.",
    )

    # This is the authoritative normal-course demand path.  The selected
    # trimestre courses are explicitly confined to the same semester so their
    # normal pairing policy is testable rather than left to a loose annual
    # count split.
    half_constraints = tuple(
        {
            "offering_id": offerings[course.id].delivery_group_id,
            "exact_sections": 1,
            "semester_1_count": 1,
            "semester_2_count": 0,
        }
        for course in (wellness, arts)
    )
    budget_run = create_section_budget_run(
        academic_year=academic_year,
        created_by=counselor_user,
        budget_type="ceiling",
        section_budget=30,
        backup_policy="ignore",
        backup_overrides=(),
        offering_constraints=half_constraints,
    )
    assert budget_run.status == "complete"
    budget_approval = approve_section_budget_run(
        budget_run,
        approved_by=counselor_user,
        reason="Approve demand-based normal instructional counts.",
    )
    staffing_run = create_staffing_plan_run(
        academic_year=academic_year,
        created_by=counselor_user,
        budget_approval=budget_approval,
        backup_policy="ignore",
        backup_overrides=(),
        offering_constraints=half_constraints,
        teacher_capacity_adjustments=(),
    )
    assert staffing_run.status == "complete"
    approve_staffing_plan_run(
        staffing_run,
        approved_by=counselor_user,
        reason="Approve staff-feasible normal instructional sections.",
    )

    # A paired normal trimestre is a single sequential teaching block.  This
    # assertion is intentionally placed immediately after the actual section
    # creation stage so a missing integration cannot be hidden downstream.
    assert HalfSemesterSectionPair.objects.filter(
        course_pair__first_course=wellness,
        course_pair__second_course=arts,
    ).count() == 1

    create_course_conflict_matrix(
        academic_year=academic_year,
        initialization_mode="fresh_current_demand",
        actor=counselor_user,
    )
    placement_run = create_section_placement_run(
        academic_year_id=academic_year.id,
        input_mode="fixed_semester",
        created_by=counselor_user,
    )
    assert placement_run.status == "complete"
    approve_section_placement_run(
        placement_run,
        approved_by=counselor_user,
        reason="Approve reviewed semester and A-D placement.",
    )
    assert OnlineSupervisionSession.objects.filter(
        academic_year=academic_year,
        timeslot__isnull=False,
    ).count() == 2
    assert len(set(OnlineSupervisionSession.objects.filter(
        academic_year=academic_year,
    ).values_list("timeslot_id", flat=True))) == 2

    teacher_run = create_teacher_assignment_run(
        academic_year_id=academic_year.id,
        created_by=counselor_user,
    )
    assert teacher_run.status == "complete"
    approve_teacher_assignment_run(
        teacher_run,
        approved_by=counselor_user,
        reason="Approve named teachers and online supervisors.",
    )
    assert OnlineSupervisionSession.objects.filter(
        academic_year=academic_year,
        supervisor=supervisor,
    ).exists()

    first_study_lock = create_student_special_commitment_lock(
        academic_year=academic_year,
        created_by=counselor_user,
        lock_type="study_time",
        lock_mode="exact",
        schedule_commitment_request=first_two_study_request,
        timeslot=slots[(1, "A")],
        reason="Keep the first requested Study in Semester 1 Block A.",
    )
    second_study_lock = create_student_special_commitment_lock(
        academic_year=academic_year,
        created_by=counselor_user,
        lock_type="study_time",
        lock_mode="exact",
        schedule_commitment_request=second_two_study_request,
        timeslot=slots[(1, "B")],
        reason="Keep the second requested Study in Semester 1 Block B.",
    )
    create_student_special_commitment_lock(
        academic_year=academic_year,
        created_by=counselor_user,
        lock_type="focus_semester",
        lock_mode="exact",
        schedule_commitment_request=focus_one_request,
        semester=1,
        reason="The external Focus program is confirmed for Semester 1.",
    )
    create_student_special_commitment_lock(
        academic_year=academic_year,
        created_by=counselor_user,
        lock_type="focus_semester",
        lock_mode="exact",
        schedule_commitment_request=focus_two_request,
        semester=2,
        reason="The external Focus program is confirmed for Semester 2.",
    )
    create_student_special_commitment_lock(
        academic_year=academic_year,
        created_by=counselor_user,
        lock_type="co_op_time",
        lock_mode="exact",
        course_request=request_by_student_course[co_op_student.id, co_op.id],
        semester=2,
        co_op_block_pair="a_b",
        reason="The employer placement requires the Semester 2 A+B commitment.",
    )

    student_run = create_student_assignment_run(
        academic_year=academic_year,
        staffing_mode="final_staffing",
        soft_constraint_importance=SOFT_IMPORTANCE,
        created_by=counselor_user,
    )
    assert student_run.status == "complete", student_run.result
    review = preview_student_assignment_approval(student_run)
    approval = approve_student_assignment_run(
        student_run,
        approved_by=counselor_user,
        reason="Approve the reviewed student recommendations.",
    )

    assert approval.student_assignment_run_id == student_run.id
    assert OnlineEnrollment.objects.filter(supervision_session__academic_year=academic_year).count() == 7
    assert not Section.objects.filter(course=co_op, academic_year=academic_year).exists()
    assert offerings[co_op.id].delivery_group_id is None
    assert all(
        enrollment_count <= session.capacity_max
        for session, enrollment_count in (
            (session, session.online_enrollments.count())
            for session in OnlineSupervisionSession.objects.filter(academic_year=academic_year)
        )
    )
    second_online_enrollments = list(OnlineEnrollment.objects.filter(
        student=second_online_student,
        lifecycle_status="active",
    ).select_related("supervision_session").order_by("id"))
    assert len(second_online_enrollments) == 2
    assert len({item.supervision_session.timeslot_id for item in second_online_enrollments}) == 2
    # The supervisor carries a normal workload slot but is deliberately not a
    # teachable match for the online academic course codes being supervised.
    assert not TeacherQualification.objects.filter(teacher=supervisor).exists()
    assert StudentScheduleCommitment.objects.filter(
        student=normal_student,
        commitment_kind="study",
        lifecycle_status="active",
    ).count() == 0
    assert StudentScheduleCommitment.objects.filter(
        student=two_study_student,
        commitment_kind="study",
        lifecycle_status="active",
    ).count() == 2
    locked_study_occupancy = set(
        StudentScheduleCommitmentOccupancy.objects.filter(
            commitment__student=two_study_student,
            commitment__commitment_kind="study",
            commitment__lifecycle_status="active",
        ).values_list("timeslot_id", "half_semester_segment")
    )
    assert locked_study_occupancy == {
        (slots[(1, "A")].id, "first_half"),
        (slots[(1, "A")].id, "second_half"),
        (slots[(1, "B")].id, "first_half"),
        (slots[(1, "B")].id, "second_half"),
    }
    co_op_commitment = StudentScheduleCommitment.objects.get(
        student=co_op_student,
        commitment_kind="co_op",
        lifecycle_status="active",
    )
    assert float(co_op_commitment.credit_value) == 2.0
    assert set(
        StudentScheduleCommitmentOccupancy.objects.filter(
            commitment=co_op_commitment,
        ).values_list("timeslot__semester", "timeslot__block", "half_semester_segment")
    ) == {
        (2, "A", "first_half"), (2, "A", "second_half"),
        (2, "B", "first_half"), (2, "B", "second_half"),
    }
    focus_commitments = StudentScheduleCommitment.objects.filter(
        student__in=(focus_one_student, focus_two_student),
        commitment_kind="focus",
        lifecycle_status="active",
    )
    assert focus_commitments.count() == 2
    for commitment, semester in (
        (focus_commitments.get(student=focus_one_student), 1),
        (focus_commitments.get(student=focus_two_student), 2),
    ):
        assert set(
            StudentScheduleCommitmentOccupancy.objects.filter(
                commitment=commitment,
            ).values_list("timeslot__semester", "timeslot__block", "half_semester_segment")
        ) == {
            (semester, block, segment)
            for block in ("A", "B", "C", "D")
            for segment in ("first_half", "second_half")
        }
    pair = HalfSemesterSectionPair.objects.select_related(
        "first_section", "second_section"
    ).get(course_pair__first_course=wellness)
    assert pair.first_section.half_semester_segment == "first_half"
    assert pair.second_section.half_semester_segment == "second_half"
    assert pair.first_section.teacher_id == pair.second_section.teacher_id
    assert SectionSchedule.objects.get(section=pair.first_section).timeslot_id == SectionSchedule.objects.get(
        section=pair.second_section
    ).timeslot_id
    paired_assignments = [
        assignment
        for assignment in review["assignments"]
        if assignment["student_id"] == paired_half_student.id
        and assignment["course_id"] in {wellness.id, arts.id}
    ]
    assert len(paired_assignments) == 2
    assert {item["half_semester_segment"] for item in paired_assignments} == {
        "first_half", "second_half"
    }
    assert len({item["timeslot_id"] for item in paired_assignments}) == 1
    assert not any(
        item["student_id"] == paired_half_student.id
        and item["code"] == "student_assignment_half_semester_unallocated_opposite_half"
        for item in review["special_commitment_review_items"]
    )
    assert any(
        item["code"] == "student_assignment_half_semester_unallocated_opposite_half"
        and item["student_id"] in {first_half_only_student.id, second_half_only_student.id}
        for item in review["special_commitment_review_items"]
    )
    online_half_assignment = next(
        item for item in review["assignments"]
        if item["student_id"] == online_half_student.id
        and item["course_id"] == online_wellness.id
    )
    assert online_half_assignment["half_semester_segment"] == "first_half"
    assert any(
        item["code"] == "student_assignment_online_half_semester_unused_supervision_half"
        and item["student_id"] == online_half_student.id
        and item["request_id"] == request_by_student_course[online_half_student.id, online_wellness.id].id
        for item in review["special_commitment_review_items"]
    )
    assert not any(
        item["code"] == "student_assignment_unallocated_school_time"
        and item["student_id"] == online_half_student.id
        and item["detail"]["timeslot_id"] == online_half_assignment["timeslot_id"]
        for item in review["special_commitment_review_items"]
    )
    unallocated_items = [
        item for item in review["special_commitment_review_items"]
        if item["code"] == "student_assignment_unallocated_school_time"
    ]
    assert any(
        item["student_id"] == normal_student.id
        and item["detail"]["has_requested_study"] is False
        and item["detail"]["recognized_commitment"] is False
        for item in unallocated_items
    )
    assert not any(
        item["student_id"] == focus_one_student.id
        and item["detail"]["semester"] == 1
        for item in unallocated_items
    )
    assert not any(
        item["student_id"] == focus_two_student.id
        and item["detail"]["semester"] == 2
        for item in unallocated_items
    )
    difficulty_by_course = {
        item["course_id"]: item
        for item in review["course_difficulty_facts"]
    }
    assert difficulty_by_course[online_math.id]["category"] == "math"
    assert difficulty_by_course[online_english.id]["category"] == "english"
    assert difficulty_by_course[co_op.id]["category"] == ""
    assert not {
        focus_one_student.id, focus_two_student.id,
    } & {item["student_id"] for item in review["student_difficulty_balance"]}
    assert not Enrollment.objects.filter(
        student__in=(focus_one_student, focus_two_student),
        section__semester=1,
        lifecycle_status="active",
    ).filter(student=focus_one_student).exists()
    assert not Enrollment.objects.filter(
        student=focus_two_student,
        section__semester=2,
        lifecycle_status="active",
    ).exists()
    # The three persisted shapes have different domain meanings, but all occupy
    # the same student-time space.  Verify the approved result does not hide a
    # collision at the boundary between normal, online, and special records.
    occupied_time = defaultdict(list)
    section_timeslots = {
        row.section_id: row.timeslot_id
        for row in SectionSchedule.objects.filter(section__academic_year=academic_year)
    }
    for enrollment in Enrollment.objects.filter(
        section__academic_year=academic_year,
        lifecycle_status="active",
    ).select_related("section"):
        segments = (
            (enrollment.section.half_semester_segment,)
            if enrollment.section.half_semester_segment
            else ("first_half", "second_half")
        )
        for segment in segments:
            occupied_time[enrollment.student_id].append(
                (section_timeslots[enrollment.section_id], segment)
            )
    for enrollment in OnlineEnrollment.objects.filter(
        supervision_session__academic_year=academic_year,
        lifecycle_status="active",
    ).select_related("supervision_session"):
        for segment in ("first_half", "second_half"):
            occupied_time[enrollment.student_id].append(
                (enrollment.supervision_session.timeslot_id, segment)
            )
    for occupancy in StudentScheduleCommitmentOccupancy.objects.filter(
        commitment__academic_year=academic_year,
        commitment__lifecycle_status="active",
    ).select_related("commitment"):
        occupied_time[occupancy.commitment.student_id].append(
            (occupancy.timeslot_id, occupancy.half_semester_segment)
        )
    assert all(
        len(values) == len(set(values))
        for values in occupied_time.values()
    )
    assert all(
        schedule.timeslot_id is not None
        for schedule in SectionSchedule.objects.filter(section__academic_year=academic_year)
    )

    # A scoped rerun reads the approved history and the existing special locks
    # as fixed context. It must neither move the locked Study occupancy nor
    # rewrite the prior approval while the counselor only reviews the rerun.
    rerun = create_student_assignment_run(
        academic_year=academic_year,
        staffing_mode="final_staffing",
        soft_constraint_importance=SOFT_IMPORTANCE,
        created_by=counselor_user,
        scope_type=STUDENT_ASSIGNMENT_RUN_SCOPE_SCOPED,
        source_approval=approval,
        scope_student_ids=(two_study_student.id,),
    )
    assert rerun.status == "complete", rerun.result
    assert {
        item["lock_id"] for item in rerun.input_snapshot["special_commitment_locks"]
    } >= {first_study_lock.id, second_study_lock.id}
    assert set(
        StudentScheduleCommitmentOccupancy.objects.filter(
            commitment__student=two_study_student,
            commitment__commitment_kind="study",
            commitment__lifecycle_status="active",
        ).values_list("timeslot_id", "half_semester_segment")
    ) == locked_study_occupancy
