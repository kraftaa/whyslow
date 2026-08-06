# pg_blocking_pids() returns the whole chain, not the culprit

I load-tested a Postgres incident tool with 80 sessions all genuinely blocked
on the same row. It reported **3,161 blockers**. The correct answer was **1**.

That gap is worth understanding, because it isn't a bug in my code so much as a
mismatch between what `pg_blocking_pids()` is *for* and what I assumed it
returned.

## What I assumed

`pg_blocking_pids(pid)` sounds like it answers "who is directly blocking this
session?" So the naive collector query is: for every waiting session, record an
edge from it to each pid that's blocking it.

```sql
SELECT blocked.pid, blocking_pid.pid
FROM pg_stat_activity AS blocked
CROSS JOIN LATERAL unnest(pg_blocking_pids(blocked.pid)) AS blocking_pid(pid)
WHERE cardinality(pg_blocking_pids(blocked.pid)) > 0;
```

With one holder and 80 waiters, I expected 80 edges, all pointing at the holder.

## What it actually returns

`pg_blocking_pids()` returns the **entire transitive wait-queue chain**, not
the direct predecessor. Postgres's row-lock queue is ordered: waiter N is
queued behind waiters 1..N-1, all of them behind the true holder. So
`pg_blocking_pids()` for waiter N includes *everyone ahead of it in line* —
waiters 1 through N-1 **plus** the holder.

That means the edge count isn't linear in the number of waiters. It's
quadratic:

```
edges = Σ (position in queue) ≈ N²/2
```

For N = 80, that's ~3,160 edges. Measured: **3,161**. Storing that is an O(N²)
write amplification, and — much worse — `explain` would name dozens of
different sessions as "the blocker" during precisely the incident where there
is exactly **one** root cause. The tool would be least trustworthy exactly when
it mattered most.

## The fix is one predicate

The true root is the session that is blocking others but is **not itself
waiting on anyone**:

```sql
FROM pg_stat_activity AS blocked
CROSS JOIN LATERAL unnest(pg_blocking_pids(blocked.pid)) AS blocking_pid(pid)
JOIN pg_stat_activity AS blocking ON blocking.pid = blocking_pid.pid
WHERE cardinality(pg_blocking_pids(blocked.pid)) > 0
  -- Only the true root blocker: blocking others, waiting on no one.
  AND cardinality(pg_blocking_pids(blocking.pid)) = 0;
```

Same 80-waiter scenario, after: **3,161 edges → 1**, pointing at the actual
holder.

## The second-order lesson: root cause is not blast radius

Collapsing to the root fixes attribution but throws away a real number. Once
`blocking_edges` contains only roots, it can no longer tell you *how many*
sessions are stuck — only who's responsible. And "how bad is it" is a question
you very much want answered mid-incident.

The temptation is to reconstruct the count from the chain you just deleted.
Don't. The blast radius was already sitting in a cheaper place: the plain
session poll already tags every lock-waiter with `category = 'lock'`, with no
join and no `pg_blocking_pids()` call at all. So the tool reports two numbers
from two cheap queries:

- **root cause** — one edge, from the blocking query
- **blast radius** — the count of `category = 'lock'` sessions, from the
  session query

Two cheap, honest numbers instead of one expensive, misleading one.

## What transfers

A built-in that returns a **transitive closure** ("everyone in my way") is not
the same as one that returns a **root** ("who started this"), even when the name
suggests otherwise. Any time you `unnest` a reachability function into edges,
ask whether you want the closure or the frontier — the two differ by a factor
of N, and for causal attribution the closure is not just bigger, it's *wrong*.
This shows up far outside Postgres: dependency graphs, distributed traces,
incident correlation, "who imported this module." The convenience shape of the
API is rarely the diagnostic shape you need.
