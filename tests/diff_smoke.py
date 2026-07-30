import shutil
import time

from whyslow.storage import Store
from whyslow import diff as diff_mod
from whyslow import explain as explain_mod

DB_PATH = "/tmp/whyslow_smoke3/store.sqlite3"
shutil.rmtree("/tmp/whyslow_smoke3", ignore_errors=True)

store = Store(DB_PATH)
t0 = time.time()

# --- baseline: quiet, healthy period ---
baseline_start = t0
store.write_sessions(
    [(101, "client backend", "app_role", "web-1", "active", "other", "SELECT * FROM orders WHERE id = $1")],
    ts=t0 + 1,
)
store.write_puma_stat(host="web-1", backlog=0, pool_capacity=16, max_threads=16, running=2, rss_mb=300.0, ts=t0 + 1)
store.write_cloudwatch_metric("CPUUtilization", 38.0, ts=t0 + 1)
baseline_end = t0 + 5

# --- incident: the reindex scenario, offset later ---
incident_start = t0 + 10
store.write_sessions(
    [(210, "client backend", "analytics_role", "batch-host", "active", "cpu", "REINDEX TABLE orders")],
    ts=incident_start + 1,
)
store.write_blocking_edges(
    [(512, "web-3", 210, "batch-host", "analytics_role", "REINDEX TABLE orders")],
    ts=incident_start + 2,
)
store.write_puma_stat(host="web-3", backlog=12, pool_capacity=0, max_threads=16, running=16, rss_mb=512.0, ts=incident_start + 3)
store.write_cloudwatch_metric("CPUUtilization", 91.0, ts=incident_start + 3)
incident_end = t0 + 20

store.close()
store = Store(DB_PATH)

print("=== whyslow (incident window) ===")
result = explain_mod.explain(store, incident_start, incident_end)
print(explain_mod.render(result, incident_start, incident_end))

print("\n=== whyslow diff (baseline vs incident) ===")
d = diff_mod.diff(store, baseline_start, baseline_end, incident_start, incident_end)
print(diff_mod.render(d))

assert "analytics_role" in d["incident"]["roles"] and "analytics_role" not in d["baseline"]["roles"]
assert "reindex" in d["incident"]["maintenance_labels"]
assert d["baseline"]["blocking_edges"] == 0 and d["incident"]["blocking_edges"] == 1
print("\nPASS: diff correctly shows analytics_role and reindex as newly appeared, blocking 0 -> 1")
