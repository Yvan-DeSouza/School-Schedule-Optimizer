# Paul-Desmarais-Shaped Target-Scale Production Stress Benchmark

## Authority and purpose

This is the single human-readable authority for the benchmark-specific assumptions of `paul_desmarais_shaped_g9_12_stress_v2_2`. It is a deterministic, synthetic **1,400-student Grades 9--12 production stress benchmark** shaped by verified Paul-Desmarais and Ontario rules. It is **not** measured Paul-Desmarais enrollment, demand, or program-prevalence data.

The intentionally upper-bound scale supplies capacity and runtime margin for the existing two-semester v1 product. It is not a universal Canadian timetable model.

## Identity and reproducibility

| Field | Value |
| --- | --- |
| Fixture identity/version | `paul_desmarais_shaped_g9_12_stress_v2_2` / `v2.2` |
| Generator | `scheduling_engine.paul_desmarais_stress_benchmark` |
| Seed | None; deterministic construction, not sampled generation |
| Fingerprint | SHA-256 of semantic input, curated metadata, assumptions, and coverage descriptors |
| Static audit | `summarize_paul_desmarais_shaped_g9_12_stress_fixture(...)` |
| Legacy relationship | Legacy fixtures are unchanged for historical replay; this is a distinct lineage |

Run the static audit without CP-SAT:

```powershell
python -m scheduling_engine.paul_desmarais_stress_benchmark
```

Any material change must update the fixture/manifest, this document, deterministic audit expectations, and fixture fingerprint/version together.

Version `v2.1` corrected a material detached-candidate-contract defect in the
superseded `v2` fixture: engine-only online-supervision sections had advertised
normal instructional offerings. The corrected fixture uses distinct online
offering identities and advertises only synthetic online requests, matching
the production adapter's offering-membership contract. This is a new semantic
fixture version within the same benchmark lineage; its fingerprint is
intentionally different. Its two 900-seat sections per normal course then
proved individually infeasible for 345 Grade 10 students because their timing
cells formed a Hall deficiency.

Version `v2.2` retains the corrected online-offering contract and replaces the
unrealistic topology with deterministic demand-driven section multiplicity and
a fixed two-cell anchor grid. It has 276 ordinary full-course sections, 18
paired-half sections, and 6 online-supervision sections. Capacities, the
approximately 300-section shape, online-session count, and anchor grid are
synthetic stress assumptions, not measured Paul-Desmarais facts.

Before a target-scale run, the bounded fixture suite performs an exact
per-student completion preflight over the detached candidate input. It checks
that every generated student has an isolated, collision-free completion of
mandatory requests; it deliberately ignores other students' competition for
shared capacity. It is therefore an acceptance gate against individually
impossible generated programs, **not** proof that the 1,400-student allocation
is globally feasible or a substitute for the production section-placement
workflow. The v2.2 topology passes this isolated gate for all 1,400 students.

### Stage 1 certification record (2026-09-11)

One authorized replacement Stage-1-only hard-feasibility attempt was made on
the v2.2 fixture. Its input copy was explicitly set to `120.0` seconds; the
canonical fixture input remained `20.0` seconds, and both the requested and
effective Stage 1 limits were verified as `120.0` seconds. The normal Stage 1
worker count was `8`. The run used fixture fingerprint
`3cbd268dea7afd2b34baaf0712d63296af5326c2592571a8c7476316ba581e35`.

It returned `stage1_solver_outcome=infeasible` after `29.114` seconds of
external seed-attempt time. No complete seed was produced, so independent
full-model seed validation was not applicable and no Stage 2 or optimization
operator ran. The enclosing result was `status=failed` with
`solver_outcome=unknown`; that result-level field does not supersede the
explicit Stage 1 infeasibility finding. The fixture is therefore **not
globally certified**. The isolated preflight remains a local-program check,
not a constructive whole-cohort certificate.

A subsequent reduced-model diagnosis established the cause without changing
the fixture. Capacity-only matching assigns all 10,500 ordinary full-course
groups, but adding the existing per-student no-double-booking rule is
infeasible. For example, 329 Grade 9 students require both `MTH1W` and
`CGC1W`; their only cells are S1-A and S2-B. Each such student must use one
of those two courses in S1-A, while the two S1-A pools contain only 320 seats
(`160 + 160`). The fixed anchor grid repeats positions with the same
course-ID parity, so demand-driven odd section counts orient both courses
alike rather than complementing each other. This is a proven synthetic
topology/anchor-correlation defect, not an isolated-program, online-membership,
or special-program failure. A repaired globally feasible topology requires a
new semantic fixture version; no repair is made by this record.

