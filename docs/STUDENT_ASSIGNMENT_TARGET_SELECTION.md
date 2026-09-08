# Student Assignment Target Selection

This document owns the research and diagnostic question: after an operator such
as `targeted_utilization_r16_s4` has already been selected, which students are
put in its bounded target scope? It does not choose the operator, change
Objective Semantics v2, define hard constraints, or authorize a candidate.

## What a counselor should understand

Target selection is a search-efficiency choice, not a counselor preference.
The solver still evaluates the complete schedule and the existing validator
still decides whether a candidate can replace the complete incumbent. A target
scope only says where a bounded utilization-repair probe is allowed to look.
Students outside the scope are protected by the operator's existing
neighborhood rules; they are not deleted, deprioritized in the schedule, or
assigned a worse counselor score.

The current implementation is in
`student_assignment/utilization_guidance.py`, through
`select_utilization_cluster_targets`. It starts from authoritative incumbent
assignments and the quality report, computes section-utilization pressure by
delivery group, enumerates legal alternate-section moves, and gives each
student deterministic leverage facts such as total positive leverage,
strongest single move, number of alternate-section opportunities, and relevant
delivery groups.

## The two current target policies

`top_individual` sorts students by their individual utilization leverage. It is
the broad, direct-leverage policy: students with the largest potential
pairwise-utilization improvement are considered first.

`interaction_aware` starts with the most pressured delivery group and then
prefers students whose leverage overlaps with already selected students'
delivery groups. It is intended to form a coherent cluster in the same
utilization basin, rather than simply taking four independent high-leverage
students.

Both policies use the same target size, legal-move calculation, delivery-group
pressure facts, and deterministic tie-breaking. The current diagnostic
R16/S4 screen uses four students. The research harness can also pass an exact
fixed scope for a matched replay or repeatability calibration; that fixed scope
is observational and does not alter authority.

## What is optimized and what is not

The targeting heuristic optimizes only the chance that a bounded R16/S4 probe
can improve the global section-utilization component. It does not directly
optimize a student's difficulty, category diversity, sequence, or semester
balance. Those five frozen v2 components are evaluated after a candidate is
produced and validated. A utilization-targeted move can therefore improve one
component while trading against another; the authoritative v2 value decides
whether the complete candidate is a strict improvement.

The delivery-group penalty and its alternate-section leverage are guidance
signals. `section_utilization_balance` remains a global pairwise objective; it
is not converted into a student-local counselor penalty. `guidance_facts` are
marked `guidance_only` and `objective_attribution=False` in the implementation.

## Counselor controls

There is no ordinary counselor control that selects individual target students.
The relevant diagnostic controls are the target policy (`top_individual` or
`interaction_aware`) and the bounded scope size. They belong to research or
future search configuration, not to a counselor's Objective Semantics v2
importance settings. Counselors control the v2 importance profile and special
commitments through the normal scheduling inputs; those settings change the
quality evaluation, not the meaning of target selection.

There are no target-selection soft constraints to tune. Legal alternate-section
availability, request compatibility, capacity, time conflicts, commitments,
and all other hard scheduling rules remain enforced by the existing model and
full-model validator. A target policy may propose a student only when the
student has usable legal alternate-section opportunities; it cannot waive a
hard rule.

## What is recorded for research

Target snapshots record the selected IDs, policy, target size, delivery-group
pressure, leverage summaries, construction trace when applicable, source
fingerprint, and bounded move facts. The screen's 48 cells and the later
eight-worker fixed-scope calibration record the scope, changed students,
changed source decisions, candidate gain, validation classification, and
candidate fingerprint. These records support descriptive mechanism and
repeatability analysis; a different scope or candidate does not imply a better
alternative schedule unless it is actually produced and independently
validated.

The one-worker screen was classified C and is not an eight-worker qualification.
The subsequent calibration deliberately fixes the historical TOP scopes to
separate worker/search variance from target-policy comparison. A future
held-out TOP-versus-IA study must use new authoritative R16 pre-states, the
production-like eight-worker contract, repeated clean processes, and
pre-state strata. It must not infer a policy rule from branch labels or build a
third heuristic without a transparent supported mechanism.

Related ownership: [Adaptive Search](STUDENT_ASSIGNMENT_ADAPTIVE_SEARCH.md)
chooses the next operator; [Hint Strategy](STUDENT_ASSIGNMENT_HINT_STRATEGY.md)
describes CP-SAT guidance inside the chosen scope; [Objective Semantics](STUDENT_ASSIGNMENT_OBJECTIVE_SEMANTICS.md)
defines the counselor-weighted v2 outcome; and [Validation](STUDENT_ASSIGNMENT_VALIDATION.md)
defines candidate authority.
