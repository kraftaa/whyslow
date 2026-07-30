import shutil
import subprocess
import sys
import time

from whyslow.storage import Store
from whyslow import explain as explain_mod

DB_PATH = "/tmp/event_smoke/store.sqlite3"
shutil.rmtree("/tmp/event_smoke", ignore_errors=True)

store = Store(DB_PATH)
t0 = time.time()

# A REINDEX blocks a tagged web host, with a Puma backlog spike.
# That's 3 of 4 blocking signals -- no event yet.
store.write_sessions(
    [(210, "client backend", "analytics_role", "batch-host", "active", "cpu", "REINDEX TABLE orders")],
    ts=t0,
)
store.write_blocking_edges(
    [(512, "web-3", 210, "batch-host", "analytics_role", "REINDEX TABLE orders")],
    ts=t0 + 2,
)
store.write_puma_stat(host="web-3", backlog=12, pool_capacity=0, max_threads=16, running=16, rss_mb=512.0, ts=t0 + 4)
store.close()

store = Store(DB_PATH)
before = explain_mod.explain(store, t0 - 5, t0 + 120)
b = [c for c in before["contributors"] if c["category"] == "blocking"][0]
print(f"without event: {len(b['signals'])}/{b['signals_possible']} signals -> {b['confidence']}")
assert len(b["signals"]) == 3
assert b["signals_possible"] == 4, "blocking path has 4 checkable signals"
store.close()

# Now record a deploy via the REAL CLI -- previously impossible, the
# events table had no writer at all.
result = subprocess.run(
    [sys.executable, "-m", "whyslow.cli", "event",
     "--source", "deploy", "--kind", "v1.2.3 released",
     "--payload", "sha=abc123", "--at", str(t0 + 1), "--db", DB_PATH],
    capture_output=True, text=True,
)
print(result.stdout.strip())
assert result.returncode == 0, f"whyslow event failed: {result.stderr}"

store = Store(DB_PATH)
after = explain_mod.explain(store, t0 - 5, t0 + 120)
print()
print(explain_mod.render(after, t0 - 5, t0 + 120))

a = [c for c in after["contributors"] if c["category"] == "blocking"][0]
assert len(a["signals"]) == 4, f"expected 4 signals with the deploy event, got {a['signals']}"
assert any("external event" in s for s in a["signals"]), "deploy should appear as a signal"
assert a["confidence"] == "High", f"4/4 signals should be High, got {a['confidence']}"

# The display-bug regression: denominators must never be exceeded.
for c in after["contributors"]:
    assert len(c["signals"]) <= c["signals_possible"], (
        f"nonsensical signal count {len(c['signals'])}/{c['signals_possible']} for {c['name']}"
    )

store.close()
print("\nPASS: events are writable via CLI, correlate as a real signal, and "
      "signal counts never exceed their denominator")
