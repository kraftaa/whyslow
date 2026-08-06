# Four ways a diagnostic tool lies "all clear"

The worst bug I have ever shipped did not crash, throw, or return a wrong
number. It printed:

> none found — no blocking edges or sustained resource pressure in this window

…during an incident, with **no collector running at all**. The runbook then
told the on-call engineer that this meant "the database is probably not your
problem — look at the app layer." A tool that had collected zero data sent a
human confidently in the wrong direction at 2am.

That is absence of evidence dressed up as evidence of absence. Fixing it once
was easy. Fixing the *class* took four rounds, because "the tool is blind but
says all-clear" has at least four distinct causes, and each earlier fix
revealed the next.

## The taxonomy

### 1. Nothing was collecting

No collector deployed, or it crashed. The heartbeat table was upsert-only — it
could answer "alive *now*?" but made past outages invisible, so a window with
zero coverage looked identical to a quiet, healthy window.

**Fix:** record historical coverage, one row per collector per minute. `explain`
now leads with a loud warning for an unwatched window and reserves the confident
"none found" phrasing for windows a collector actually covered.

### 2. *Some* collector was up, but not the one that answers the question

The obvious fix to #1 is to check "was anything running?" — and that reintroduces
the same bug in subtler form. Coverage was computed as one aggregate number. A
healthy Puma collector next to a dead Postgres collector read as **100%
covered**, and `explain` confidently reported "no blocking edges" — from the
collector that had never been up.

**Fix:** per-role coverage, and name the specific conclusion each gap
invalidates:

```
!! INCOMPLETE COVERAGE -- some conclusions below are unsupported !!

  postgres    missing 100% of window -> cannot rule out: blocking chains and database session pressure
  cloudwatch  missing 100% of window -> cannot rule out: instance CPU and connection counts
  puma        covered (puma:web-3)
```

The confident "none found" is now gated on the **database** collector
specifically, because blocking edges and session categories come from it
exclusively.

### 3. The right collector was up the whole window — and structurally blind

This is the one nobody tests for. Aurora clusters expose a *writer* endpoint and
a *reader* endpoint. Pointing the collector at the reader is the
safer-*looking* choice — read-only, no write risk — and it is exactly wrong:
write-lock contention happens on the writer and is **structurally invisible**
from a reader replica.

Coverage reads **100%**. The collector really was running, the whole window,
healthy. Every uptime-based check waves it through. And `explain` reports "no
blocking found" forever, because the instrument it's reading from is
constitutionally incapable of seeing the thing you asked about.

**Fix:** detect it at the source — `pg_is_in_recovery()` on connect — record it
in *every* heartbeat (not logged once at startup where nobody sees it), and
surface it in `explain`, in `status`, and as a non-zero exit code so monitoring
catches it.

### 4. The self-consistency bugs that hid behind all three

Fixing #1–#3 by *testing the output* rather than trusting it surfaced a last
layer: the coverage block said `postgres covered` while the warning directly
above it said the data was unusable; the "nothing was running here" message
fired for a collector that *was* running, just on the wrong endpoint. When your
tool's job is to be trustworthy under stress, its output contradicting itself is
itself a bug.

## The two axes

Every one of these is a failure to distinguish two independent questions:

1. **Presence** — was the instrument *up* over the window?
2. **Capability** — even if up, was it *able to see* the thing being asked
   about?

Almost every monitoring stack tracks axis 1 (uptime, heartbeats, "last seen").
Almost none track axis 2. Axis 2 is where the reader-endpoint bug lives: 100%
present, 0% capable. Time-based coverage turned out to be necessary but not
sufficient — a collector can cover a window perfectly and still be structurally
unable to answer your question.

## The pattern worth stealing

State it as a rule a diagnostic tool should enforce mechanically:

> A conclusion must declare which instruments it depends on, and the tool must
> refuse to assert that conclusion if those instruments were **absent** *or*
> **structurally blind** over the window in question.

"Refuse rather than degrade." A finding whose evidence source was missing or
incapable should not soften to low confidence — it should decline to be a
finding, and say why. This is the single most transferable idea in the whole
project, and it applies to every diagnostic tool anyone builds, in any stack:
the profiler that didn't sample the hot thread, the APM trace that dropped the
downstream span, the log query over a window your retention had already
evicted. All of them will happily tell you "nothing here" when the honest
answer is "I couldn't see."
