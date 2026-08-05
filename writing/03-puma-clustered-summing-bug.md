# Summing across workers hides the one that's on fire

Puma in clustered mode runs several worker processes, and its stats endpoint
reports each worker separately. To store one number per host, you have to
aggregate across workers. The collector summed them.

That is a bug, and it's the same bug as reporting mean latency. It's worth
spelling out because "how do I combine these N numbers into one" looks like a
formatting decision and is actually a modeling decision — one that determines
whether the tool can see the failure it exists to detect.

## Why summing is wrong here

The two Puma numbers that matter during a slowdown are `backlog` (requests
queued, waiting for a thread) and `pool_capacity` (threads free to accept
work). The failure you care about is **saturation**: one worker pinned at zero
free capacity with requests piling up, while its idle peers make the fleet
*look* fine on average.

Summing erases exactly that signal. Consider a real disagreement case from the
test suite:

| worker | backlog | pool_capacity |
|--------|---------|---------------|
| A      | 20      | 0             |  ← saturated, this is the incident
| B      | 3       | 14            |
| C      | 2       | 13            |

- **Sum:** `backlog = 25`, `pool_capacity = 27`. Both numbers are meaningless.
  A `pool_capacity` of 27 reads as "plenty of headroom" — the fleet looks
  healthy while worker A is faceplanting.
- **Worst worker:** `backlog = 20`, `pool_capacity = 0`. Correct. A saturated
  worker is visible, which is the whole point.

The aggregate `pool_capacity = 27` isn't just noisy — it's actively
reassuring at the moment you most need alarm.

## The fix

Report the worst worker, not the total: **max backlog, min pool_capacity**.
Saturation of *any* worker survives aggregation; the healthy peers can no
longer dilute it away.

## Why it hid for so long

The Puma collector had **zero test coverage** before this pass. The bug wasn't
found by reading the code — summing looks perfectly reasonable in isolation. It
was found by building a fake Puma stats server (`tests/fake_puma.py`, no Ruby
stack required), feeding it a clustered response where one worker was hot, and
watching the stored number come out reassuring. The fix shipped with a
regression test built from the disagreement case above.

## What transfers

When you reduce many measurements to one, the reduction function encodes what
you believe the failure mode is:

- **Sum** answers "total load." Right for throughput, capacity planning.
- **Mean** answers "typical case." Right for almost nothing operational.
- **Max / min** answers "is *anything* in trouble." Right for saturation,
  tail latency, SLO violations, the-one-shard-that's-hot.

The same mistake is reporting mean response time instead of p99, or average CPU
across a fleet instead of the hottest node. If the thing you're hunting is "one
component is failing while the rest are fine," any aggregate that averages the
failure against the successes will lie to you — and it will lie *most*
confidently right when the failure is localized, which is the common case.
Choose the reducer for the failure mode, not for tidiness.
