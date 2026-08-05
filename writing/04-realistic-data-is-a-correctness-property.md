# 864,031 lines → 152, and 733 MB → 8 MB

Every test in the project used between one and six rows. The code was correct on
that data. Then I generated a *realistic* dataset — 48 hours at ~5 session
changes per second, which is ordinary churn on a busy database, not a
pessimistic estimate — and two bugs fell out immediately that months of
passing tests had hidden.

Neither was a logic error. Both were things that only exist at scale, and
therefore things that toy fixtures are structurally incapable of catching.

## Bug 1: the output was unreadable

A 48-hour `explain` produced **38 MB of text across 864,031 lines**. That's not
a report; it's a denial-of-service against the person reading it. Worse, the
command the runbook actually recommends — a 15-minute window — still produced
**4,500 lines**. The per-row timeline was never viable outside a demo.

The fix was to aggregate at render time: sessions collapse to one line per
minute per category. Blocking edges and events stay individual, because they're
rare and always significant — the whole value of the tool is *not* summarizing
those away. The timeline is capped at 120 lines with an explicit note about
what was omitted, so truncation is visible rather than silent.

**864,031 lines → 152.**

## Bug 2: a single query used 733 MB of RAM

Loading ~900k session rows into Python to count and correlate them cost
**733 MB** for one `explain`. That is a genuine OOM on the kind of host this is
meant to run on — a t3.small has 2 GB, and the systemd units set no memory
limit, so a wide window during an incident could take the whole box down.
Again: the one moment the tool exists for is the one moment it would fall over.

The fix was to push the aggregation down into SQL. The application layer only
ever needed *counts per minute per category* and *whether timestamps fall near
each other* — and both of those survive aggregation perfectly. Nothing was
lost, because individual session rows were never displayed or reasoned about
individually. Memory is now bounded by `(minutes × categories)` instead of by
row count:

**733 MB → 8 MB. 7.3s → 0.6s.**

Two follow-ups made the fix durable: the benchmark became an **asserting
regression test** (not a script you run and eyeball), and the systemd units
gained `MemoryMax=512M`, so a future regression gets killed at 512 MB instead
of taking the host with it.

## What transfers

**Test data volume is a correctness property, not a performance nicety.** Two
distinct bugs — an unreadable-output cliff and an OOM — were both invisible on
six-row fixtures and both obvious on realistic data. If your tests only ever
run on toy inputs, you are not testing the system you ship; you're testing a
smaller system that happens to share its code.

Two concrete practices:

1. **Put realistic scale in the suite.** Generate a dataset sized to your P50
   production case and assert on line count, memory, and latency — not just
   correctness of values. The regression test that caught this asserts `8 MB`,
   not "runs without error."

2. **Aggregate at the layer that owns the data.** If the application only ever
   consumes an aggregate, compute the aggregate where the data lives (SQL, the
   storage engine, the query) and never materialize the rows in application
   memory. Pulling 900k rows into Python to `len()` them is the same mistake as
   `SELECT *` followed by a client-side filter — the boundary you cross is the
   expensive part.

The meta-lesson is the same one that runs through every bug in this project:
reasoning about whether code works is not the same as watching it work on the
data it will actually see.
