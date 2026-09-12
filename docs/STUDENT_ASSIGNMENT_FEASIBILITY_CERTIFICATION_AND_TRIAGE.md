# Student-Assignment Feasibility Certification and Triage (Planned)

## Status and purpose

This is future-design documentation only.  It does not alter the accepted
first-release student-assignment contract, create a partial-scheduling mode,
or make a certificate mandatory.  It records the two follow-up capabilities
needed after candidate-section construction becomes sufficiently mature:

1. a bounded, whole-cohort hard-feasibility certificate; and
2. honest, counselor-visible handling when some imported programs cannot be
   completed.

Existing target-scale research has already produced complete, validated Stage 1
seeds for its own immutable detached inputs.  The gap described here is a
repeatable product workflow after the production adapter constructs the actual
candidate domain, not a claim that the solver cannot find complete schedules.

## Future feasibility certification

After the production adapter has constructed candidate sections and before
soft-objective optimisation, a future workflow may run the existing Stage 1
hard-feasibility model with a configured, bounded runtime.  It must use the
same hard rules and candidate contract as allocation; it must not silently
weaken eligibility, capacity, timing, fixed commitments, locks, or academic
constraints merely to obtain a result.

Possible result states should remain distinct:

| State | Meaning |
| --- | --- |
| `structural_preflight_passed` | Local structural safeguards found no known issue; this is not a full certificate. |
| `globally_certified_feasible` | Stage 1 produced and validated a complete hard-feasible assignment. |
| `proven_globally_infeasible` | The exact certification model proved infeasibility. |
| `certification_unknown` | The configured runtime ended without a proof or validated complete seed. |

A certificate is evidence, not an operational allocation: a persisted artifact
should identify the candidate-input fingerprint, solver/model version, runtime
bound, outcome, and validation result.  Whether certification is optional,
required for particular runs, or advisory remains a product/governance decision.

## Future partial-result triage

If a complete schedule cannot be certified, the product must not describe a
partial allocation as complete or silently omit/alter authoritative requests.
An eventual triage workflow may distinguish:

| State | Meaning |
| --- | --- |
| `individual_program_impossible` | An isolated student program has no completion under its actual candidate domain. |
| `global_contention_unresolved` | Individual programs may complete, but shared sections/resources prevent or have not yet yielded a whole-cohort result. |
| `unknown` | The available bounded work cannot classify the cause. |

The individual check is diagnostic only.  It cannot account for shared seat,
teacher, room, or global timing contention, and it must never be used to
automatically remove a request.  Counselor actions, approvals, review flags,
and persistence of any changed program remain future design work.

## Relationship to the v2.1 stress fixture

The `paul_desmarais_shaped_g9_12_stress_v2_1` fixture is currently blocked at
the first category: 345 Grade 10 students have isolated candidate-domain
infeasibility after the fixture was corrected to mirror the production
adapter's online-offering contract.  Its witness is a Hall deficiency: six
required requests compete for four whole-block slots.  No global certification
should run until the fixture's candidate construction is repaired and the
isolated preflight passes.

Benchmark facts and provenance are owned by
[`TARGET_SCALE_PRODUCTION_STRESS_BENCHMARK.md`](TARGET_SCALE_PRODUCTION_STRESS_BENCHMARK.md).
