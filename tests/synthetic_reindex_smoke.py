import shutil
import time

from whyslow.storage import Store
from whyslow import explain as explain_mod

DB_PATH = "/tmp/whyslow_smoke2/store.sqlite3"
shutil.rmtree("/tmp/whyslow_smoke2", ignore_errors=True)

store = Store(DB_PATH)

t0 = time.time()

# analytics_role starts a REINDEX (matches maintenance pattern -> signal 3)
store.write_sessions(
    [(210, "client backend", "analytics_role", "batch-host", "active", "cpu", "REINDEX TABLE orders")],
    ts=t0,
)

# pid 512 (web-3) gets blocked by pid 210 shortly after
store.write_blocking_edges(
    [(512, "web-3", 210, "batch-host", "analytics_role", "REINDEX TABLE orders")],
    ts=t0 + 2,
)

# Puma backlog spikes on web-3 close in time -> signal 1
store.write_puma_stat(host="web-3", backlog=12, pool_capacity=0, max_threads=16, running=16, rss_mb=512.0, ts=t0 + 4)

store.close()

store = Store(DB_PATH)
result = explain_mod.explain(store, t0 - 5, t0 + 10)
print(explain_mod.render(result, t0 - 5, t0 + 10))

assert result["contributors"], "expected at least one contributor"
c = result["contributors"][0]
assert c["confidence"] == "High", f"expected High confidence, got {c['confidence']} (signals={c['signals']})"
print("\nPASS: 3/3 signals correctly yield High confidence")
