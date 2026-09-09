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

The one-worker screen was classified C and was not an eight-worker
qualification. The subsequent calibration deliberately fixed the historical
TOP scopes to separate worker/search variance from target-policy comparison.
The production-like TOP-versus-IA qualification described below has now been
run, but its states were selected from the already analyzed 70-state TOP
trajectory. It is therefore a repeated, diverse-prestate qualification rather
than a fully independent state-aware generalization test.

## All-70 scope-structure evidence (2026-09-08)

A solver-free reconstruction covered all 70 authoritative R16/S4 attempts in
the three-hour R16-only branch. The four jackpot attempts (27, 36, 60, and 63)
supplied 396 of the branch's 1,212 validated v2 points, or 32.673%, while
representing 4/70 attempts. The analysis joined each executed TOP scope to its
persisted pre-solve legal-move guidance, source/destination overlap, possible
chains, pressure concentration, and current component-pressure facts.

No measured joint structural feature was a strong continuous gain predictor.
Source-to-destination overlap was higher in the four jackpots, but its
continuous correlation with gain was weak and the four-event sample is too
small for a policy coefficient. No TOP scope had a guidance-level structural
cycle candidate. Selected individual leverage was not positively associated
with jackpot gain. These results do not support introducing a third
coordination-aware target policy.

The solver-free interaction-aware shadow nevertheless selected a materially
different scope in every one of the 70 states: there were zero exact matches,
median Jaccard overlap was zero, maximum overlap was 1/7, and all overlaps were
below 0.5. That establishes policy separation, not IA schedule quality. It was
enough to motivate an eight-worker comparison of the existing
`top_individual` and `interaction_aware` policies.

The qualification used eight diverse pre-states selected from the analyzed
trajectory, including state 60 as a known TOP-jackpot positive control. With
two policies and three clean repeats it produced 48 one-transition cells.
Every cell retained R16/S4, eight optimization workers, seed 101, current
incumbent-derived hints, a 300-second CP-SAT ceiling, independent one-worker
validation, strict v2 adoption, and a fresh authoritative reset. The sealed
lineage is
`C:\Users\desou\research_runs\v2_r16_top_vs_ia_eight_worker_20260908_0245_a7c1e9f3`.

All 48 cells were operationally valid and fully validated/adopted, with no
sleep contamination or rerun. TOP had mean/median gain 17/18 v2 points and
IA had 13.5/15. TOP won the state-level median comparison in three states,
IA won three, TOP won four, and one was tied; excluding the known state-60
control, the split remained 3--3. The primary classification was therefore
**C: TOP and IA show state-dependent complementary value**. TOP produced the
largest observed gain (+84 at the known positive-control state), while IA's
largest gain was +18. These are observed outcomes of this matched
production-like qualification, not a general policy-superiority claim.

The qualification also showed that IA consistently made smaller changes: all
24 IA cells changed one student and at most two source decisions, whereas TOP
changed one to four students and reached 16 source decisions in the positive
control. This describes the observed search geometry; it does not prove that
the larger TOP moves are preferable in future states. No third targeting
policy, hint treatment, or dynamic continuation treatment was added.

The solver-free post-run audit of this sealed lineage is at
`C:\Users\desou\research_runs\v2_r16_top_vs_ia_eight_worker_postrun_audit_20260908T101415Z_3d199425`.
It is a derived integrity and analysis package; it does not contain or infer
alternative schedules.

Related ownership: [Adaptive Search](STUDENT_ASSIGNMENT_ADAPTIVE_SEARCH.md)
chooses the next operator; [Hint Strategy](STUDENT_ASSIGNMENT_HINT_STRATEGY.md)
describes CP-SAT guidance inside the chosen scope; [Objective Semantics](STUDENT_ASSIGNMENT_OBJECTIVE_SEMANTICS.md)
defines the counselor-weighted v2 outcome; and [Validation](STUDENT_ASSIGNMENT_VALIDATION.md)
defines candidate authority.
