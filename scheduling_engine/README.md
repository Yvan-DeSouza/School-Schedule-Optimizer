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

Run its isolated test suite from the repository root:

```bash
python -m pytest -c scheduling_engine/pytest.ini scheduling_engine/tests
```
