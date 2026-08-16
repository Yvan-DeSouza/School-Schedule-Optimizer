# Decision: Semester and A-D Placement With Staffing Feasibility

## Status

Accepted. This decision supersedes older roadmap/SDD wording that bundled room
assignment into the first placement solve.

## Decision

The next scheduling stage places sections in Semester 1 or Semester 2 and in a
recurring A-D `TimeSlot`. It proves that the confirmed teacher roster has at
least one legal way to cover the resulting timetable, but it does not persist a
teacher recommendation, assign rooms, or assign students.

There are two sources:

- `fixed_semester` places active existing draft sections only in A-D blocks.
- `annual_total` starts with approved annual delivery-group counts, creates
  stable virtual slots such as “Physics slot 3,” and lets the solver choose both
  semester and A-D block. Real `Section` rows are materialized only on approval.

The annual source avoids nullable-semester or fake Section rows. A counselor can
therefore lock any annual virtual slot to a real target-year `TimeSlot` before a
semester has been chosen. Approval transfers that lock to the materialized
section and preserves its audit history.

## Constraints and objectives

- Course semester rules, active target-year timeslots, accepted schedules, and
  explicit timing locks are hard constraints.
- The roster must be ready. The timing objective first works over legal
  section-to-timeslot candidates, then an exact anonymous staffing witness
  validates every complete timing recommendation before it can be returned as
  complete. The witness enforces qualification, availability, one teacher per
  block, and semester/annual capacity limits. This decomposition avoids
  carrying interchangeable teacher identities through the large timing
  objective; witness identities never appear in a result or database write.
- Availability is available by default; only an explicit unavailable record
  denies a block.
- A yearly counselor-managed matrix records primary-request overlap, calculated
  co-request percentage, estimated retained shared demand, and tracked score
  overrides. Pair penalties use both effective score and likely affected volume.
- Placement also carries completion-defining full-semester normal-course demand
  as an internal aggregate timing-capacity guard. For same-semester student
  pathways, Hall-style block-subset lower bounds reject a timing pattern that
  has enough annual seats but provably too little capacity in the required A-D
  blocks. The guard is anonymous and creates no enrollment, section roster, or
  named-teacher decision; downstream student assignment remains the complete
  per-student feasibility authority for special commitments, locks, and every
  other student-level rule.
- Annual mode also balances semester exposure for high-overlap pairs. Same-course
  spread uses delivery-group balance objectives, not invalid self-conflict rows.

## Workflow

1. Counselors set up or refresh the annual conflict matrix and adjust scores
   through an append-only reasoned audit action.
2. They optionally add timing locks. Changes require a reason.
3. A run stores exactly the DTO snapshot it solved and returns a reviewable
   complete, partial, infeasible, or failed result.
4. Only a complete, unchanged run can be approved. Approval rechecks every
   relevant source fact inside one transaction and returns conflict on drift.
5. Approval writes timeslot-only `SectionSchedule` rows. Those schedules are
   fixed lifecycle context. Room assignment remains a later dedicated stage.

## Consequences

This gives counselors a conflict-aware timetable structure without pretending
that a staffing witness is a teacher assignment or that an unfinished room plan
is complete. Later teacher, room, and student stages consume accepted timing as
fixed context and retain separate approval workflows.

The staffing witness remains a hard approval prerequisite. A timing candidate
that cannot be covered by the confirmed roster is not returned as complete;
this formulation change affects search structure only, not the school's
staffing, qualification, availability, collision, or workload rules.
