# Special Student Schedule Commitments

**Status:** Accepted and implemented

## Decision

The scheduler distinguishes academic course properties from how a student's
time and local school resources are used. A normal instructional course remains
on the established path:

`CourseOffering -> DeliveryGroup -> Section -> accepted timeslot -> Section.teacher -> Enrollment`

Study, Focus, Co-op, and online supervision do not become fake `Section` or
`Enrollment` records. The student-assignment engine nevertheless reasons about
their occupancy alongside normal academic enrollments through detached DTOs and
an immutable run snapshot.

This decision is intentionally narrow. It models the school's described
programs and two known half-semester courses; it does not create a generic
calendar framework for arbitrary partial-duration programs.

## Catalog and offering rules

`Course.delivery_kind` is one of `normal_instruction`, `online`, or `co_op`.
`Course.duration` is `full_semester` or `half_semester`, and `credit_value`
remains an academic fact separate from delivery.

- Normal and online courses have an academic category. Online delivery does not
  make a course easier or category-neutral.
- Co-op is the single two-credit `co_op` course/program. It is category-neutral
  but has an ordinary calculated difficulty contribution.
- Normal instruction alone receives a `DeliveryGroup` and instructional
  `Section` rows. Online and Co-op still receive year-specific
  `CourseOffering` rows, but a null delivery group is intentional and is not a
  cancellation state.
- `HalfSemesterCoursePair` records the school's configured first-half then
  second-half catalog pair. `HalfSemesterSectionPair` records the matching
  concrete instructional section pair after approved section planning.

## Study and Focus

Study and Focus requests are represented by
`StudentScheduleCommitmentRequest`, not `CourseRequest`.

- A student may request up to two Study sessions in an academic year. Each
  occupies one recurring A-D block for both halves of its selected semester,
  but uses no section, teacher, room, academic category, or credit.
- The solver may choose the Study block unless a counselor creates an exact or
  exclusion lock. It never invents Study to fill an unrequested gap.
- Focus is a single request that occupies every A-D block in exactly one
  semester. It consumes no local section, teacher, room, online supervision,
  Study, or Co-op resource. The selected Focus semester is excluded from local
  semester-load, difficulty, and category-diversity comparisons rather than
  being presented as an artificially easy semester.

If a student has unallocated school time without a requested Study, alternate,
or other recognized commitment, the result reports a review item rather than
creating an implicit commitment.

## Online academic courses and supervision

An online course remains an academic `CourseOffering` with its normal credit,
category, difficulty, prerequisite, and request semantics. The student is
assigned an `OnlineEnrollment`, which links that offering to one shared
`OnlineSupervisionSession` at an accepted A-D timeslot.

Online supervision capacity is planned and approved before the established
placement and named-teacher stages:

1. A counselor configures one academic-year capacity profile and reviews an
   immutable `OnlineSupervisionPlanRun`.
2. Approval materializes unplaced supervision resources; it does not create
   instructional sections or assign teachers.
3. The regular placement stage assigns their semester/A-D timeslots alongside
   normal sections.
4. The regular named-teacher stage assigns a supervisor. A supervisor occupies
   ordinary availability and workload capacity, but no course-specific
   qualification is required.
5. Student assignment places each online academic request in one compatible
   supervision seat. Students taking the same online course may use different
   sessions; grouping them is deferred as a future soft preference.

Counselors may exactly lock or exclude an online request's supervision block.
The lock controls student time, not the supervisor identity. Online supervision
is included in the four staffing-mode snapshots and drift checks using the
same declared staffing context as normal sections.

## Co-op

Co-op is one two-credit academic request fulfilled by one active
`StudentScheduleCommitment`, not two normal course requests or sections. It
occupies A+B or C+D in a single semester. It has no local instructional
section, classroom, teacher workload, or course-qualification requirement.

Counselors can use exact or exclusion locks for a semester and its legal
`a_b`/`c_d` pair. The engine never splits the paired blocks. Co-op contributes
its two credits and difficulty to local academic balance but contributes no
category-diversity signal.

## Half-semester courses

Half-semester courses remain normal instructional courses unless their delivery
kind is online. A normal paired half-course has two ordinary sections that:

- share an accepted semester and A-D timeslot;
- occupy `first_half` and `second_half` respectively;
- receive the same qualified named teacher; and
- consume one teacher workload slot because the teaching is sequential rather
  than concurrent.

The student solver treats each half-course as half its usual semester-level
credit and difficulty contribution. Category-diversity comparisons are made per
half: sequential paired categories are not treated as simultaneous course
concentration. A missing configured partner or unmatched section pair is
scheduled when possible and reported for counselor review; the system does not
invent a replacement course or Study period.

For a half-semester online course, the academic difficulty/category applies
only to its configured half, while the physical online supervision seat remains
full-semester. The unused supervision half is a counselor review item and does
not create implicit Study or another course.

## Locks, history, and approval

`StudentSpecialCommitmentLock` is append-only. It has a required reason,
creator, release actor/reason/timestamp, exact-or-exclusion mode, and one
narrow target shape:

- `study_time` and `focus_semester` target a schedule-commitment request;
- `online_supervision_time` and `co_op_time` target the matching academic
  course request.

Approval writes `OnlineEnrollment` or `StudentScheduleCommitment` rows plus
their immutable student-assignment approval provenance. Retiring a prior active
special row preserves history; the workflow never deletes prior accepted facts.
All special records, locks, relevant course/request data, placed sessions, and
occupied timeslots are loaded as primitive facts into the immutable
student-assignment snapshot and revalidated before approval.

## Diagnostics and deferred work

Stable lower-snake-case diagnostics cover invalid or unavailable special
commitments, study limits, online supervision capacity, invalid Co-op pairs,
Focus placement, special locks, incomplete half-course pairs, unused online
supervision halves, and unallocated school time. Review uses these codes and
concrete occupancy facts rather than invented narrative explanations.

Deferred work includes grouping the same online course within supervision as a
soft objective, more generalized partial-duration scheduling, automatic
resolution of an unused online half, and broader solver explainability. The
historical student-results intake remains deliberately paused pending discovery
of the real school source-data representation.

Schema changes follow the repository's migrationless local-development rule:
no Django migration files are generated. The owner deliberately rebuilds a
local database with `migrate --run-syncdb` before using the new tables/columns.
