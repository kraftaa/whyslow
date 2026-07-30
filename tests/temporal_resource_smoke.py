import shutil
import time

from whyslow.storage import Store
from whyslow import explain as explain_mod


BASE = "/tmp/temporal_resource_smoke"
shutil.rmtree(BASE, ignore_errors=True)
t0 = (int(time.time()) // 60) * 60


def resource_contributors(store, start, end):
    result = explain_mod.explain(store, start, end)
    return [
        contributor
        for contributor in result["contributors"]
        if contributor["category"] == "resource_contention"
    ]


# Three isolated session changes across three minutes are not a spike.
# The old whole-window total incorrectly combined them into one incident.
scattered = Store(f"{BASE}/scattered.sqlite3")
row = (301, "client backend", "app", "web-1", "active", "cpu", "SELECT 1")
for offset in (1, 61, 121):
    scattered.write_sessions([row], ts=t0 + offset)
scattered.write_cloudwatch_metric("CPUUtilization", 95.0, ts=t0)

assert not resource_contributors(scattered, t0 - 1, t0 + 180), (
    "isolated changes in different minutes must not become one resource spike"
)
scattered.close()


# A real per-minute spike should be detected. A high CloudWatch datapoint
# far away in the same query window must not corroborate it, while a
# 60-second Average interval overlapping the spike must.
clustered = Store(f"{BASE}/clustered.sqlite3")
clustered.write_sessions(
    [
        (401, "client backend", "app", "web-1", "active", "cpu", "SELECT 1"),
        (402, "client backend", "app", "web-1", "active", "cpu", "SELECT 2"),
        (403, "client backend", "app", "web-1", "active", "cpu", "SELECT 3"),
    ],
    ts=t0 + 10,
)
clustered.write_cloudwatch_metric("CPUUtilization", 99.0, ts=t0 - 600)

contributors = resource_contributors(clustered, t0 - 700, t0 + 60)
assert len(contributors) == 1
signals = contributors[0]["signals"]
assert any("CPU-bound session spike" in signal for signal in signals)
assert not any("CloudWatch" in signal for signal in signals), (
    "a distant high-CPU datapoint must not corroborate this spike"
)

clustered.write_cloudwatch_metric("CPUUtilization", 92.0, ts=t0)
contributors = resource_contributors(clustered, t0 - 700, t0 + 60)
signals = contributors[0]["signals"]
assert any("CloudWatch CPUUtilization" in signal for signal in signals), (
    "an overlapping CloudWatch Average interval should corroborate the spike"
)
clustered.close()

print("PASS: resource incidents require per-minute clusters and overlapping evidence")
