"""Lossless detached research-checkpoint contracts (no Django/database)."""

import json
import subprocess
import sys

from scheduling_engine.dto import (
    CourseCategoryRelationshipDTO,
    CourseDifficultyDTO,
    OnlineSupervisionSessionDTO,
    StudentAssignmentInputDTO,
    StudentAssignmentRequestDTO,
    StudentAssignmentSectionDTO,
    StudentAssignmentResultDTO,
    StudentScheduleCommitmentRequestDTO,
    TimeSlotDTO,
)
from scheduling_engine.student_assignment import core
from scheduling_engine.student_assignment.quality import evaluate_student_assignment_quality
from scheduling_engine.student_assignment.research_artifacts import (
    FROZEN_INPUT_SCHEMA,
    VALIDATED_STAGE1_SEED_SCHEMA,
    load_frozen_student_assignment_input,
    load_validated_stage1_seed,
    serialize_frozen_student_assignment_input,
    serialize_validated_stage1_seed,
    validate_loaded_stage1_seed,
)
from scheduling_engine.student_assignment.runtime import semantic_student_assignment_input_fingerprint


def _fixture():
    slots = tuple(
        TimeSlotDTO(semester * 10 + index, 1, semester, block, True)
        for semester in (1, 2)
        for index, block in enumerate(("A", "B", "C", "D"), start=1)
    )
    return StudentAssignmentInputDTO(
        academic_year_id=1,
        requests=(
            StudentAssignmentRequestDTO(1, 1, 1, 101, True, True, 1,
                duration="half_semester", credit_value=.5,
                half_semester_segment="first_half", paired_half_course_id=2),
            StudentAssignmentRequestDTO(2, 1, 2, 102, True, True, 1,
                duration="half_semester", credit_value=.5,
                half_semester_segment="second_half", paired_half_course_id=1),
            StudentAssignmentRequestDTO(3, 2, 3, 103, True, True, 1,
                delivery_kind="online"),
            StudentAssignmentRequestDTO(4, 5, 4, 104, True, True, 1,
                delivery_kind="co_op", credit_value=2.0),
        ),
        sections=(
            StudentAssignmentSectionDTO(1, 1, (101,), (1,), 1, 11, 2, 2,
                half_semester_segment="first_half", half_semester_pair_key="pair-1"),
            StudentAssignmentSectionDTO(2, 2, (102,), (2,), 1, 11, 2, 2,
                half_semester_segment="second_half", half_semester_pair_key="pair-1"),
        ),
        fixed_enrollments=(), hard_prerequisites=(), soft_sequence_preferences=(),
        section_utilization_balance_importance="important",
        student_semester_balance_importance="important",
        course_sequence_preferences_importance="important",
        course_difficulties=(
            CourseDifficultyDTO(1, "language", 50, None, 50, "test"),
            CourseDifficultyDTO(2, "language", 50, None, 50, "test"),
            CourseDifficultyDTO(3, "science", 60, None, 60, "test"),
            CourseDifficultyDTO(4, "other", 0, None, 0, "test"),
        ),
        course_category_relationships=(
            CourseCategoryRelationshipDTO("language", "science", 0),
        ),
        difficulty_balance_importance="important",
        course_category_diversity_importance="important",
        online_supervision_sessions=(
            OnlineSupervisionSessionDTO(9, 1, 12, 4, 4),
        ),
        schedule_commitment_requests=(
            StudentScheduleCommitmentRequestDTO(5, 3, "study"),
            StudentScheduleCommitmentRequestDTO(6, 4, "focus"),
        ),
        timeslots=slots,
        objective_semantics_version="v2",
        objective_importance_scores={
            "section_utilization_balance": 6,
            "student_semester_balance": 6,
            "course_sequence_preferences": 6,
            "difficulty_balance": 6,
            "course_category_diversity": 6,
        },
        student_grades=((1, 10), (2, 12), (3, 12), (4, 11), (5, 12)),
        time_limit_seconds=5.0,
    )


def _stage1(data):
    return core._solve_student_assignment(
        data,
        include_lock_costs=False,
        include_candidate_ledger=False,
        use_hard_feasibility_bootstrap=True,
        local_only=True,
        hard_feasibility_time_limit_seconds=5.0,
        hard_feasibility_worker_count=1,
        hard_feasibility_validation_time_limit_seconds=5.0,
        hard_feasibility_validation_worker_count=1,
    )


