# Runbook: "the web servers are slow, what's the reason?"

## Step 0 — this week, not during the incident

Nothing below works unless collectors were **already running** before
things went wrong. This is the one limitation that can't be engineered
away: `pg_stat_activity` is a live view with no history.

**Point the DSN at the cluster WRITER endpoint, not the reader.** The
reader looks like the safer choice (read-only!) and is the wrong one —
write-lock contention happens on the writer and is invisible from a
reader. `whyslow status` will flag this and exit non-zero if you get it
wrong.

```bash
# on the host with DB credentials -- WRITER endpoint
whyslow collect-pg --dsn "$WHYSLOW_PG_DSN" --db /var/lib/whyslow/store.sqlite3

# on each web host
whyslow collect-puma --host-name $(hostname) --stats-url http://127.0.0.1:9293/stats
```

Use the systemd units in `deploy/` so they survive reboots. Then verify
it's actually working, and check again occasionally:

```bash
whyslow status     # exits non-zero if any collector is stale
```

Also add these two one-liners, they cost nothing and pay off later:

```yaml
# config/database.yml -- makes DB activity attributable to a web host
production:
  application_name: <%= "web-#{Socket.gethostname}" %>
```

```bash
# end of your deploy pipeline
whyslow event --source deploy --kind "$VERSION" --payload "sha=$GIT_SHA"
```

---

## During the incident — three commands

### 1. Confirm you actually have data

```bash
whyslow status
```

If a collector is STALE, stop here — you're flying blind for this
window, and you should say so out loud rather than let people argue
from nothing. Skip to the manual fallback at the bottom.

### 2. What happened

```bash
whyslow --last 15m
```

### 3. What changed vs. before it started

```bash
whyslow diff --last 15m --baseline-last 15m
```

---

## Reading the answer

The output names **one of two categories**. They look identical from the
app side (slow requests, Puma backlog) but have completely different
fixes — telling them apart is the entire point of the tool.

### `[blocking]` — someone is holding a lock

```
Observed contributors
- analytics_role (reindex) [blocking] -- High confidence (4/4 signals)
```

The line gives you everything needed to act:

```
- analytics_role (reindex) [blocking] -- High confidence (4/4 signals)  pid=928  held=3s  blocking=12 session(s)
```

**What it means:** one root session is holding a lock; everything else
is queued behind it.

**What to do now:** use `held=` to decide. Still climbing after minutes
→ kill it. A few seconds and falling → it's probably resolving itself.
If the blocker is still active at window end, `explain` prints the exact
command for you:

```sql
SELECT pg_cancel_backend(928);    -- polite: cancels the query
SELECT pg_terminate_backend(928); -- forceful: kills the connection
```

Verify the pid is still the same session first — Postgres reuses pids.

**What to fix later:** whatever ran that statement against the primary
during traffic. If it's a maintenance operation, most have a
non-blocking form (`CREATE INDEX CONCURRENTLY`, `REINDEX CONCURRENTLY`).

### `[resource_contention]` — the instance is saturated

```
Observed contributors
- resource contention (CPU/IO) [resource_contention] -- High confidence (4/5 signals)
```

**What it means:** no single lock holder. Multiple things are competing
for CPU or disk. Check whether the evidence says CPU-bound or IO-bound —
they point different directions.

**What to do now:** find the heaviest sessions and stop the ones that
are batch work rather than user traffic. There's no single pid to kill.

**What to fix later:** this is usually a scheduling problem, not a
tuning problem. Move batch work (dbt, bulk loads, reports) off the
primary to a read replica, or out of peak hours.

### No contributors found

**First, check which of these two you got — they mean opposite things.**

```
!! NO COLLECTOR DATA FOR THIS WINDOW !!
```

Nothing was watching. This is **not** evidence the database was fine.
Conclude nothing; go to the manual fallback below.

```
Observed contributors
  (none found -- no blocking edges or sustained resource pressure in this window)
```

This one only appears when collectors actually covered the window, so
it **is** a real finding: the database is probably not your problem.
Look at the app layer — worker exhaustion from a slow external API, GC
pauses, memory pressure. Ruling the DB out with evidence beats arguing
about it.

If you see `!! INCOMPLETE COVERAGE !!`, read which role is missing — it
names exactly what can't be ruled out:

```
  postgres    missing 100% of window -> cannot rule out: blocking chains and database session pressure
  puma        covered (puma:web-3)
```

A live collector for one role never validates conclusions that depend on
a different one. Puma being up tells you nothing about whether the
database was blocking.

---

## Why the confidence label matters less than the evidence

Read the `Evidence` block, not the label. `High` just means most
checkable signals fired. Two unrelated things co-occurring can still
score Medium — the signal list is what lets you judge that yourself.

---

## Manual fallback (no collectors running)

Run all four at once. Slower than the tool, faster than tab-switching
between dashboards:

```sql
-- what's running, by state
SELECT state, count(*) FROM pg_stat_activity GROUP BY state ORDER BY count(*) DESC;

-- what everything active is waiting on
SELECT wait_event_type, wait_event, count(*)
FROM pg_stat_activity WHERE state != 'idle'
GROUP BY 1,2 ORDER BY count(*) DESC;

-- live blocking chains
SELECT pid, application_name, pg_blocking_pids(pid) AS blocked_by, query
FROM pg_stat_activity
WHERE cardinality(pg_blocking_pids(pid)) > 0;

-- longest-running right now
SELECT pid, now() - query_start AS duration, state, query
FROM pg_stat_activity WHERE state != 'idle'
ORDER BY duration DESC LIMIT 10;
```

Reading it manually, same two categories:

- Lots of `Lock` waits + entries in `pg_blocking_pids` → **blocking**
- Lots of `active` with `wait_event IS NULL` → **CPU contention**
  (Postgres has no "CPU" wait event; active-and-waiting-on-nothing means
  it's actually running on a core)
- Lots of `IO` waits → **disk contention**

Then check Puma's `/stats` on a web host for `backlog` and
`pool_capacity` to confirm the app side is saturated too.

---

## After it's over

```bash
# capture the evidence into the incident doc while it's still in retention
whyslow --from 11:42 --to 11:47 > incident-2026-07-28.txt
```

Session data ages out after 48h; blocking edges after 30 days.
