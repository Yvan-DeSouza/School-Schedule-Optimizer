"""Validate raw scheduling DTOs and compile solver-friendly indexes.

This is the single pure-engine authority for referential validation and teacher
eligibility.  Solvers consume its frozen maps instead of reimplementing joins or
qualification rules.  In particular, Grade 11-12 courses fail closed when a
required normalized qualification rule is absent; Grade 7-10 courses retain the
legally flexible teacher pool defined by the adapter/compiler contract.
"""

from types import MappingProxyType

from .dto import CompiledConstraintSetDTO, SchedulingInputDTO


def _index_unique(items, label):
    """Index DTOs by ``id`` while rejecting ambiguous duplicate identities."""

    indexed = {}
    for item in items:
        if item.id in indexed:
            raise ValueError(f"Duplicate {label} id: {item.id}.")
        indexed[item.id] = item
    return indexed


def _require(value, known, label):
    """Raise a domain-readable error for a broken foreign-key-like reference."""

    if value not in known:
        raise ValueError(f"Unknown {label} id: {value}.")


def _freeze_sets(values):
    """Freeze a mapping and every mutable set it contains."""

    return MappingProxyType({key: frozenset(value) for key, value in values.items()})


def compile_constraints(data: SchedulingInputDTO) -> CompiledConstraintSetDTO:
    """Validate and compile a complete scheduling input into solver-friendly indexes.

    The function is intentionally strict.  Bad adapter data should fail before a
    solver can turn it into a misleading "infeasible" scheduling result.
    """

    # Establish the identity universe first.  Every relationship validated below
    # must point into one of these maps.
    courses = _index_unique(data.courses, "course")
    sections = _index_unique(data.sections, "section")
    students = _index_unique(data.students, "student")
    teachers = _index_unique(data.teachers, "teacher")
    rooms = _index_unique(data.rooms, "room")
    timeslots = _index_unique(data.timeslots, "timeslot")
    qualifications = _index_unique(data.qualifications, "qualification")

    # Demand records must reference known students/courses; is_primary is checked
    # explicitly because truthy strings would otherwise silently distort demand.
    for request in data.course_requests:
        _require(request.student_id, students, "student")
        _require(request.course_id, courses, "course")
        if not isinstance(request.is_primary, bool):
            raise ValueError("Course request is_primary must be a boolean.")
    for record in data.historical_demand:
        _require(record.course_id, courses, "course")

    # Existing sections provide fixed planning context.  A named teacher is
    # optional because newly approved drafts are intentionally unstaffed.
    for section in sections.values():
        _require(section.course_id, courses, "course")
        if section.teacher_id is not None:
            _require(section.teacher_id, teachers, "teacher")
    for timeslot in timeslots.values():
        # Cross-year timeslots would create subtle availability and lock bugs, so
        # reject them rather than filter them silently.
        if timeslot.academic_year_id != data.academic_year_id:
            raise ValueError(f"Timeslot {timeslot.id} is outside the compiled academic year.")

    # Compile normalized credential IDs held by each teacher.  Raw source strings
    # and provenance stay in Django and never participate in matching.
    teacher_qualifications = {teacher_id: set() for teacher_id in teachers}
    seen_teacher_qualifications = set()
    for item in data.teacher_qualifications:
        _require(item.teacher_id, teachers, "teacher")
        _require(item.qualification_id, qualifications, "qualification")
        key = (item.teacher_id, item.qualification_id)
        if key in seen_teacher_qualifications:
            raise ValueError("Duplicate teacher qualification.")
        seen_teacher_qualifications.add(key)
        teacher_qualifications[item.teacher_id].add(item.qualification_id)

    # Required and preferred qualifications are deliberately separated: only the
    # required set may remove a teacher from legal eligibility.
    required_qualifications = {course_id: set() for course_id in courses}
    preferred_qualifications = {course_id: set() for course_id in courses}
    seen_course_qualifications = set()
    for item in data.course_qualification_requirements:
        _require(item.course_id, courses, "course")
        _require(item.qualification_id, qualifications, "qualification")
        key = (item.course_id, item.qualification_id)
        if key in seen_course_qualifications:
            raise ValueError("Duplicate course qualification requirement.")
        seen_course_qualifications.add(key)
        target = required_qualifications if item.is_required else preferred_qualifications
        target[item.course_id].add(item.qualification_id)
    qualified_teachers = {}
    for course_id, course in courses.items():
        required = required_qualifications[course_id]
        if course.requires_statutory_qualification:
            # Senior courses fail closed.  An absent rule is bad configuration,
            # not permission to treat the entire staff as qualified.
            if not required:
                raise ValueError(
                    f"Course {course.course_code} requires a statutory qualification but has no required qualification rule."
                )
            qualified_teachers[course_id] = {
                teacher_id
                for teacher_id, held in teacher_qualifications.items()
                if required <= held
            }
        else:
            # Grade 7-10 qualification matches may influence preferences later,
            # but do not act as a hard legal eligibility barrier.
            qualified_teachers[course_id] = set(teachers)

    # Room-type requirements are set-valued because specialized courses may need
    # multiple capabilities in future placement models.
    required_room_types = {course_id: set() for course_id in courses}
    seen_room_requirements = set()
    for item in data.course_room_requirements:
        _require(item.course_id, courses, "course")
        key = (item.course_id, item.room_type)
        if key in seen_room_requirements:
            raise ValueError("Duplicate course room requirement.")
        seen_room_requirements.add(key)
        required_room_types[item.course_id].add(item.room_type)

    # Availability contains only explicitly available recurring slots.  The
    # downstream placement solver decides how absence of records is interpreted.
    available_by_teacher = {teacher_id: set() for teacher_id in teachers}
    seen_availability = set()
    for item in data.teacher_availability:
        _require(item.teacher_id, teachers, "teacher")
        _require(item.timeslot_id, timeslots, "timeslot")
        key = (item.teacher_id, item.timeslot_id)
        if key in seen_availability:
            raise ValueError("Duplicate teacher availability.")
        seen_availability.add(key)
        if item.is_available:
            available_by_teacher[item.teacher_id].add(item.timeslot_id)

    # Structured course IDs avoid parsing teacher-entered prose in the engine.
    preferences = {teacher_id: set() for teacher_id in teachers}
    seen_preferences = set()
    for item in data.teacher_preferences:
        _require(item.teacher_id, teachers, "teacher")
        _require(item.course_id, courses, "course")
        key = (item.teacher_id, item.course_id)
        if key in seen_preferences:
            raise ValueError("Duplicate teacher course preference.")
        seen_preferences.add(key)
        preferences[item.teacher_id].add(item.course_id)
    # Current-course history is scoped to the target year during compilation so
    # future objective code cannot accidentally reward stale records.
    current_courses = {teacher_id: set() for teacher_id in teachers}
    seen_current_courses = set()
    for item in data.teacher_current_courses:
        _require(item.teacher_id, teachers, "teacher")
        _require(item.course_id, courses, "course")
        key = (item.teacher_id, item.course_id, item.academic_year_id)
        if key in seen_current_courses:
            raise ValueError("Duplicate teacher current course.")
        seen_current_courses.add(key)
        if item.academic_year_id == data.academic_year_id:
            current_courses[item.teacher_id].add(item.course_id)

    # Prerequisites form directed edges.  Self-edges are always invalid even if
    # the database did not happen to enforce that semantic rule.
    prerequisites = {course_id: set() for course_id in courses}
    seen_prerequisites = set()
    for item in data.course_prerequisites:
        _require(item.course_id, courses, "course")
        _require(item.prerequisite_id, courses, "course")
        if item.course_id == item.prerequisite_id:
            raise ValueError("A course cannot be its own prerequisite.")
        key = (item.course_id, item.prerequisite_id)
        if key in seen_prerequisites:
            raise ValueError("Duplicate course prerequisite.")
        seen_prerequisites.add(key)
        prerequisites[item.course_id].add(item.prerequisite_id)

    # Store unordered course pairs under a sorted key so callers can look them up
    # without remembering which course was recorded as A or B.
    conflict_weights = {}
    for item in data.course_conflicts:
        _require(item.course_a_id, courses, "course")
        _require(item.course_b_id, courses, "course")
        if item.course_a_id == item.course_b_id or item.weight < 0:
            raise ValueError("Course conflicts require distinct courses and a non-negative weight.")
        key = tuple(sorted((item.course_a_id, item.course_b_id)))
        if key in conflict_weights:
            raise ValueError("Duplicate unordered course conflict.")
        conflict_weights[key] = item.weight

    # Locks are loaded as immutable context.  A locked teacher is revalidated
    # against required qualifications so stale data cannot bypass legal rules.
    locks = {}
    for lock in data.section_locks:
        _require(lock.section_id, sections, "section")
        if lock.section_id in locks:
            raise ValueError("Duplicate section lock.")
        if lock.locked_teacher_id is not None:
            _require(lock.locked_teacher_id, teachers, "teacher")
            section = sections[lock.section_id]
            if not required_qualifications[section.course_id] <= teacher_qualifications[lock.locked_teacher_id]:
                raise ValueError("Locked teacher lacks a required course qualification.")
        if lock.locked_timeslot_id is not None:
            _require(lock.locked_timeslot_id, timeslots, "timeslot")
        if lock.locked_room_id is not None:
            _require(lock.locked_room_id, rooms, "room")
        locks[lock.section_id] = lock

    # Constraint metadata is compiled separately from domain relationships.  A
    # duplicate hard type would make precedence ambiguous and is rejected.
    hard_priorities = {}
    for item in data.hard_constraints:
        if item.type in hard_priorities:
            raise ValueError(f"Duplicate hard constraint type: {item.type}.")
        hard_priorities[item.type] = item.priority
    soft_weights = _index_unique(data.soft_constraints, "soft constraint")
    # Counselor weights are keyed by both counselor and constraint; this retains
    # the source decision without collapsing different counselors together.
    counselor_weights = {}
    for item in data.counselor_constraint_preferences:
        _require(item.soft_constraint_id, soft_weights, "soft constraint")
        key = (item.counselor_id, item.soft_constraint_id)
        if key in counselor_weights:
            raise ValueError("Duplicate counselor constraint preference.")
        counselor_weights[key] = item.weight

    # MappingProxyType/frozenset make the returned compiler product read-only.
    # This is important because multiple solver stages may share one input.
    return CompiledConstraintSetDTO(
        academic_year_id=data.academic_year_id,
        course_by_id=MappingProxyType(courses),
        section_by_id=MappingProxyType(sections),
        qualified_teacher_ids_by_course=_freeze_sets(qualified_teachers),
        available_timeslot_ids_by_teacher=_freeze_sets(available_by_teacher),
        preferred_course_ids_by_teacher=_freeze_sets(preferences),
        current_course_ids_by_teacher=_freeze_sets(current_courses),
        required_room_types_by_course=_freeze_sets(required_room_types),
        required_qualification_ids_by_course=_freeze_sets(required_qualifications),
        preferred_qualification_ids_by_course=_freeze_sets(preferred_qualifications),
        prerequisite_ids_by_course=_freeze_sets(prerequisites),
        conflict_weights_by_course_pair=MappingProxyType(conflict_weights),
        locked_sections_by_id=MappingProxyType(locks),
        available_room_ids=frozenset(rooms),
        available_timeslot_ids=frozenset(item.id for item in timeslots.values() if item.is_available),
        hard_constraint_priorities=MappingProxyType(hard_priorities),
        soft_constraint_weights=MappingProxyType({item.id: item.default_weight for item in soft_weights.values()}),
        counselor_constraint_weights=MappingProxyType(counselor_weights),
    )