def _solvable_fixture():
    """Small complete v2 input for seed replay; rich DTO coverage is above."""

    return StudentAssignmentInputDTO(
        academic_year_id=1,
        requests=(StudentAssignmentRequestDTO(1, 1, 1, 101, True, True, 1),),
        sections=(StudentAssignmentSectionDTO(1, 1, (101,), (1,), 1, 11, 2, 2),),
        fixed_enrollments=(), hard_prerequisites=(), soft_sequence_preferences=(),
        section_utilization_balance_importance="important",
        student_semester_balance_importance="important",
        course_sequence_preferences_importance="important",
        course_difficulties=(CourseDifficultyDTO(1, "language", 50, None, 50, "test"),),
        course_category_relationships=(),
        difficulty_balance_importance="important",
        course_category_diversity_importance="important",
        schedule_commitment_requests=(
            StudentScheduleCommitmentRequestDTO(2, 2, "study"),
        ),
        timeslots=(
            TimeSlotDTO(11, 1, 1, "A", True),
            TimeSlotDTO(12, 1, 1, "B", True),
            TimeSlotDTO(13, 1, 1, "C", True),
            TimeSlotDTO(14, 1, 1, "D", True),
            TimeSlotDTO(21, 1, 2, "A", True),
        ),
        objective_semantics_version="v2",
        objective_importance_scores={
            "section_utilization_balance": 6,
            "student_semester_balance": 6,
            "course_sequence_preferences": 6,
            "difficulty_balance": 6,
            "course_category_diversity": 6,
        },
        student_grades=((1, 10), (2, 12)),
        time_limit_seconds=5.0,
    )


def test_frozen_input_round_trip_is_lossless_and_semantically_identical(tmp_path):
    data = _fixture()
    path = tmp_path / "input.json"
    written = serialize_frozen_student_assignment_input(path, data=data, provenance={"attempt": "test"})
    loaded = load_frozen_student_assignment_input(
        path, expected_input_fingerprint=written["input_semantic_fingerprint"]
    )
    assert written["schema"] == FROZEN_INPUT_SCHEMA
    assert loaded["data"] == data
    assert loaded["input_semantic_fingerprint"] == semantic_student_assignment_input_fingerprint(data)


def test_validated_seed_round_trip_replays_canonically_and_scores_offline(tmp_path):
    data = _solvable_fixture()
    result = _stage1(data)
    assert result.status == "complete"
    assert not result.unmet_requests
    input_path = tmp_path / "input.json"
    frozen = serialize_frozen_student_assignment_input(input_path, data=data)
    loaded_input = load_frozen_student_assignment_input(input_path)["data"]
    seed_path = tmp_path / "seed.json"
    written = serialize_validated_stage1_seed(
        seed_path, data=data, result=result, independent_full_model_validation=True,
        provenance={"validation": "test"},
    )
    loaded_seed = load_validated_stage1_seed(
        seed_path,
        data=loaded_input,
        expected_input_fingerprint=frozen["input_semantic_fingerprint"],
    )
    assert written["schema"] == VALIDATED_STAGE1_SEED_SCHEMA
    assert written["counts"]["assignment_count"] == len(loaded_seed["assignments"])
    replay = validate_loaded_stage1_seed(loaded_input, loaded_seed, time_limit_seconds=5.0, worker_count=1)
    assert replay["valid"] is True
    assert replay["assignment_count"] == len(loaded_seed["assignments"])
    first = evaluate_student_assignment_quality(
        loaded_input,
        assignments=loaded_seed["assignments"],
        commitment_assignments=loaded_seed["commitment_assignments"],
    )
    second = evaluate_student_assignment_quality(
        loaded_input,
        assignments=loaded_seed["assignments"],
        commitment_assignments=loaded_seed["commitment_assignments"],
    )
    assert first == second
    assert first["objective_semantics"]["version"] == "v2"


def test_unvalidated_or_incomplete_result_cannot_create_seed_artifact(tmp_path):
    data = _solvable_fixture()
    incomplete = StudentAssignmentResultDTO(
        status="failed", solver_outcome="unknown", assignments=(), unmet_requests=(),
        diagnostics=(), objective_components={}, sequence_outcomes=(),
    )
    try:
        serialize_validated_stage1_seed(
            tmp_path / "must-not-exist.json", data=data, result=incomplete,
        )
    except ValueError as error:
        assert "validated" in str(error)
    else:
        raise AssertionError("Incomplete result unexpectedly created a validated seed")
    assert not (tmp_path / "must-not-exist.json").exists()


def test_fresh_process_verifies_only_persisted_artifacts(tmp_path):
    data = _solvable_fixture()
    result = _stage1(data)
    input_path = tmp_path / "input.json"
    seed_path = tmp_path / "seed.json"
    serialize_frozen_student_assignment_input(input_path, data=data)
    serialize_validated_stage1_seed(
        seed_path, data=data, result=result, independent_full_model_validation=True,
    )
    completed = subprocess.run(
        [
            sys.executable, "-m",
            "scheduling_engine.student_assignment.verify_research_artifacts",
            str(input_path), str(seed_path),
        ],
        capture_output=True, text=True, check=False,
    )
    assert completed.returncode == 0, completed.stderr
    facts = json.loads(completed.stdout)
    assert facts["artifact_only"] is True
    assert facts["independent_full_model_validation"] is True
    assert facts["objective_v2_deterministic"] is True
