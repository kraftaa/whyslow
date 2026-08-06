# whyslow audit log

The full record of bugs found by repeated audits, not by guessing — moved out
of the [README](README.md) to keep it short, and linked from there.

Each entry is a bug (or a fix that was itself wrong) surfaced by running
whyslow against realistic conditions rather than reasoning about the happy
path. The four most transferable stories are also written up as standalone,
teaching-oriented posts in **[writing/](writing/)**.

---

## Bugs found by repeated audits, not by guessing

### Most recent round: wide windows combined unrelated evidence

Resource pressure used to be counted across the entire requested window.
Three isolated CPU session changes in three different minutes therefore
became one "spike," and any high CloudWatch CPU value anywhere in the
window counted as corroboration. A wide investigation could combine two
unrelated events into one confident-looking explanation.

Detection now requires at least three CPU- or IO-category changes in the
same minute. Puma, events, and CloudWatch evidence must overlap that spike;
CloudWatch uses the actual 60-second measurement interval rather than the
time the delayed datapoint happened to be collected.

The CloudWatch collector also resolves the Aurora cluster's current writer
before every poll, preserves that source instance with each datapoint, and
deduplicates overlapping publication-lag queries. A failover therefore
switches collection to the new writer without a restart, while historical
evidence remains attributable to the instance that produced it. The temporal
regression test includes both counterexamples: scattered activity never
forms a candidate, and a distant high-CPU metric never corroborates a
nearby spike.

### Most recent round: every test used unrealistically tiny data

Generating a realistic dataset — 48 hours at ~5 session changes/second,
which is normal churn on a busy database, not a pessimistic estimate —
exposed two problems that every previous test had hidden by using
between one and six synthetic rows.

