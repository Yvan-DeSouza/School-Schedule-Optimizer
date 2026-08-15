# Naming Conventions

This document defines naming conventions for the backend, scheduling engine,
API contract, documentation, tests, and future frontend.

Existing names are preserved unless explicitly changed through a reviewable
rename. New code follows these rules.

## General principles

- Prefer clear, descriptive names over unexplained abbreviations.
- Use one canonical name for each domain concept.
- Stable API-facing codes must not be renamed casually.
- Naming changes to Django model fields are schema changes and must follow the
  project's migrationless development policy.
- Human-readable text may change; machine-readable names and codes are stable
  contracts.

## Python naming

### Modules and packages

Use lowercase `snake_case`.

Examples:

- `section_placement.py`
- `teacher_assignment.py`
- `demand_analyzer.py`

Private helpers may begin with one underscore, for example
`_eligible_teachers` and `_build_annual_model`.

Django app and package directories use lowercase names:

- `backend/apps/courses`
- `backend/apps/scheduling`
- `backend/apps/constraints`

### Functions and variables

Use lowercase `snake_case`.

```python
def plan_section_counts(...):
    recommended_count = 3
```

Use descriptive names rather than unexplained abbreviations. Domain-standard
acronyms such as `DTO`, `API`, and `ID` may remain uppercase in class names
or type names.

### Classes

Use PascalCase.

```python
class SectionPlacementRun:
    ...
```

Django model classes, serializers, views, services, policies, DTOs, and
exceptions all follow PascalCase. Examples include `Course`,
`SectionPlacementRun`, `TeacherAssignmentSerializer`,
`SectionPlacementValidationError`, and `PlacementInputDTO`.

### Constants

Use `UPPER_SNAKE_CASE` for module-level constants.

```python
SECTION_LIFECYCLE_ACTIVE = "active"
NO_AVAILABLE_TIMESLOT = "no_available_timeslot"
```

Constants belong in the domain module that owns them:

- school-wide values: `backend/apps/common/school_values.py`
- role values: `backend/apps/people/constants.py`
- course values: `backend/apps/courses/constants.py`
- qualification values: `backend/apps/constraints/constants.py`
- planning values: `backend/apps/scheduling/constants.py`
- solver diagnostics: `scheduling_engine/diagnostics.py`
- pure scheduling-engine values shared by engine modules:
  `scheduling_engine/constants.py`

`backend/apps/common/constants.py` is a compatibility export only. New code
should not add authoritative definitions there.

`scheduling_engine/constants.py` must remain Django-free. It owns shared
engine vocabulary only; Django-owned domain values remain in their owning app.

Local constants are allowed when they are truly private to one module, but
shared values must not be duplicated across modules.

### Linting

The repository currently has no Ruff, Flake8, or pre-commit configuration.
When Python linting is introduced, the recommended starting rule is Ruff's
pycodestyle-naming rule family:

```toml
[tool.ruff.lint]
select = ["N"]
```

This should be added only alongside the project's chosen Ruff dependency and
CI command. Any existing intentional exceptions should be configured
explicitly and reviewed individually; broad per-file ignores should not be
used to hide naming drift.

## Django naming

### Apps

Django app directories use lowercase names.

AppConfig classes use PascalCase and end in `Config`:

```python
class CoursesConfig(AppConfig):
    name = "backend.apps.courses"
```

New apps should use their full repository import path in `AppConfig.name`.
The existing `backend/apps/core/apps.py` configuration is a legacy exception.

### Models

Model classes use singular PascalCase:

```python
class CourseOffering(models.Model):
    ...
```

Model fields use lowercase `snake_case`:

```text
course_code
academic_year
is_archived
created_at
```

Foreign-key fields use the related concept name:

```python
teacher = models.ForeignKey(...)
```

Django's default database column is then `teacher_id`. Do not add
`db_column` overrides unless there is an explicit integration requirement.
Do not add `db_table` overrides without a documented reason.

### Boolean fields

New BooleanField names should normally use a clear predicate prefix:

- `is_...`
- `has_...`
- `can_...`
- `should_...`
- `uses_...`
- `requires_...`

Examples:

```python
is_archived
is_locked
has_prerequisite
requires_statutory_qualification
```

Existing names such as `is_reduced_load` and
`uses_current_demand_fallback` remain valid legacy names. New fields should
avoid vague names such as `flag`, `value`, or `enabled` unless the meaning
is unambiguous.

### Relationships

Use singular field names for forward relationships and plural `related_name`
values for collections:

