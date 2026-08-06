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

# --- regression: summarize_window must aggregate sessions in SQL, not load
# every row. A wide window with many rows would OOM if diff still called the
# raw sessions_in; assert both the counts and the distinct dimensions survive
# aggregation, and that sessions_in is never invoked here. Isolated store so a
# lingering unresolved edge from the scenario above doesn't leak roles in.
shutil.rmtree("/tmp/whyslow_smoke3_agg", ignore_errors=True)
agg_store = Store("/tmp/whyslow_smoke3_agg/store.sqlite3")
agg_start = 1_000_000.0
n_rows = 500
for i in range(n_rows):
    agg_store.write_sessions(
        [(700 + i, "client backend", f"role_{i % 3}", f"host_{i % 4}", "active", "cpu", "REINDEX TABLE t")],
        ts=agg_start + 1 + (i % 60),
    )
agg_end = agg_start + 120

agg_store.sessions_in = lambda *a, **k: (_ for _ in ()).throw(  # type: ignore[assignment]
    AssertionError("diff.summarize_window must not call the unbounded sessions_in")
)
summary = diff_mod.summarize_window(agg_store, agg_start, agg_end)
assert summary["session_events"] == n_rows, summary["session_events"]
assert summary["category_counts"] == {"cpu": n_rows}, summary["category_counts"]
assert summary["roles"] == {"role_0", "role_1", "role_2"}, summary["roles"]
assert summary["apps"] == {"host_0", "host_1", "host_2", "host_3"}, summary["apps"]
assert summary["maintenance_labels"] == {"reindex"}, summary["maintenance_labels"]
print(
    f"PASS: summarize_window aggregates {n_rows} session rows in SQL "
    "(counts, roles, apps, labels intact) without the unbounded row load"
)