## Scope and topology

| Fact | Provenance | Meaning |
| --- | --- | --- |
| Grades 9--12 only | `verified_school_rule` | The supported v1 universe is `{9,10,11,12}`. Grades 7--8 are absent from students, sections, and objective denominators. |
| Two semesters | `verified_school_rule` | v1 supports exactly two terms, not arbitrary term counts. |
| Four blocks per term | `verified_school_rule` | Current fixture blocks are A/B/C/D. |
| Rotation | `verified_code_behavior` | Day 1 A B C D; Day 2 C D A B; Day 3 B A D C; Day 4 D C B A. |
| Locale | `verified_school_rule` | French-first `fr-CA`, with English labels. |

Abstract block identity is solver-relevant now. The cycle ordering is execution/presentation metadata today, but future day-specific constraints may make it solver-relevant.

## Provenance model

| Classification | Meaning |
| --- | --- |
| `verified_school_rule` | Paul-Desmarais/domain-owner fact or explicit v1 product scope. |
| `verified_ontario_rule` | Provincial course/policy fact used by the fixture. |
| `verified_code_behavior` | Current repository behavior; not an institutional claim. |
| `explicit_project_domain_rule` | Explicit project/domain decision used by the fixture, without claiming a provincial or school-wide mandate. |
| `synthetic_stress_assumption` | Deterministic scale/distribution selected for stress coverage, not measured prevalence. |
| `synthetic_coverage_case` | Deliberately rare case used to exercise a boundary, not a prevalence claim. |
| `unknown_deferred_domain_fact` | Important unknown that must not be promoted into a hard rule. |

## Verified school and Ontario rules

School/domain-owner supplied facts used here are: normal Study use is Grade 12; normal FOCUS eligibility is Grades 11--12; FOCUS occupies a whole semester; current connected two-credit Co-op placements are A+B or C+D; and the four-day rotation above. Study occupies a timetable position and has zero credits. There is no verified hard maximum of two Studies.

Ontario facts used by this fixture are:

| Fact | Provenance |
| --- | --- |
| CHV2O (Civisme / Civics and Citizenship) is 0.5 credit | `verified_ontario_rule` |
| GLC2O (Choix/Exploration de carrière / Career Studies) is 0.5 credit | `verified_ontario_rule` |
| The normal CHV2O/GLC2O pair represents one course position | `explicit_project_domain_rule` |

