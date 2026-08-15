"""Factual named-teacher candidate evidence from one completed CP-SAT solve.

This module never builds another model or identifies placement's anonymous
staffing witness. Its candidate pool is only the real ready roster passed to
the named-teacher assignment stage.
"""

from __future__ import annotations

from collections import defaultdict

from .diagnostics import (
    TEACHER_ANNUAL_CAPACITY_SHORTAGE,
    TEACHER_ASSIGNMENT_ANNUAL_CAPACITY_EXHAUSTED,
    TEACHER_ASSIGNMENT_COURSE_RULE_MAXIMUM_REACHED,
    TEACHER_ASSIGNMENT_EXACT_TEACHER_LOCKED_ELSEWHERE,
    TEACHER_ASSIGNMENT_QUALIFICATION_UNAVAILABLE,
    TEACHER_ASSIGNMENT_SEMESTER_CAPACITY_EXHAUSTED,
    TEACHER_ASSIGNMENT_TEACHER_UNAVAILABLE,
    TEACHER_SEMESTER_CAPACITY_SHORTAGE,
    TEACHER_TIMESLOT_COLLISION,
)
from .dto import TeacherAssignmentCandidateLedgerDTO


def _section_key(section):
    return (
        "online_supervision", section.online_supervision_session_id
    ) if section.is_online_supervision else ("section", section.section_id)


def _decision_units(sections):
    """Group sequential half-semester sections into their one staffing decision."""

    pairs = defaultdict(list)
    units = []
    for section in sections:
        if section.shared_staffing_key:
            pairs[section.shared_staffing_key].append(section)
        else:
            units.append((section,))
    units.extend(tuple(sorted(pair, key=_section_key)) for _key, pair in sorted(pairs.items()))
    return tuple(sorted(units, key=lambda unit: tuple(_section_key(item) for item in unit)))


def _rejection(code, *, phase, detail=None):
    return {
        "code": code,
        "phase": phase,
        **({"detail": detail} if detail else {}),
    }


def _candidate_static_rejections(*, unit, teacher, data, fixed_course_counts):
    """Record pre-solve facts without changing the solver's candidate domain."""

    rejections = []
    for section in unit:
        if section.locked_teacher_id is not None and teacher.id != section.locked_teacher_id:
            rejections.append(_rejection(
                TEACHER_ASSIGNMENT_EXACT_TEACHER_LOCKED_ELSEWHERE,
                phase="static",
                detail={"locked_teacher_id": section.locked_teacher_id},
            ))
        if section.timeslot_id in teacher.unavailable_timeslot_ids:
            rejections.append(_rejection(
                TEACHER_ASSIGNMENT_TEACHER_UNAVAILABLE,
                phase="static",
                detail={"timeslot_id": section.timeslot_id},
            ))
        if (
            not section.is_online_supervision
            and not set(section.member_course_ids).issubset(teacher.eligible_course_ids)
        ):
            rejections.append(_rejection(
                TEACHER_ASSIGNMENT_QUALIFICATION_UNAVAILABLE,
                phase="static",
                detail={"member_course_ids": tuple(section.member_course_ids)},
            ))
        if section.semester == 1 and teacher.remaining_semester_1 <= 0:
            rejections.append(_rejection(TEACHER_SEMESTER_CAPACITY_SHORTAGE, phase="static"))
        if section.semester == 2 and teacher.remaining_semester_2 <= 0:
            rejections.append(_rejection(TEACHER_SEMESTER_CAPACITY_SHORTAGE, phase="static"))
        if teacher.remaining_annual <= 0:
            rejections.append(_rejection(TEACHER_ANNUAL_CAPACITY_SHORTAGE, phase="static"))
        for rule in data.rules:
            if rule.teacher_id != teacher.id or rule.maximum_sections is None:
                continue
            if rule.course_id in section.member_course_ids and fixed_course_counts[
                teacher.id, rule.course_id
            ] >= rule.maximum_sections:
                rejections.append(_rejection(
                    TEACHER_ASSIGNMENT_COURSE_RULE_MAXIMUM_REACHED,
                    phase="static",
                    detail={"course_id": rule.course_id, "maximum_sections": rule.maximum_sections},
                ))
    unique = []
    seen = set()
    for rejection in rejections:
        key = (rejection["code"], tuple(sorted(rejection.get("detail", {}).items())))
        if key not in seen:
            seen.add(key)
            unique.append(rejection)
    return tuple(unique)


