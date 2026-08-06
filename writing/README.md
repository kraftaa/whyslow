# The whyslow audit log

Four write-ups, each a bug found by running [whyslow](../README.md) against
realistic conditions rather than reasoning about it. They are ordered by how
transferable the lesson is, not by when they happened. None of them are about
whyslow specifically — whyslow is just where they were expensive enough to
learn.

1. **[pg_blocking_pids() returns the whole chain, not the culprit](01-pg-blocking-pids-transitive-chain.md)**
   — 80 blocked sessions reported 3,161 "blockers." The right answer was 1.
   The difference between a transitive closure and a root cause.

2. **[Four ways a diagnostic tool lies "all clear"](02-false-all-clear-taxonomy.md)**
   — the worst bug in the project produced a confident *wrong* answer, not a
   missing one. A taxonomy of blindness, and the two axes every diagnostic
   tool should track but almost none do.

3. **[Summing across workers hides the one that's on fire](03-puma-clustered-summing-bug.md)**
   — choosing `sum` over `max` is a modeling decision disguised as a
   formatting one. The same mistake as reporting mean latency.

4. **[864,031 lines → 152, and 733 MB → 8 MB](04-realistic-data-is-a-correctness-property.md)**
   — every test used one to six rows. Realistic volume exposed an
   unreadable-output cliff and an OOM that fixtures had hidden for months.

The throughline: **reasoning about whether code works is not the same as
watching it fail.** Each of these survived code review and looked correct.
None survived contact with 900k rows, 80 concurrent lock waiters, a saturated
Puma worker, or an Aurora reader endpoint.
