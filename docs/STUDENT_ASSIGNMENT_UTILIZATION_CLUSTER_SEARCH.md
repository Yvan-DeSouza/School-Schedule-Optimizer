# Student Assignment Utilization-Cluster Search

This document owns the utilization-cluster diagnostic operator's scope and
evidence. General student-assignment search policy remains in the linked search
strategy document.

## Status and boundary

This document records the diagnostic utilization-cluster study against the
durable `production_scale_v1` benchmark. The implementation is compatible
with both objective-semantics versions, but the target-scale results below are
v1 evidence because that benchmark and its 65,025 incumbent are v1. It is
search guidance, not a new quality metric and not a production scheduling
policy.

The authoritative section-utilization objective remains the global pairwise
absolute-difference penalty over sections in each delivery group. The
utilization guidance module uses that existing objective's current section
counts to select a bounded multi-student neighborhood. It does not attribute
the global penalty to an individual student, does not add an objective term,
and does not authorize a move. CP-SAT and the unchanged full-model validator
remain the only authorities.

The diagnostic entry point is
`run_student_assignment_operator_session_diagnostic`. The utilization family
names are opt-in diagnostic operators; they are not called by ordinary
student assignment and are not enabled in the adaptive production path.

## Guidance semantics

For every multi-section delivery group, the guidance computes the existing
pairwise penalty and identifies currently overfull and underfull sections. For
each currently assigned ordinary course request with another compatible
section in the same delivery group, it computes an optimistic single-request
penalty delta. This calculation deliberately ignores collisions, locks,
prerequisites, capacity contention, coupled commitments, and other global
feasibility interactions. It is therefore a leverage signal only.

The selection policies are:

- `top_individual`: highest optimistic individual leverage;
- `delivery_group_focused`: prioritize students touching the highest-pressure
  delivery group;
- `interaction_aware`: prefer high-leverage students whose delivery groups
  overlap with already selected students;
- `mixed`: deterministic leverage ordering as a control.

The initial implementation uses a conservative scope rule: the selected
candidate pool is exactly the operator's changed-student cap. This avoids a
second hidden scope-versus-cap semantic. Dynamic sessions recalculate the
scope after each adopted candidate; fixed sessions retain the caller-provided
scope. Session facts include bounded pressure/leverage evidence and explicitly
mark it as `guidance_only` with `objective_attribution=False`.

## Operator ladder

The diagnostic families currently describe this ladder:

| Family | Radius | Maximum changed students |
| --- | ---: | ---: |
| `targeted_utilization_r16_s2` | 16 | 2 |
| `targeted_utilization_r16_s4` | 16 | 4 |
| `targeted_utilization_r32_s4` | 32 | 4 |
| `targeted_utilization_r32_s6` | 32 | 6 |
| `targeted_utilization_r64_s6` | 64 | 6 |
| `targeted_utilization_r64_s8` | 64 | 8 |
| `targeted_utilization_r64_s10` | 64 | 10 |

The effective radius is reported as the smaller of the configured radius and
the number of source decisions owned by the selected students. This is a
diagnostic fact, not a relaxation of the configured neighborhood contract.
Every candidate is still a strict substantive improvement, complete, and
fully validated before adoption. `UNKNOWN` remains unresolved and is never
treated as infeasible.

## Target-scale benchmark identity

The study uses the detached durable benchmark only:

- input fingerprint:
  `1c4843ac33fccabd76218c63d8818c94a0a8dd8ab2886e3f5718ca1cd9576a11`;
- Stage 1/mature source fingerprint:
  `d5036a44e71d5a3b2a94eebe51d645bb4034179a0dd29537492ea81feda2e900`.

The benchmark contains 1,400 students, 10,760 requests, 10,945 required source
groups, 10,635 assignments, zero unmet required requests, 310 special
commitments, and 317 sections. The mature v1 source incumbent is substantive
65,025 with components 6,727 utilization, 175 semester balance, 35,973
difficulty, and 22,150 category diversity. The earlier v2 target-scale study,
which began at 37,596, is a separate historical evidence set and is not mixed
into the v1 measurements in this document.

No placement or named-teacher replay was rerun for these diagnostics. The
detached input and canonical checkpoint were read and verified; neither was
mutated.

## Progressive target-scale results

The first v1 screening ladder used eight workers, a 180-second session budget,
three attempts, 45-second per-probe limits, and 20 seconds for each
single-worker full-model validation. All adopted candidates were complete,
passed full-model validation, retained 10,635 assignments, retained zero
unmet required requests, and preserved all 310 special commitments. All
observed improvements were section-utilization improvements; the other
reported substantive components remained unchanged.