The bilingual curated course subset is not the complete current school offering. Future catalog work must distinguish valid Ontario courses, board/local courses, school-active offerings, and synthetic demand. Relevant sources are the [Ontario Ministry Defined Courses dataset](https://data.ontario.ca/dataset/ministry-defined-courses), [Ontario Locally Developed Courses dataset](https://data.ontario.ca/dataset/locally-developed-courses), and [Ontario course descriptions and prerequisites](https://www.dcp.edu.gov.on.ca/en/course-descriptions-and-prerequisites/).

## Synthetic stress assumptions

All values below are deterministic benchmark choices, never measured school prevalence.

| Assumption | Value | Provenance |
| --- | ---: | --- |
| Total students | 1,400 | `synthetic_stress_assumption` |
| Per Grade 9/10/11/12 | 350 each | `synthetic_stress_assumption` |
| Study requests | 70 across 56 Grade 12 students: 42 with one, 14 with two | `synthetic_stress_assumption` |
| FOCUS commitments | 28: 14 Grade 11 and 14 Grade 12 | `synthetic_stress_assumption` |
| Connected 2-credit Co-op | 42: 1 Grade 9, 1 Grade 10, 20 Grade 11, 20 Grade 12 | `synthetic_stress_assumption` |
| Online course choices | 84: 21 per grade | `synthetic_stress_assumption` |
| Course/pathway demand mix | Curated deterministic code/pathway mix | `synthetic_stress_assumption` |

Online is modeled as one 1.0-credit replacement where current data permits; it is not measured online enrollment.

### Section topology

Normal full-course section count is `ceil(normal instructional demand /
capacity_max)`. Most sections use maximum/target capacity `40/35`; the
MCF3M/MCR3U coverage courses use `30/28`. CHV2O/GLC2O have nine matched
`40/35` section pairs. The six online-supervision sessions use `16/14`.

Each ordinary course is offered in two deterministic cross-semester anchor
cells, selected from S1-A/S2-B, S1-B/S2-C, S1-C/S2-D, and S1-D/S2-A according
to its curriculum position. Multiple physical sections divide demand across
those two cells; they do not make every course available in every block. The
six zero-demand canonical courses have no synthetic instructional section.

## Synthetic coverage cases

| Case | Fixture behavior | Provenance |
| --- | --- | --- |
| Upper-grade CHV2O/GLC2O | Five Grade 11/12 pair cases; remaining 345 are Grade 10 | `synthetic_coverage_case` |
| Co-op + Study | Compositional occupancy | `synthetic_coverage_case` |
| Co-op + online | Compositional occupancy | `synthetic_coverage_case` |
| Study + online | Compositional occupancy | `synthetic_coverage_case` |
| Co-op + Study + online | Compositional occupancy | `synthetic_coverage_case` |
| FOCUS + online/ordinary work in other term | Whole-term occupancy plus remaining-term work | `synthetic_coverage_case` |
| MCF3M -> MCR3U | Exactly 56 eligible same-year opportunities | `synthetic_coverage_case` |
| Lower-grade Study/FOCUS | Audit metadata only, not ordinary population | `synthetic_coverage_case` |

MCF3M -> MCR3U is a counselor-defined soft sequence preference. It normalizes by actual eligible opportunities, never school enrollment, and is not an official prerequisite.

## Special-program semantics

Study occupies one position and has zero credits; its intended future-domain difficulty is zero. Historical v2 replay remains intact and is not reinterpreted by this fixture. FOCUS occupies all instructional blocks in one selected term. Its internal credit, difficulty, and category accounting are excluded pending authoritative information.

The ordinary movable Co-op is one indivisible two-credit request occupying connected A+B or C+D in one term. Credit and occupancy are separate concepts.

| Co-op shape | Task-1 state |
| --- | --- |
| One-credit / one position | Fixed-context coverage only |
| Connected two-credit | Current movable v1 support |
| Two independent one-credit requests | Explicitly not movable support yet |
| Flexible same-semester two-credit | Explicitly not movable support yet |
| Four-credit whole-term | Fixed-context coverage only |

No five-block Co-op rule, universal special-program system, or automatic credit-to-block inference is introduced.

## Difficulty and category provenance

All fixture difficulties use `metadata_and_relative_history_v2` through its metadata-only path: no fabricated marks, zero historical observations, zero historical confidence, and no manual overrides. The 0.5-credit pair therefore remains 0.5-credit weighted wherever credit weighting applies.

The existing seven-category taxonomy and diversity mathematics remain unchanged. The accidental `english` fixture category is corrected to `language`. Existing matrix values are `synthetic_default_relationships`, not counselor-reviewed. Category Taxonomy v2 is deferred.

## Prerequisite scope

Task 1 neither supplies a complete Ontario prerequisite graph nor implements waiver persistence or final no-waiver scheduling behavior. Official prerequisites, explicit authorized student-specific waivers, and counselor soft sequence preferences remain distinct. The treatment of imported downstream coursework with neither prerequisite evidence nor waiver evidence remains an unresolved domain/policy decision. No inferred prerequisite edge is added here.

## Unknown and deferred facts

- Exact current Grades 9--12 enrollment and program/course prevalence.
- Formal Study maximum and review threshold.
- FOCUS internal credit, difficulty, and category accounting.
- Complete school/CECCE active offering and locally developed metadata.
- Actual MCF3M/MCR3U opportunity prevalence.
- Prerequisite-waiver representation and missing-waiver behavior.
- Five-block special-program occupancy templates.
- Institutional governance/approval policy.

## Change-control boundaries and non-goals

This fixture adds neither Grades 7--8 scheduling, trimesters/quarters, five-block solver support, arbitrary half-course combinatorics, an Ontario-wide prerequisite catalog, a complete board catalog, Objective v3, nor Category Taxonomy v2. It does not call synthetic prevalence, category defaults, or current governance behavior institutional fact.

Keep benchmark-specific facts here. Generic architecture belongs in dedicated architecture records; other documents should link here rather than duplicate these assumption tables.