```python
teacher = models.ForeignKey(
    ...,
    related_name="planning_capacities",
)
```

Related names use lowercase `snake_case`.

### Services

Service modules use lowercase `snake_case` and should describe the workflow:

- `section_planning.py`
- `section_reconciliation.py`
- `teacher_assignment.py`
- `teacher_directory.py`

Services belong under an app's `services/` package when the app has multiple
workflows. A single `services.py` module is acceptable for a small domain.

Service functions use verb-oriented `snake_case` names:

```python
approve_section_plan(...)
reconcile_sections(...)
review_teacher_qualification(...)
```

Service classes, when needed, use PascalCase and should represent a domain
workflow or error type rather than acting as a generic utility container.

### Selectors

Reusable query logic belongs in selector modules.

Use `selectors.py` for a small app-level selector surface. Use
`selectors/<domain>.py` only when the selector surface becomes large enough to
require multiple modules.

Selector functions use descriptive `snake_case` names:

```python
active_sections_for_year(...)
course_offerings_for_year(...)
```

Views and services should not duplicate reusable queryset rules.

## API and JSON naming

### JSON keys

The API should remain `snake_case`.

Django/Python field names map directly to JSON keys by default:

```text
academic_year  ->  academic_year
course_code    ->  course_code
is_available   ->  is_available
```

DRF serializers should not automatically transform keys to camelCase. This
preserves the existing serializer contract, avoids unnecessary translation at
every endpoint, keeps backend and API names aligned, and supports the existing
principle that clients depend on stable machine contracts rather than
presentation wording.

Explicit serializer fields may expose a clearer API name when the underlying
model relationship requires it, but that name must still use `snake_case`.

```python
course_code = serializers.CharField(
    source="course.course_code",
    read_only=True,
)
```

### Future frontend boundary

The wire format remains `snake_case`.

A future TypeScript frontend may use `camelCase` for local variables, functions,
component props, and view-model fields. If local camelCase models are useful,
conversion must happen explicitly in an API adapter or mapper:

```text
API response:  teacher_assignment_run
UI model:      teacherAssignmentRun
```

There must be no hidden global key transformation. API DTO types should mirror
the actual wire format, while explicit mapper functions own any UI-specific
translation.

## Stable diagnostic, action, and status codes

### General format

Public machine-readable codes use lowercase ASCII `snake_case`.

They must:

- contain no spaces;
- contain no punctuation other than underscores;
- avoid human-readable sentences;
- remain stable after clients depend on them;
- be unique across the public API contract.

### Diagnostics

New diagnostic codes should use:

```text
<domain_or_stage>_<condition>
```

Examples:

```text
section_placement_no_available_timeslot
teacher_assignment_annual_capacity_shortage
student_assignment_prerequisite_missing
```

The existing diagnostic values in `scheduling_engine/diagnostics.py` are
grandfathered. They must not be renamed solely to add a prefix, because tests
and future clients may already depend on them.

New diagnostics must be defined once in the owning diagnostic module and then
imported by services and views. Services must not introduce duplicate literal
definitions.

### Actions

Action names use:

```text
<verb>_<domain_or_stage>[_<object>]
```

Examples:

```text
run_section_placement
approve_teacher_assignment
view_scheduling_run_status
```

Action names are defined as constants in the owning action-policy module and
must not be repeated as unrelated string literals.

### Status values

Status values use lowercase `snake_case` and are defined by the owning domain
choice tuple or constants module.

Existing field-scoped statuses such as `draft`, `ready`, `complete`,
`partial`, `infeasible`, `failed`, `active`, and `retired` remain valid.
Status values do not need redundant prefixes when the field and resource
already provide the namespace. If a status is exposed as a cross-resource
standalone code, it should use the domain/stage prefix format.

### Code ownership and uniqueness

Every public code must have:

1. one canonical symbolic definition;
2. one stable string value;
3. tests asserting the value where it is part of the API contract;
4. documentation when frontend behavior depends on it.

Existing hard-coded workflow codes should be migrated individually into
domain-specific code modules through reviewable changes. They must not be
mass-renamed or silently changed.

## Documentation filenames

### Decision records

All files under `docs/decisions/` use lowercase kebab-case:

```text
section-lifecycle.md
named-teacher-assignment.md
semester-placement-and-staffing-feasibility.md
```

New decision records must follow this format.

### Top-level documentation

Existing top-level documentation filenames are preserved. New top-level
normative convention documents use uppercase `SNAKE_CASE.md`, matching files
such as `NAMING_CONVENTIONS.md` and `PROJECT_STATE_SUMMARY.md`.

