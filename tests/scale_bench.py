import os
import shutil
import sqlite3
import time

from whyslow.storage import Store
from whyslow import explain as explain_mod

DB_PATH = "/tmp/scale_test/store.sqlite3"
shutil.rmtree("/tmp/scale_test", ignore_errors=True)

# Realistic volume estimate: on a busy database, the diff key is
# (pid, state, category, query) -- and short queries churn constantly, so
# several rows per second is normal, not pessimistic.
ROWS_PER_SECOND = 5
HOURS = 48
now = time.time()
window_start = now - HOURS * 3600
total_rows = ROWS_PER_SECOND * HOURS * 3600

print(f"generating {total_rows:,} session rows over {HOURS}h "
      f"({ROWS_PER_SECOND}/s, a realistic busy-database rate)...")

store = Store(DB_PATH)
# Bulk insert directly for speed; same shape the collector would write.
conn = store.conn
t0 = time.perf_counter()
batch = []
for i in range(total_rows):
    ts = window_start + (i / ROWS_PER_SECOND)
    batch.append((ts, 1000 + (i % 200), "client backend", "app_role",
                   f"web-{i % 8}", "active", ["cpu", "io", "other", "lock"][i % 4],
                   f"SELECT * FROM orders WHERE id = {i % 1000}"))
    if len(batch) >= 50000:
        conn.executemany(
            "INSERT INTO session_changes (ts, pid, backend_type, usename, "
            "application_name, state, category, query) VALUES (?,?,?,?,?,?,?,?)",
            batch,
        )
        batch = []
if batch:
    conn.executemany(
        "INSERT INTO session_changes (ts, pid, backend_type, usename, "
        "application_name, state, category, query) VALUES (?,?,?,?,?,?,?,?)",
        batch,
    )
conn.commit()

# A handful of blocking edges and heartbeat coverage across the window.
for h in range(HOURS * 60):
    store.write_heartbeat("postgres", detail="ok", ts=window_start + h * 60,
                           instance_role="primary")
store.write_blocking_edges(
    [(512, "web-3", 210, "batch", "analytics_role", "REINDEX TABLE orders")],
    ts=now - 300,
)
gen_seconds = time.perf_counter() - t0
db_mb = os.path.getsize(DB_PATH) / (1024 * 1024)
print(f"generated in {gen_seconds:.1f}s -- database size: {db_mb:.1f} MB")
store.close()

# --- The actual measurement: what happens when someone runs
# `whyslow --last 48h` on this?
store = Store(DB_PATH)

print("\nmeasuring explain() over the full 48h window...")
t0 = time.perf_counter()
result = explain_mod.explain(store, window_start, now)
explain_seconds = time.perf_counter() - t0
print(f"  explain():  {explain_seconds:.2f}s")
print(f"  timeline rows held in memory: {len(result['timeline']):,}")

t0 = time.perf_counter()
out = explain_mod.render(result, window_start, now)
render_seconds = time.perf_counter() - t0
print(f"  render():   {render_seconds:.2f}s")
print(f"  output size: {len(out) / (1024*1024):.1f} MB, {out.count(chr(10)):,} lines")

# For comparison, the realistic incident-sized window.
t0 = time.perf_counter()
narrow = explain_mod.explain(store, now - 900, now)
narrow_seconds = time.perf_counter() - t0
print(f"\n  explain() over a 15m window: {narrow_seconds:.3f}s "
      f"({len(narrow['timeline']):,} rows)")

store.close()

# Regression thresholds. Before SQL-level aggregation this window took
# 8.1s, held 864k rows in memory (733 MB RSS), and produced 38 MB /
# 864,031 lines of output -- an OOM risk on a small host and unreadable
# either way.
print()
assert explain_seconds < 3.0, f"48h explain took {explain_seconds:.1f}s (was 8.1s before aggregation)"
assert len(result["timeline"]) < 50_000, (
    f"{len(result['timeline']):,} timeline rows in memory -- session data "
    f"must stay aggregated in SQL, not loaded row-by-row"
)
assert len(out.splitlines()) < 500, (
    f"{len(out.splitlines()):,} output lines -- nobody can read that"
)
assert narrow_seconds < 0.5, f"15m explain took {narrow_seconds:.3f}s"
print(f"PASS: 48h window -> {explain_seconds:.2f}s, "
      f"{len(result['timeline']):,} rows in memory, {len(out.splitlines())} output lines")