| Family | Final substantive | Adoptions | Session stopping | Peak working set |
| --- | ---: | ---: | --- | ---: |
| `R16/S2` | 65,019 | 3/3 | attempt cap | 0.83 GiB |
| `R16/S4` | 65,009 | 3/3 | attempt cap | 0.83 GiB |
| `R32/S4` | 65,007 | 3/3 | attempt cap | 0.83 GiB |
| `R32/S6` | 65,009 | 3/3 | attempt cap | 0.83 GiB |
| `R64/S6` | 65,007 | 3/3 | attempt cap | 0.83 GiB |

The strongest observed endpoint was 65,007, a gain of 18 from the 65,025
incumbent. `R32/S4` reached it in approximately 77.7 seconds in the measured
session; `R64/S6` reached the same endpoint in approximately 73.3 seconds.
The smaller families also reached validated improvements quickly. These are
bounded diagnostic sessions, not claims of global optimality.

The R32/S4 dynamic target history was `(9, 514, 714, 799)`, then
`(9, 514, 714, 844)`, then `(9, 514, 714, 794)`. Its first candidate changed
14 source decisions across two students and improved utilization by 8; the
next two candidates each changed one student and improved utilization by 8
and 2. R64/S6 produced a three-student, 19-decision intermediate candidate,
but its final endpoint was not better than R32/S4.

A fixed R32/S4 control using the first dynamic scope returned a candidate at
65,017, but full validation did not complete inside its bounded validation
window. It was correctly not adopted. This is an inconclusive fixed-scope
control, not evidence that fixed targeting is infeasible or inferior.

The R64/S8 and R64/S10 variants were not promoted after R64/S6 matched the
strongest R32 result. They remain available for a separately justified
experiment, and are now exposed to the offline calibration harness, but an
exhaustive ladder sweep would not be evidence-efficient.

## Model and resource observations

The targeted models were approximately 112,317 variables and 200,538 to
200,570 constraints in these target-scale trials. CP-SAT reported optimal
status for the bounded strict-improvement probes; full validation was the
larger external cost than the solver wall time in the captured sessions.
Peak process-tree working set stayed around 0.81–0.83 GiB, with no observed
monotonic growth across the session attempts. The guidance itself is bounded
when recorded: pressure facts retain complete pure-engine values, while
session summaries retain only a bounded sample of relevant student IDs and
the total count.

These measurements do not prove that larger radii cannot find a better
candidate. They show that the tested R64/S6 escalation did not outperform the
tested R32/S4 family under the same source incumbent and bounded session
configuration.

## Current classification

- utilization-cluster guidance: implemented as a diagnostic capability;
- `R16/S2`: useful diagnostic family;
- `R16/S4`: useful diagnostic family;
- `R32/S4`: strongest current utilization-cluster candidate;
- `R32/S6`: useful reference family, not superior in the measured sample;
- `R64/S6`: useful escalation reference, currently not better than R32/S4;
- `R64/S8` and `R64/S10`: available diagnostic families, unresolved in this
  utilization-cluster study;
- production adaptive enablement: not authorized;
- objective semantics: unchanged.

The study does not yet justify adaptive allocation across the full operator
portfolio. Before that decision, any controller study must compare these
families with qualified R2/R4/R8 controls on identical detached input and
source seed, measure normal run-to-run variation, include candidate and
validation cost, and use an explicit promotion gate for quality, runtime,
memory, completeness, repeatability, and unresolved outcomes.

## Next research boundary

Objective Semantics v2, input-derived normalization, the canonical counselor
importance score, and the complete diagnostic operator portfolio are now
implemented. The next boundary is offline adaptive-search calibration, not
further unbounded endpoint shaving or a second objective system. The current
order is:

1. preserve this diagnostic evidence and do not mutate the canonical source;
2. compare adaptive allocation with matched static controls across detached
   states, counselor profiles, budgets, quality, runtime, memory, and
   validation outcomes;
3. retain only evidence-backed policy behavior for further offline research;
4. require a separate production-promotion study before any adaptive
   scheduling execution is enabled;
5. defer full-school global escape until the preceding evidence justifies it.

Future guidance may select students, pairs, neighborhoods, or grades to
search. It must never authorize a candidate. CP-SAT and unchanged full-model
validation remain authoritative.
