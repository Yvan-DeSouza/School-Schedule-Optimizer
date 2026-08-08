# Scheduling Engine

This package is deliberately independent of Django and the ORM. It accepts
`SchedulingInputDTO` values and returns plain result data only. ORM loading,
persistence, and HTTP endpoints remain in the Django adapter and application
service layers outside this package.

`section_planner.py` produces a demand baseline, a staffing-feasible annual
plan, and a Semester 1/2 split. Its result includes stable structured
diagnostics for infeasible scenario constraints, qualification gaps, staffing
capacity shortfalls, and feasible plans with unmet demand. It never creates
database sections or named teacher assignments.

`section_budget_planner.py` allocates an exact or ceiling school-wide physical
section budget without teacher data and resolves explicit cancellation/backup
policies without mutating source requests. `staffing_planner.py` then works on
physical delivery groups, including combined courses, and proves that counts fit
the confirmed qualified teacher-capacity pool. A linked staffing solve keeps the
approved budget total while reporting any delivery-group reallocations.

Run its isolated test suite from the repository root:

```bash
python -m pytest -c scheduling_engine/pytest.ini scheduling_engine/tests
```
