# How whyslow works, and why it's trustworthy

Deliberately model-free: every conclusion is a lookup against rows the
collectors already wrote. This covers the mechanism, why it needs no named
integrations, the evidence it works, and what is out of scope.

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

## Verified against a real Postgres instance

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

## Not yet built / deliberately out of scope

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
- CloudWatch collection and Aurora writer failover are tested against a
  mocked AWS account/client (`moto` plus an RDS API fake), but still not run
  against a **real** AWS account -- the
  remaining gap is smaller (auth/IAM/region edge cases in practice),
  not the core query logic, which is now exercised end-to-end
