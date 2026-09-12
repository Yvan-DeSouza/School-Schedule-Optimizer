# Impossible Schedule Diagnosis

## Purpose and ownership

This is the canonical historical and technical record of preserved impossible
schedule incidents. It records reproducible evidence, mathematical witnesses,
and the upstream product stage that owns each repair. It is not the future
counselor-facing diagnosis workflow. That future capability belongs to
[`IMPORTANT_PRODUCT_IMPROVEMENTS.md`](IMPORTANT_PRODUCT_IMPROVEMENTS.md).

Passing a narrower diagnostic is useful evidence, not redundancy: each layer
eliminates a different class of possible causes before a larger model is run.

## Diagnostic ladder

The current research ladder is:

1. establish the raw solver status and input identity;
2. test each student's isolated completion program;
3. compare demand with eligible capacity;
4. solve ordinary capacity-only matching;
5. add ordinary student timetable collisions;
6. add supported paired-half-course occupancy and capacity;
7. later add online supervision;
8. later add Study, FOCUS, and Co-op;
9. inspect relevant combinations, find the smallest failing family, derive a
   mathematical/domain witness, and repair the owning upstream stage.

The reusable ordinary-plus-paired-half layer is
`scheduling_engine.benchmark_global_feasibility.ordinary_and_half_pair_diagnostic`.
It is deliberately not a substitute for the full Student Assignment hard
model or a counselor repair workflow.

## Incident 1 — detached manually authored topology

| Fact | Value |
| --- | --- |
| Fixture | `paul_desmarais_shaped_g9_12_stress_v2_2` |
| Frozen fingerprint | `3cbd268dea7afd2b34baaf0712d63296af5326c2592571a8c7476316ba581e35` |
| Source | Detached synthetic fixture code; not production placement |
| Preservation | Intentionally retained after later work |
| Reproduction | `python -m scheduling_engine.paul_desmarais_v2_2_diagnostics` |

The fixture had 1,400/1,400 isolated-feasible student programs and complete
ordinary raw capacity: capacity-only matching was 10,500/10,500. The global
ordinary capacity-plus-collision model was nevertheless `INFEASIBLE`.

The representative Grade-9 witness is MTH1W/CGC1W: 329 shared students could
reach S1-A, whose combined compatible capacity was 320, leaving deficiency 9.
The cause was correlated course-anchor/parity placement authored directly in
the detached benchmark topology, not a real section-placement workflow.

The v2.3 follow-up candidate is separate:
`paul_desmarais_shaped_g9_12_stress_v2_3` /
`3ab28b4c95577d3acabaaa4c47a16e49456f5511702b690e84a40cbbe9ed4600`.
It retained another cross-grade correlation problem. Neither detached fixture
is the production-pipeline lineage or a production repair target.

## Incident 2 — real production-pipeline half-semester topology

| Fact | Value |
| --- | --- |
| Source lineage | `paul_desmarais_shaped_g9_12_production_pipeline_v1` |
| Initial final-input fingerprint | `4ca85caf11673a604bb373beb9d443b6cee8bb295698933c345699bc9020a7fa` |
| Source fingerprint | `40a68e5c8948c1526113b6f153c7fa1e6ee67802b69131b7674c62582f20d35e` |
| Frozen negative artifacts | `scheduling_engine/benchmarks/production_pipeline/paul_desmarais_shaped_g9_12_production_pipeline_v1/` |
| Reproduction | `python -m scheduling_engine.diagnose_production_pipeline_stage1_infeasibility --time-limit-seconds 15 --kinds ordinary,half_pair --grades 10` |
| Expected result | Grade-10 ordinary = feasible/optimal; Grade-10 ordinary + paired-half = `INFEASIBLE` |

This lineage used real online planning, section budget, staffing preflight,
conflict matrix, annual-total placement, anonymous staffing witness,
placement approval, named staffing, and the final-staffing adapter. Its
complete Stage-1 hard model proved the frozen input `INFEASIBLE`.

Earlier layers correctly passed: placement safeguards, 1,400/1,400 isolated
feasibility, ordinary capacity-only matching, ordinary global collision, and
the Grade-10 ordinary-only layer. They do not model the physical
CHV2O/GLC2O-pair contract, so none could expose this later failure.

### Mathematical witness

The annual placement solver formerly placed CHV2O and GLC2O independently.
Approval then paired same-semester sections by ordinal/list `zip(...)` without
requiring a shared timeslot.

- 345 Grade-10 students require CHV2O/GLC2O and already have seven
  full-position requirements.
- Ten physical pairs were materialized; only four were co-timed.
- Four co-timed pairs at capacity 40 provide 160 one-position seats.
- Six split pairs consumed two A-D positions.
- A co-timed pair needs `7 + 1 = 8` positions; a split pair needs
  `7 + 2 = 9` in an eight-position year.
- Thus 185 students (`345 - 160`) could not use a valid one-position pair.

This was not a timeout, ordinary-capacity, staffing, online, Co-op, Study,
FOCUS, prerequisite, lock, or adapter-fabrication failure. The final adapter
faithfully imported the upstream invalid physical topology.

The preserved diagnostic tool is
`scheduling_engine/diagnose_production_pipeline_stage1_infeasibility.py`; it
reads immutable negative evidence and runs detached diagnostic models only.
The original accepted placement and `qualification_failure.json` remain
negative regression evidence and must never be overwritten.

### Repair boundary and new attempts

The real placement contract now creates a deterministic pre-placement key for
each supported first/second-half annual pair. CP-SAT requires both units with
that key to choose the same recurring semester/A-D timeslot. Approval checks
academic year, course order, half order, semester, timeslot, and equal capacity
before it creates `HalfSemesterSectionPair`; it never repairs a bad candidate.

New qualification attempts retain the same upstream source lineage but write
under:

`scheduling_engine/benchmarks/production_pipeline/paul_desmarais_shaped_g9_12_production_pipeline_v1/attempts/<attempt-id>/`

Run the real qualification explicitly (not as ordinary pytest):

```powershell
pytest --create-db -q -s backend/tests/paul_desmarais_production_pipeline_qualification.py
```

The attempt must stop on any `INFEASIBLE` or `UNKNOWN` production CP-SAT stage.
Before Stage 1 it must prove zero split pairs, 1,400/1,400 isolated feasibility,
complete ordinary matching, ordinary collision feasibility, and ordinary-plus-
paired-half feasibility. Stage 1, if reached, uses the actual final-staffing
DTO, 120 seconds, eight workers, local-only mode, no Stage 2, and independent
full-model validation of any complete seed.

## Change control

Preserved negative fixtures, fingerprints, artifacts, and witnesses are
historical evidence. A repair creates a distinct attempt artifact; it does not
rewrite an earlier failure. Any new impossible-schedule incident must record
its identity, source type, semantic fingerprint, artifact location,
reproduction command, expected result, witness, diagnostic tool, and repair
owner here.