No documentation rename is implied by this document.

## Tests

### Test files

Pytest-discovered test modules use:

```text
test_<domain_or_behavior>.py
```

Examples:

```text
test_section_planning_api.py
test_teacher_assignment.py
test_engine_boundary_contracts.py
```

This remains consistent with `pytest.ini`'s `python_files = test_*.py`.

### Test functions

Test functions use:

```python
def test_<behavior_in_snake_case>():
    ...
```

Names should describe observable behavior rather than implementation details.

### Fixtures and helpers

Fixtures and helper functions remain lowercase `snake_case` but do not require
the `test_` prefix. Examples include `backend/tests/conftest.py`,
`backend/tests/factories.py`, and `scheduling_engine/tests/factories.py`.

If test classes are introduced, use PascalCase beginning with `Test`:

```python
class TestSectionPlacement:
    ...
```

Test methods still use `test_...` plus `snake_case`.

## Frontend convention

The frontend is not yet implemented. The following convention is pre-registered
for a future TypeScript/React frontend.

### Frontend symbols

- variables and functions: `camelCase`;
- boolean variables: `isX`, `hasX`, `canX`, `shouldX`, or `usesX`;
- React components: `PascalCase`;
- hooks: `useCamelCase`;
- TypeScript interfaces and types: `PascalCase`;
- module constants: `UPPER_SNAKE_CASE`;
- API DTO property names: preserve wire-format `snake_case`.

### Frontend files and directories

- feature and utility directories: lowercase kebab-case;
- non-component files: lowercase kebab-case;
- React component files: PascalCase;
- test files: `<name>.test.ts` or `<name>.test.tsx`.

Examples:

```text
src/features/section-planning/
src/api/section-placement-client.ts
src/api/mappers/teacher-assignment-mapper.ts
src/components/SectionPlacementReview.tsx
src/hooks/useSectionPlacementRun.ts
src/features/section-planning/SectionPlacementReview.test.tsx
```

Frontend code should use explicit API client modules and mapper functions.
Components should not contain ad hoc snake_case/camelCase conversions.

## Current inconsistencies and reviewable fixes

This section records the audit findings and the disposition of each of the six
reviewable items. Completed items preserve all existing public string values
unless explicitly noted otherwise.

### 1. Workflow-code centralization — done

Domain-owned constants modules now exist at:

- `backend/apps/constraints/codes.py`;
- `backend/apps/control/codes.py`;
- `backend/apps/courses/codes.py`; and
- `backend/apps/scheduling/codes.py`.

The relevant views and service call sites import workflow codes from those
modules. Scheduling codes shared by section planning and reconciliation have a
single definition in `backend/apps/scheduling/codes.py`.

### 2. Duplicate diagnostic values — done

Backend services now import the existing diagnostic constants from
`scheduling_engine/diagnostics.py`. The stable values, including
`annual_lock_outside_annual_count`, `placement_input_changed_since_run`, and
`teacher_assignment_input_changed_since_run`, were not changed.

### 3. README constants documentation — done

`README.md` now identifies `backend/apps/common/school_values.py` and the
domain-owned constants modules as authoritative, and describes
`backend/apps/common/constants.py` as a compatibility export.

### 4. Boolean naming exceptions — done

The approved boolean renames are complete: `reduced_load` is now
`is_reduced_load`, and `excluded` is now `is_excluded`. The model, serializer,
engine adapter, DTO, planning-core lookup, and supporting documentation were
updated together. No migration file was created; the local database must be
recreated under the project's migrationless `run-syncdb` workflow before using
the renamed model column.

### 5. Core app path configuration — done

Repository-wide checks found no dependency on the old app name. The app
configuration now uses `name = "backend.apps.core"` in
`backend/apps/core/apps.py`; settings, migrations, imports, content-type
lookups, permissions, fixtures, and hardcoded app-label references required no
other changes.

### 6. Documentation filename split — no action needed

Existing `docs/decisions/*.md` filenames already comply with the kebab-case
rule. The rule applies to new decision records going forward; no existing file
requires renaming.

## Compatibility and changes

Naming conventions apply to new code first.

Existing inconsistencies are not corrected through mass renames. Each rename
must identify:

- the current name;
- all code and API consumers;
- whether it changes a database column;
- whether it changes a public JSON key or stable code;
- the compatibility and test impact.

Django field renames require explicit schema review under the project's
migrationless development policy. Do not generate migration files as a side
effect of a naming cleanup.
