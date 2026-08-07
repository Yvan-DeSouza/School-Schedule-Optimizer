# Scheduling Engine

This package is deliberately independent of Django and the ORM. It accepts
`SchedulingInputDTO` values and returns plain result DTOs only. ORM loading,
persistence, HTTP endpoints, and solver execution belong to a future adapter
layer outside this package.

Run its isolated test suite from the repository root:

```bash
python -m pytest -c scheduling_engine/pytest.ini scheduling_engine/tests
```