**Output was unreadable at real volume.** A 48-hour window produced
**38 MB across 864,031 lines**. Worse, the *recommended* command from the
runbook — a 15-minute window — produced **4,500 lines**. The per-row
timeline was never viable outside a toy dataset. Sessions are now
aggregated per minute per category (blocking edges and events stay
individual, since they're rare and always significant), and the timeline
is capped at 120 lines with a note about what was omitted:
**864,031 lines -> 152**.

**A single `explain` used 733 MB of RAM.** Loading ~900k session rows
into Python is a genuine OOM risk on a small collector host — a t3.small
has 2 GB, and the systemd units set no limit. Session aggregation moved
into SQL, bounding memory by (minutes x categories) rather than row
count. Nothing was lost, because individual session rows were never
displayed or used individually — only counted and checked for temporal
proximity, both of which survive aggregation:
**733 MB -> 8 MB, 7.3s -> 0.6s**.

The benchmark is now an asserting regression test, and the systemd units
gained `MemoryMax=512M` so a future regression gets killed there instead
of taking the host down.

### Most recent round: the Aurora reader endpoint — a false all-clear from *misconfiguration*

Aurora clusters expose a writer endpoint and a reader endpoint. Pointing
the collector at the **reader** is the safer-*looking* choice — read-only,
no write risk — and it is the wrong one: write-lock contention happens on
the writer and is structurally invisible from a reader.

The failure mode is the same catastrophic false all-clear as the two
rounds below, but from an entirely different cause. Coverage would read
**100%** (the collector really was running the whole window), so every
coverage check already built would wave it through, and `explain` would
report "no blocking found" forever.

Now detected via `pg_is_in_recovery()` on connect, recorded in every
heartbeat (not just logged once at startup, where nobody would see it),
and surfaced in `explain`, in `status`, and as a non-zero `status` exit
code so monitoring catches it.

Two follow-on inconsistencies caught while fixing it, both from testing
the output rather than trusting it: the coverage block said
`postgres covered` while the warning above said that data was
unusable, and the "nothing was running here" message fired for a
collector that **was** running, just on the wrong endpoint. Time-based
coverage turned out to be necessary but not sufficient — a collector can
cover a window perfectly and still be structurally unable to see the
thing you're asking about.

### Most recent round: the previous round's fix was itself wrong

Auditing the coverage fix from the round below found **the same class of
bug reintroduced in subtler form.** Coverage was computed as one
aggregate number — "was *any* collector running?" — but each collector
answers a different question. A healthy Puma collector alongside a dead
Postgres collector therefore read as 100% covered, and `explain` printed
a confident *"none found — no blocking edges"* when the collector that
records blocking edges had never been running.

Fixed by making coverage per-role, and naming the specific conclusion
each gap invalidates:

```
!! INCOMPLETE COVERAGE -- some conclusions below are unsupported !!

  postgres    missing 100% of window -> cannot rule out: blocking chains and database session pressure
  cloudwatch  missing 100% of window -> cannot rule out: instance CPU and connection counts
  puma        covered (puma:web-3)
```

The confident "none found" phrasing is now gated on the **database**
collector specifically, since blocking edges and session categories come
from it exclusively. Worth stating plainly: this was a regression in a
fix shipped one round earlier, caught only by testing the fix rather
than trusting it.

Puma coverage is also evaluated across the full known fleet. Every
target that existed during the requested window must meet the coverage
threshold; one healthy web host cannot hide a missing peer. Historical
reports ignore targets that had not joined the fleet yet or had already
been explicitly retired. Run `whyslow retire puma:HOST` during planned
scale-down; a later heartbeat automatically reactivates that collector.

Stored query evidence is redacted by a PostgreSQL-aware scanner before
it reaches SQLite. It handles ordinary and escape strings, tagged dollar
quotes, bit/hex literals, nested comments, and unterminated input; only
statement structure needed for diagnosis is retained.

### Round: an unwatched window read as an all-clear

- **The worst bug found in this project, because it produced a confident
  *wrong* answer rather than a missing one.** With no collector running,
  `explain` printed *"none found — no blocking edges or sustained
  resource pressure in this window"* — and RUNBOOK.md told you that
  meant "the database is probably not your problem, look at the app
  layer." That is absence of evidence dressed up as evidence of absence.
  A collector that crashed, or was never deployed, would have sent you
  confidently in the wrong direction mid-incident.

  Fixed by recording historical coverage (one row per collector per
  minute — the existing heartbeat table is upsert-only, so it answers
  "alive now?" but makes past outages invisible). `explain` now leads
  with a loud warning for an unwatched window, flags partial coverage
  with a percentage, and reserves the confident "none found" phrasing
  for windows collectors actually covered. The runbook's guidance was
  corrected to match.

- **Live incidents — the primary use case — showed no duration.**
  `held_seconds` was only computed for *resolved* blocks, so during an
  ongoing incident it printed a useless `held=ongoing`, unable to
  support the runbook's own "kill it, or wait if it's nearly done"
  decision. Now reports elapsed-so-far: `held=240s+ and counting`.

- **Long-running blocks could disappear from later windows.** Blocking
  rows were selected only when their start timestamp fell inside the
  requested window. They are now treated as intervals: a block that
  began earlier but remained active is included, correlated with
  evidence observed while it was active, and rendered using its state
  at the requested window end. A block known to have resolved later
  does not receive a dangerous live PID termination suggestion.

- **A collector crash could make an unresolved block look active
  forever.** An edge with no recorded end is now bounded at the first
  complete Postgres coverage gap and labeled `resolution unknown`.
  Later windows do not resurrect it as a live blocker, and reconnects
  preserve continuous edges while separating post-gap PID reuse into a
  new episode.

### Round: writing the runbook exposed three gaps

Writing [RUNBOOK.md](RUNBOOK.md) — what to actually type at 2am — turned
out to be the most effective audit yet, because it forced the question
"can someone actually *act* on this output?"

- **The runbook said "kill the blocker: `pg_cancel_backend(<pid>)`", but
  the output never showed a pid.** The contributor line read
  `analytics_role (reindex) [blocking]` with no way to act on it. Now:
  `pid=928  held=3s  blocking=1 session(s)`, plus a ready-to-paste
  cancel/terminate command — shown **only** for blockers still active at
  window end, since Postgres reuses pids and suggesting a kill for a
  resolved one is useless at best, dangerous at worst.
- **The runbook said "kill it, or wait if it's nearly done" — with no
  duration to decide on.** This turned out to be a *data model* gap, not
  a display one: the collector recorded when a blocking edge *appeared*
  and never when it *resolved*, so duration was uncomputable from the
  stored data. Added an `ended_ts` column (with a real in-place
  migration, since `CREATE TABLE IF NOT EXISTS` silently does nothing on
  an existing table) and edge-disappearance detection in the collector.
- **A serious bug I introduced two rounds ago, found while debugging the
  above: the priming poll silently swallowed in-progress blocking
  chains.** A collector starting or restarting *during* an incident
  recorded **zero** blocking edges — blind in exactly the situation it
  exists for. Priming legitimately suppresses *session* restart noise;
  it should never have suppressed blocking edges, which are rare and
  always significant. Verified before and after: 0 edges → 1 edge,
  with a permanent regression test.

The `diff` command also gained `Longest block held: 0s -> 47s` for free
once duration became available — often the single clearest indicator of
how much worse the incident window was.

### Round: collector liveness, plus two resilience bugs found while adding it

- **Silent collector death was undetectable.** Covered in the
  [`whyslow status`](docs/operations.md#whyslow-status--whyslow-doctor--is-this-thing-actually-collecting)
  section of the operations docs -- the core problem was that diff-based writes make
  "healthy but quiet" and "dead three weeks ago" produce identical data
  (nothing at all). Heartbeats fix it; `tests/status_smoke.py` asserts
  all three states are correctly distinguished, including that a *stale*
  collector must not be told its empty tables are "normal".
- **The Puma collector died permanently on any HTTP error.** A Puma
  restart, a redeploy, or a briefly-unavailable control app would kill
  the collector for good -- and with no heartbeat, silently. Now retries
  with backoff and logs recovery.
- **The CloudWatch collector died permanently on any AWS API error.**
  Same class of bug: transient throttling or a credential refresh would
  end collection permanently. Now retries with backoff, capped at 5
  minutes.

Both resilience bugs were found *because* adding heartbeats forced the
question "what happens when this collector stops?" -- neither was
visible from reading the happy path.

### Earlier round: SQLite concurrency and startup locking

Three collector processes (Postgres, Puma, CloudWatch) all write to the
same SQLite file concurrently, and `whyslow`/`diff` read from
it while collectors keep running -- worth actually checking rather
than assuming.

- **Multi-process concurrent writes: tested.** Forced genuine
  simultaneous writes across 3 threads with a synchronization barrier
  and large batches -- zero errors. Confirmed *why*: Python's
  `sqlite3.connect()` defaults to a 5-second busy-timeout, verified by
  explicitly setting `timeout=0` on the same test (60 errors
  immediately) versus the actual default (0 errors).
- **`explain` running while a collector writes, at the collector's
  real 1-second cadence: tested, no bug.** 99/99 reads succeeded,
  0.2-0.4ms each. An earlier version of this test used a writer with
  *zero* delay between writes and got 0 completed reads in 5 seconds --
  that result was an artifact of an unrealistic, far-more-adversarial
  writer than the real collector ever produces, not a real finding.
  Worth stating plainly rather than reporting the scary number: the
  first test was wrong, not the code.
- **Switched to WAL mode.** `PRAGMA journal_mode=WAL` means readers and writers do not block
  each other at all, rather than relying on busy-timeout retries. This
  wasn't needed to pass any test above, but it directly targets the one
  scenario most relevant to this tool's own purpose: a severe incident
  producing a large write burst (hundreds of session rows in a single
  poll) while someone runs `whyslow` in real time.
  `tests/burst_concurrency_smoke.py` simulates exactly that (300-row
  bursts x 10, with concurrent reads).
- **A later Python 3.9 minimum-version run found a first-open race.**
  Enabling WAL itself needs a write lock; two processes creating the
  store simultaneously could make one fail immediately with `database
  is locked`. Initialization now uses a 30-second busy timeout plus
  bounded retries, and the same burst test covers simultaneous opens.
  Python 3.9 support was dropped separately because the AWS SDK ended
  support for it in April 2026; CI now tests Python 3.10 and 3.12.

### Round before that: IO-bound detection gap, CloudWatch timing bug

- **The resource-contention path silently ignored IO-bound sessions.**
  `find_resource_candidates()` correctly bucketed both `cpu` and `io`
  categories, but `check_resource_signals()` only ever read the `cpu`
  bucket -- despite the contributor being labeled **"resource
  contention (CPU/IO)"**. A pure disk-bound incident (bulk `COPY`,
  heavy scans, backup, WAL pressure -- zero `cpu`-category sessions at
  all) produced **zero signals**, no matter how severe. Fixed to check
  both buckets independently, each labeled by which one actually fired.
  `tests/io_contention_smoke.py` proves an IO-only scenario is now
  correctly detected, and correctly does *not* claim CPU-bound when
  there were no CPU-bound sessions.
- **CloudWatch collector queried `EndTime=now` exactly, risking a
  silently missing most-recent datapoint** — verified against a real
  mocked CloudWatch (`moto`): a metric planted at the literal query
  instant returned zero datapoints, while the same metric a few seconds
  older returned correctly. This matches a well-known real-world
  CloudWatch caveat (publication lag on the most recent period) and
  isn't a mock-only artifact. Fixed by pulling the query window back by
  a buffer instead of querying up to the exact current instant.
  `tests/cloudwatch_smoke.py` now exercises the real collector code
  end-to-end against a mocked AWS account -- the CloudWatch collector
  is no longer the one completely untested component.

### Earliest round: silent timezone bug, Rails query-tag matching

- **Silent timezone bug in `parse_time`, now fixed.** `--from 23:30`
  was being interpreted in the *local system timezone of whoever runs
  the CLI* -- but collectors store UTC epoch time, and dashboards
  (CloudWatch, logs) show UTC. An engineer on a laptop in a different
  timezone than the collector got the **wrong window with no error at
  all** -- silently wrong is worse than crashing, and during an
  incident is the worst possible time for it. Proven with
  `tests/timezone_smoke.py`, which runs `parse_time()` under
  `TZ=America/Los_Angeles` and confirms the result no longer depends
  on it (was off by ~7 hours before the fix; verified via manual
  reproduction, output kept in the test).
- **Maintenance-pattern matching silently broke on Rails/ActiveRecord
  query tags, now fixed.** This stack is Puma -- almost certainly
  Rails -- and Rails commonly prepends a comment for query tagging
  (the marginalia gem, or Rails 7+'s built-in query log tags), e.g.
  `/*application:MyApp*/ REINDEX TABLE orders`. Matching from literal
  string-start missed these entirely, degrading confidence for exactly
  the REINDEX/VACUUM FULL/COPY cases this tool cares about most.
  `label_query` now strips leading comments first; `tests/
  query_label_smoke.py` covers block comments, line comments, and
  stacked combinations of both.
