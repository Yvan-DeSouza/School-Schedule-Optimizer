# API Contract Strategy

This document owns the backend transport-contract policy. Concrete request and
response shapes remain authoritative in serializers, routes, and endpoint
tests; stage behavior belongs to the accepted decision records.

The backend API contract currently lives in DRF serializers, view routes, and
endpoint tests. That is acceptable while the project is backend-only, but before
the frontend becomes a major consumer the project needs a published, tested
contract that frontend work can depend on.

## Current rule

- Serializers remain the runtime source of request/response shape.
- Every backend API view must declare a resource policy or action policy.
- Endpoint tests should cover role access, important response fields, and stable
  diagnostic/error codes.
- Student-assignment run creation accepts an explicit objective-semantics
  version. The v1 label-only shape remains the default for compatibility; v2
  may carry five canonical integer importance scores from 0 through 10. The
  serializer owns transport validation, while the service snapshots the
  resolved version/settings and the pure engine owns objective mathematics.
  These fields are additive JSON contract data and require no model migration.

## Named teacher assignment

The named-teacher planning configuration and run endpoints follow the same
policy, serializer, review, and approval contract as placement. The frontend
must use stable codes from `scheduling_engine/diagnostics.py`, not diagnostic
text. Teacher-assignment run review responses intentionally include the proposed
teacher identity; placement witness responses intentionally do not.
- Human docs in `README.md` are helpful, but they are not the authoritative
  machine-readable API contract.

## Before frontend development

Choose one of these paths:

1. Generate an OpenAPI schema from DRF routes and serializers.
2. Maintain a tested `docs/api.md` with request/response examples copied from
   fixtures or snapshot-style tests.

Do not hand-write a large API reference that can drift from serializers. If a
manual document is used, route tests must verify the examples remain valid.

## Stable codes

Frontend behavior should key off stable codes, not English messages. Solver
diagnostic codes are defined in `scheduling_engine/diagnostics.py`; backend
workflow codes should move into domain-specific code modules as they become UI
contracts.

## Definition of done for the first frontend milestone

- Every rendered screen calls endpoints covered by backend tests.
- Role-based access behavior is tested at policy and endpoint level.
- Key planner diagnostics and workflow conflicts use stable documented codes.
- The chosen API contract document/schema is generated or verified by tests.