def build_teacher_assignment_candidate_ledger(*, data, assignments, has_solution):
    """Build alternative evidence from one returned model state; never re-solve.

    An eligible unselected teacher remains only *possible in isolation*. The
    completed model did not force that alternative, so this ledger explicitly
    avoids presenting a global impossibility claim without a separate check.
    """

    fixed_course_counts = defaultdict(int)
    for fixed in data.fixed_assignments:
        for course_id in fixed.member_course_ids:
            fixed_course_counts[fixed.teacher_id, course_id] += 1
    assignment_by_key = {
        ("online_supervision", item.online_supervision_session_id)
        if item.online_supervision_session_id is not None else ("section", item.section_id): item
        for item in assignments
    }
    selected_by_teacher_slot = defaultdict(list)
    selected_by_teacher_semester = defaultdict(list)
    selected_by_teacher_annual = defaultdict(list)
    selected_course_counts = defaultdict(int)
    # The ready roster is already the deliberately bounded school-level
    # candidate pool.  Record every roster member for each true solver
    # decision instead of silently truncating a counselor's alternatives.
    # Fixed staffing rows are retained below as fixed-context entries, not
    # treated as a new named-teacher decision by this run.
    units = _decision_units(data.sections)
    for unit in units:
        selected_ids = {
            assignment_by_key[_section_key(section)].teacher_id
            for section in unit
            if _section_key(section) in assignment_by_key
        }
        if len(selected_ids) != 1:
            continue
        teacher_id = next(iter(selected_ids))
        representative = unit[0]
        selected_by_teacher_slot[teacher_id, representative.timeslot_id].append(unit)
        selected_by_teacher_semester[teacher_id, representative.semester].append(unit)
        selected_by_teacher_annual[teacher_id].append(unit)
        for section in unit:
            for course_id in section.member_course_ids:
                selected_course_counts[teacher_id, course_id] += 1

    fixed_slots = {(item.teacher_id, item.timeslot_id) for item in data.fixed_assignments}
    ledger = []
    for unit in units:
        representative = unit[0]
        section_ids = tuple(section.section_id for section in unit if section.section_id is not None)
        fixed_teacher_ids = {section.assigned_teacher_id for section in unit if section.is_fixed}
        if fixed_teacher_ids:
            # The adapter guarantees that an accepted half-semester pair has
            # one shared teacher.  Do not present roster comparisons here:
            # this run inherited a fixed fact rather than selecting it.
            if len(fixed_teacher_ids) != 1 or not all(section.is_fixed for section in unit):
                raise ValueError("A named-teacher evidence unit cannot mix fixed and movable staffing.")
            ledger.append(TeacherAssignmentCandidateLedgerDTO(
                decision_kind=(
                    "online_supervision" if representative.is_online_supervision
                    else "half_semester_pair" if representative.shared_staffing_key
                    else "section"
                ),
                section_ids=section_ids,
                online_supervision_session_id=representative.online_supervision_session_id,
                shared_staffing_key=representative.shared_staffing_key,
                semester=representative.semester,
                timeslot_id=representative.timeslot_id,
                selection_state="fixed_context",
                selected_teacher_id=next(iter(fixed_teacher_ids)),
                candidates=(),
                selection_factors=({
                    "kind": "fixed_context",
                    "reason": "accepted_named_teacher_assignment",
                },),
            ))
            continue
        selected_ids = {
            assignment_by_key[_section_key(section)].teacher_id
            for section in unit
            if _section_key(section) in assignment_by_key
        }
        selected_teacher_id = next(iter(selected_ids)) if len(selected_ids) == 1 else None
        candidates = []
        for teacher in sorted(data.teachers, key=lambda item: item.id):
            static_rejections = _candidate_static_rejections(
                unit=unit, teacher=teacher, data=data, fixed_course_counts=fixed_course_counts,
            )
            final_rejections = []
            if has_solution and not static_rejections and teacher.id != selected_teacher_id:
                if (
                    (teacher.id, representative.timeslot_id) in fixed_slots
                    or selected_by_teacher_slot[teacher.id, representative.timeslot_id]
                ):
                    final_rejections.append(_rejection(TEACHER_TIMESLOT_COLLISION, phase="final"))
                remaining_semester = (
                    teacher.remaining_semester_1 if representative.semester == 1
                    else teacher.remaining_semester_2
                )
                if len(selected_by_teacher_semester[teacher.id, representative.semester]) >= remaining_semester:
                    final_rejections.append(_rejection(
                        TEACHER_ASSIGNMENT_SEMESTER_CAPACITY_EXHAUSTED, phase="final",
                    ))
                if len(selected_by_teacher_annual[teacher.id]) >= teacher.remaining_annual:
                    final_rejections.append(_rejection(
                        TEACHER_ASSIGNMENT_ANNUAL_CAPACITY_EXHAUSTED, phase="final",
                    ))
                for rule in data.rules:
                    if rule.teacher_id != teacher.id or rule.maximum_sections is None:
                        continue
                    if any(rule.course_id in section.member_course_ids for section in unit) and (
                        fixed_course_counts[teacher.id, rule.course_id]
                        + selected_course_counts[teacher.id, rule.course_id]
                        >= rule.maximum_sections
                    ):
                        final_rejections.append(_rejection(
                            TEACHER_ASSIGNMENT_COURSE_RULE_MAXIMUM_REACHED,
                            phase="final",
                            detail={"course_id": rule.course_id, "maximum_sections": rule.maximum_sections},
                        ))
            candidates.append({
                "teacher_id": teacher.id,
                "is_statically_eligible": not static_rejections,
                "qualification_evaluation": (
                    "not_applicable_online_supervision"
                    if representative.is_online_supervision else "required"
                ),
                "static_rejections": static_rejections,
                "final_rejections": tuple(final_rejections),
                "is_selected": teacher.id == selected_teacher_id,
                "comparison_state": (
                    "selected" if teacher.id == selected_teacher_id else (
                        "statically_ineligible" if static_rejections else (
                            "blocked_by_returned_solution" if final_rejections else
                            "possible_in_isolation_global_comparison_not_yet_proven"
                        )
                    )
                ),
            })
        selected_teacher = next(
            (teacher for teacher in data.teachers if teacher.id == selected_teacher_id), None,
        )
        selection_factors = () if selected_teacher is None else ({
            "kind": "factual_soft_evidence",
            "requested_course_match": bool(
                set(representative.member_course_ids) & set(selected_teacher.preferred_course_ids)
            ),
            "prior_year_course_match": bool(
                set(representative.member_course_ids) & set(selected_teacher.prior_year_course_ids)
            ),
            "timeslot_preference": (
                "preferred" if representative.timeslot_id in selected_teacher.preferred_timeslot_ids
                else "avoid" if representative.timeslot_id in selected_teacher.avoided_timeslot_ids
                else "neutral"
            ),
            "seniority": selected_teacher.seniority,
        },)
        ledger.append(TeacherAssignmentCandidateLedgerDTO(
            decision_kind=(
                "online_supervision" if representative.is_online_supervision
                else "half_semester_pair" if representative.shared_staffing_key
                else "section"
            ),
            section_ids=section_ids,
            online_supervision_session_id=representative.online_supervision_session_id,
            shared_staffing_key=representative.shared_staffing_key,
            semester=representative.semester,
            timeslot_id=representative.timeslot_id,
            selection_state=(
                "selected" if selected_teacher_id is not None
                else "no_solver_incumbent" if not has_solution else "unassigned"
            ),
            selected_teacher_id=selected_teacher_id,
            candidates=tuple(candidates),
            selection_factors=selection_factors,
        ))
    return tuple(ledger)
