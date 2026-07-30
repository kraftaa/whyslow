# whyslow

**Why is it slow?** Deterministic, evidence-first incident reconstruction
for Aurora Postgres + Puma — not another monitoring dashboard.

A deterministic, evidence-first CLI that reconstructs why web servers
slowed down — instead of a team manually cross-referencing Puma stats,
CloudWatch, and `pg_stat_activity` mid-incident.

```
whyslow --from 11:42 --to 11:47
```

**During an incident, go straight to [RUNBOOK.md](RUNBOOK.md)** — what to type, and what each answer means.

## Why this exists

"Production is slow" usually collapses into one of a few root causes —
a Postgres lock chain, an app-server thread pool pinned waiting on
slow queries, or a resource-contention event (a reindex, a bulk load,
autovacuum, a cronjob — anything sharing the DB instance's CPU/IO).
Nobody wants to manually correlate three dashboards' timestamps by eye
at 2am. This does it from data already being collected.

## How it works, in six steps (no model, no statistics)

1. Fetch blocking edges, Puma stats, metrics, and events inside the
   requested window; aggregate high-volume session changes by minute
   and wait category in SQLite so wide windows stay bounded.
2. Merge everything by timestamp into one plain-English timeline.
3. Find candidates: any pid that appears as a *blocker*, or any
   sustained cluster of CPU/IO-bound sessions.
4. For each candidate, check a **fixed, readable list** of named
   signals — each one a lookup: does a row exist, is a timestamp
   within `COOCCURRENCE_WINDOW_SECONDS` of another.
5. Count how many named signals fired and apply fixed thresholds:
   at least 75% High, at least 50% Medium, anything above zero Low.
6. Render the timeline, contributors, and the exact evidence lines
   that caused each confidence label.

Every constant that drives step 4/5 lives at the top of `explain.py`,
in plain sight, inspectable and editable — nothing is tuned in a way
you can't read.

## Why detection needs no named integrations

Aurora is fully managed: nothing can consume DB-instance CPU/IO or
hold a lock without going through a Postgres session. Reading
`backend_type` alongside `pg_stat_activity` means `autovacuum worker`,
`walsender`, and `parallel worker` sessions are distinguished from
normal client backends automatically — so autovacuum, a stray
cronjob, an ad hoc script, or anything you've never named shows up
the same way dbt or a known job would, with zero setup. dbt/Airflow/
deploy-webhook event ingestion is optional future enrichment, not the
detection mechanism.

## Setup

**Do this first, independent of installing anything** — tag DB
connections by host so activity is attributable:

```yaml
# config/database.yml
production:
  application_name: <%= "web-#{Socket.gethostname}" %>
```

```bash
# Python 3.10+
pip install -e .
# Include this extra on the collector host when CloudWatch is enabled:
pip install -e ".[cloudwatch]"
```

Run every collector on one collector host and point every process at the
same SQLite file. The Puma control endpoints must be reachable from that
host over a private TLS connection or tunnel; running collectors
independently on each web server creates isolated stores that cannot be
correlated.

Use long-lived processes (systemd unit, supervisor, whatever you already
use):

```bash
export WHYSLOW_PG_DSN="postgresql://user:pass@host/db"
export WHYSLOW_PUMA_TOKEN="replace-me"

whyslow collect-pg    --db /var/lib/whyslow/store.sqlite3
whyslow collect-puma  --host-name web-3 --stats-url https://web-3.internal:9293/stats --db /var/lib/whyslow/store.sqlite3
whyslow collect-cw    --db-instance-id my-aurora-cluster --db /var/lib/whyslow/store.sqlite3
```

`--dsn` and `--token` remain available for local testing, but environment
variables keep secrets out of process arguments in production.

Then, after (or during) an incident:

```bash
whyslow --from 11:42 --to 11:47
```

Times accept ISO-8601 (`2026-07-30T23:55:00Z`), `HH:MM`,
`HH:MM:SS`, or raw epoch seconds. Clock-only windows automatically roll
across UTC midnight when `--to` is earlier than `--from`.

## Root cause vs. blast radius — a real fix, not a hypothetical one

Load-testing with 80 sessions genuinely blocked on the same row
surfaced a real bug: `pg_blocking_pids()` returns the *entire
transitive wait-queue chain*, not just "who's directly blocking me."
80 blocked waiters produced **3,161** blocking edges — Postgres's
row-lock queue chains each waiter behind the ones ahead of it, so
`pg_blocking_pids()` for waiter N includes waiters 1..N-1 too, not
just the true root holder. Storing that is an O(N^2) explosion, and
`explain` would have reported dozens of different "blockers" during
exactly the incidents where there's really only one root cause.

The fix: only report an edge where the blocking session is not itself
blocked by anyone (`cardinality(pg_blocking_pids(blocking.pid)) = 0`)
— the one true root. This collapsed the same 80-waiter scenario from
3,161 edges to **1**, correctly pointing at the actual root cause.

That correctly fixes root-cause attribution, but it also means
`blocking_edges` alone can no longer tell you *how many* sessions are
actually stuck — only who's ultimately responsible. Blast radius is
reported separately, from data that was already being collected
anyway: every waiting session already shows up as `category = 'lock'`
in the plain, cheap, no-join `SESSIONS_SQL` poll. `whyslow`
and `whyslow diff` both surface this count alongside the root-cause
edges, not instead of them. Two cheap, honest numbers instead of one
expensive, misleading one.

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

The CloudWatch collector also preserves each datapoint's real timestamp
and deduplicates overlapping publication-lag queries. The temporal
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

- **Silent collector death was undetectable.** Covered above under
  `whyslow status` -- the core problem was that diff-based writes make
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

## Recording deploys and job markers

```bash
# one line at the end of your deploy pipeline
whyslow event --source deploy --kind "v1.2.3 released" --payload "sha=$GIT_SHA"

# or from a dbt/Airflow wrapper
whyslow event --source dbt --kind "nightly_rollup started"
```

These appear on the timeline (`11:42:03  deploy event: v1.2.3 released`)
**and** count as a correlation signal when they land close in time to
the pressure -- within 60s, since a deploy or batch job can take a
minute to manifest as database load.

This closes a loop that was dangling: the `events` table was read and
rendered from the very first version, but had **no writer at all** and
never influenced any signal -- so the spec's own example output
("Deployment detected") and its "overlaps a dbt run window" signal were
both unreachable in practice. Found by grepping for callers of
`write_event` and finding none.

## Confidence is a ratio, and the denominators are real

The two contributor categories have genuinely different numbers of
checkable signals:

| Category | Signals |
|---|---|
| blocking (4) | Puma backlog, host-tagged blocked sessions, maintenance query pattern, nearby event |
| resource contention (5) | CPU spike, IO spike, CloudWatch CPU, Puma backlog, nearby event |

Thresholds, stated in plain sight: `>=75%` High, `>=50%` Medium,
anything above zero Low.

This replaced a hardcoded `/3` that could produce literal **"4/3
signals"** output once the IO signal was added -- nonsense, and
corrosive to a tool whose entire pitch is that every number is
inspectable. `tests/event_smoke.py` asserts signal counts can never
exceed their denominator again.

## `whyslow status` — is this thing actually collecting?

The single most important command, because this tool can only explain
incidents **from the moment collectors started running**. If a collector
died three weeks ago, you do not want to discover that at 2am.

```bash
whyslow status
```

```
Collectors
  ✓ postgres                 alive  last heartbeat 1s ago  (interval=1.0s)
  ✗ puma:web-3               STALE  last heartbeat 4.2d ago

  WARNING: a stale collector means incidents during that gap
  cannot be explained. Check the process is still running.

Data coverage
  session_changes            2841 rows   2026-07-26 09:00:00Z -> 2026-07-28 11:47:00Z  (50.8h)
  blocking_edges               17 rows   2026-07-26 14:22:00Z -> 2026-07-28 11:44:00Z  (45.4h)
```

**Why heartbeats exist, rather than just counting rows:** collectors
write *diffs*, so a healthy collector watching a quiet database writes
**zero rows** — identical to a collector that died weeks ago. Row counts
cannot distinguish those two states; heartbeats can. This was a real
blind spot until it was found and fixed.

Exits non-zero if any collector is stale, so it works as a monitoring
check (cron, Nagios, a readiness probe), not just something read by eye.

## Relative time windows

Computing exact UTC timestamps by hand during an incident is real
friction, so `--last` is supported everywhere a window is:

```bash
whyslow --last 15m
whyslow diff --last 15m --baseline-last 15m   # baseline = the 15m just before
```

`--from`/`--to` still work (always UTC — see the timezone fix below).

## Deployment

`deploy/` contains systemd units for Postgres and CloudWatch plus a
templated Puma unit (`whyslow-collect-puma@web-3`). Run all of them on
the same collector host. Each Puma instance reads its target URL and
token from `/etc/whyslow/puma/<host>.env`.

Enable the independent retention timer as well:

```bash
systemctl enable --now whyslow-prune.timer
```

This runs `whyslow prune` hourly, so expired data is removed even if the
Postgres collector is unavailable. The collector units use
`Restart=always` with `StartLimitIntervalSec=0` — a collector that gives
up retrying is a collector that silently isn't there when it matters —
and read credentials from an `EnvironmentFile` rather than command-line
arguments, since a DSN passed as an argument is visible in `ps` output
to every user on the box. SQL string/numeric literals and comments are
removed before query evidence is stored, query text is capped at 2 KiB,
and the SQLite file is created with mode `0600`.

## Verified with a real Postgres instance

This isn't a hand-waved design doc — `tests/live_blocking_smoke.py`
opens two real concurrent transactions against a live Postgres 16
instance, creating a genuine lock-wait, while the actual collector
polls `pg_stat_activity` and `pg_blocking_pids()` concurrently. Real
output from that run:

```
Observed contributors
- postgres [blocking] -- Low confidence (1/4 signals)

Evidence
  postgres:
    ✓ blocked sessions tagged to host(s): analytics_role_session
```

Correctly Low, not High — because that run had no Puma data and the
query wasn't a maintenance pattern, so only 1 of 4 signals fired. It
also captured `autovacuum launcher`, `checkpointer`, and `walwriter`
automatically via `backend_type`, with no code written for any of
them.

`tests/synthetic_reindex_smoke.py` confirms the 75% threshold path —
a REINDEX blocking a tagged host with a corroborating Puma backlog
spike — correctly yields **High** confidence with three of four named
signals and all three evidence lines present.

`tests/load_bench.py` and `tests/blocking_load_bench.py` measure
actual per-poll cost under load rather than assuming it: ~3-5ms
average against a 1000ms budget at ~95 concurrent sessions, and
~4-5ms average even with a real 80-session blocking chain, after the
root-cause fix above. Not part of CI (slower, connection-hungry) —
run manually against a local Postgres to reproduce.

## Honest limits

- **Only reconstructs incidents from the moment collectors were
  running.** Cannot retroactively explain anything from before
  install — there is no way around this with a self-hosted collector;
  it is not a limitation to be engineered away, it is what "your own
  lightweight collector, no vendor lock-in" costs you.
- **1-second polling can miss sub-second blocking events.**
- **Confidence is a named heuristic signal count, not a statistical
  or causal guarantee.** Two unrelated things co-occurring can still
  produce a Medium/High label — read the Evidence section, don't
  just read the label.
- **Query-text pattern matching for maintenance labels
  (`REINDEX`/`VACUUM FULL`/`COPY`/`CREATE INDEX`) is best-effort
  and cosmetic only** — it never gates detection, only readability.
- **Sanitized query structure is still operational data.** Literal values
  and comments are removed, but statement types and relation names remain
  visible by design. Treat the mode-`0600` SQLite store as sensitive.
- **CloudWatch collector is real code, not live-tested** (requires
  AWS credentials this environment doesn't have). Everything else in
  this README is demonstrated against a real running Postgres
  instance, not simulated.
- **Puma collector expects `activate_control_app` enabled and reachable
  from the collector host.** Puma worker RSS is not collected because
  the control-app stats do not expose it.

## `whyslow diff` — a healthy baseline vs the incident window

```bash
whyslow diff --baseline-from 11:30 --baseline-to 11:35 --from 11:42 --to 11:47
```

Deliberately narrower than `explain`: no signals, no confidence, no
inferred cause — just counts and set differences (roles/apps/
maintenance-query-patterns that appeared or disappeared, blocking edge
count, max Puma backlog, max CloudWatch CPU). Safe to add without
touching the causality question, because it never claims one.

Verified in `tests/diff_smoke.py`: a quiet baseline vs. the REINDEX
scenario correctly shows `analytics_role` and `reindex` as newly
appeared and blocking edges going 0 -> 1.

## Reliability, retention, and Puma coverage

Four gaps closed after an audit, not speculative additions:

- **Retention/pruning is implemented and tested** (`Store.prune()`,
  `whyslow prune`, and `tests/prune_smoke.py`) — session/Puma data ages
  out after 48h, blocking edges and CloudWatch metrics after 30 days.
  The systemd timer runs independently of collector health.
- **Heartbeat staleness uses each collector's configured interval.**
  A collector intentionally running every 30 seconds no longer gets
  judged against the one-second default; older stores retain safe
  role-based fallbacks.
- **The Postgres collector now reconnects with exponential backoff**
  instead of dying if the connection drops mid-poll — plausible
  exactly during a severe incident, which is the one moment this tool
  cannot afford to go silent.
- **A priming poll on every (re)start** establishes the "already seen"
  baseline without writing it, so a restart no longer reports every
  currently-active session as newly appeared.
- **The Puma collector had zero test coverage before this pass.**
  `tests/fake_puma.py` + `tests/puma_smoke.py` exercise both
  single-mode and clustered-mode parsing against a real HTTP server
  (no Ruby stack required). This also caught a real bug: clustered
  mode was *summing* stats across workers, which hides a single
  saturated worker among idle ones. Fixed to report the worst worker
  (max backlog, min pool_capacity) instead — verified with a
  synthetic case where summing and worst-worker aggregation disagree
  (sum: backlog=25, pool_capacity=27 -- both wrong; worst-worker:
  20 and 0 -- correct).

## Not yet built

- Automatic dbt (`run_results.json`) / Airflow polling — `whyslow event`
  covers this manually with one line in a job wrapper, which is simpler
  and works for jobs of any kind; automatic parsing is only worth adding
  if the manual call proves too easy to forget
- A "recommend a fix" step once a pattern repeats — diagnosis only
  for now
- Anything resembling request-latency/APM instrumentation. Deliberately
  out of scope — it pulls this back toward generic monitoring, which
  is exactly the crowded, already-served space this tool exists to
  avoid.
- CloudWatch collector is now tested against a mocked AWS account
  (`moto`) but still not run against a **real** AWS account -- the
  remaining gap is smaller (auth/IAM/region edge cases in practice),
  not the core query logic, which is now exercised end-to-end
