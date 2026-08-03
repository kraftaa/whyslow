import shutil
import threading
import time

import psycopg2

from whyslow.storage import Store
from whyslow.collector_postgres import PostgresCollector
from whyslow import explain as explain_mod

DSN = "dbname=postgres user=postgres password=postgres host=127.0.0.1"
DB_PATH = "/tmp/duration_smoke/store.sqlite3"
shutil.rmtree("/tmp/duration_smoke", ignore_errors=True)

HOLD_SECONDS = 3.0

acquired = threading.Event()


def holder():
    conn = psycopg2.connect(DSN, application_name="batch-host")
    conn.autocommit = False
    cur = conn.cursor()
    cur.execute("BEGIN;")
    cur.execute("UPDATE orders SET val = val + 1 WHERE id = 1;")
    acquired.set()
    time.sleep(HOLD_SECONDS)
    conn.commit()   # <- lock released here; the edge should be marked ended
    conn.close()


def waiter():
    acquired.wait()
    time.sleep(0.3)
    conn = psycopg2.connect(DSN, application_name="web-3")
    conn.autocommit = True
    cur = conn.cursor()
    try:
        cur.execute("UPDATE orders SET val = val + 1 WHERE id = 1;")
    except Exception:
        pass
    conn.close()


def collect_for(seconds):
    store = Store(DB_PATH)
    collector = PostgresCollector(DSN, store, interval=0.3)
    conn = collector._connect()
    conn.autocommit = True
    collector.poll_once(conn, prime_only=True)
    end = time.time() + seconds
    while time.time() < end:
        collector.poll_once(conn)
        time.sleep(0.3)
    conn.close()
    store.close()


t_start = time.time()
threads = [
    threading.Thread(target=collect_for, args=(8,)),
    threading.Thread(target=holder),
    threading.Thread(target=waiter),
]
for t in threads:
    t.start()
for t in threads:
    t.join()
t_end = time.time()

store = Store(DB_PATH)

# Raw check: was the edge actually marked as ended?
rows = store.blocking_edges_in(t_start, t_end)
print(f"blocking edges recorded: {len(rows)}")
for r in rows:
    ts, blocked_pid, blocked_app, blocking_pid, blocking_app, blocking_user, blocking_query, ended_ts = r
    dur = (ended_ts - ts) if ended_ts else None
    print(f"  blocked={blocked_pid} blocking={blocking_pid} ended_ts={'set' if ended_ts else 'NULL'} "
          f"duration={f'{dur:.1f}s' if dur else 'n/a'}")

assert rows, "expected at least one blocking edge"
resolved = [r for r in rows if r[7] is not None]
assert resolved, "collector must record when an edge RESOLVES, not only when it appears"

duration = resolved[0][7] - resolved[0][0]
assert 0.5 < duration < HOLD_SECONDS + 3, (
    f"recorded duration {duration:.1f}s is implausible for a ~{HOLD_SECONDS}s hold"
)

print()
result = explain_mod.explain(store, t_start, t_end)
print(explain_mod.render(result, t_start, t_end))

blocking = [c for c in result["contributors"] if c["category"] == "blocking"]
assert blocking, "expected a blocking contributor"
b = blocking[0]
assert b.get("pid"), "contributor must expose the pid -- the runbook needs it to act"
assert b.get("held_seconds") is not None, "resolved edge should report a held duration"

store.close()
print(f"\nPASS: real blocking chain captured with duration ({b['held_seconds']:.1f}s) "
      f"and actionable pid ({b['pid']})")


# --- Regression: a collector starting DURING an active block must still
# record it. The priming poll used to swallow in-progress blocking edges,
# meaning a collector restarted mid-incident was silently blind in exactly
# the situation it exists for. Found while debugging this very test.
print("\n--- regression: collector starts mid-incident ---")
shutil.rmtree("/tmp/duration_smoke_restart", ignore_errors=True)
RESTART_DB = "/tmp/duration_smoke_restart/store.sqlite3"

acquired2 = threading.Event()


def holder2():
    conn = psycopg2.connect(DSN, application_name="batch-host")
    conn.autocommit = False
    cur = conn.cursor()
    cur.execute("BEGIN;")
    cur.execute("UPDATE orders SET val = val + 1 WHERE id = 1;")
    acquired2.set()
    time.sleep(4)
    conn.commit()
    conn.close()


def waiter2():
    acquired2.wait()
    time.sleep(0.3)
    conn = psycopg2.connect(DSN, application_name="web-3")
    conn.autocommit = True
    cur = conn.cursor()
    try:
        cur.execute("UPDATE orders SET val = val + 1 WHERE id = 1;")
    except Exception:
        pass
    conn.close()


th2 = threading.Thread(target=holder2)
tw2 = threading.Thread(target=waiter2)
th2.start()
tw2.start()
time.sleep(1.5)  # block is already in progress before the collector exists

store2 = Store(RESTART_DB)
col2 = PostgresCollector(DSN, store2, interval=0.3)
conn2 = col2._connect()
conn2.autocommit = True
col2.poll_once(conn2, prime_only=True)   # priming must NOT swallow this
for _ in range(5):
    col2.poll_once(conn2)
    time.sleep(0.3)

mid_rows = store2.blocking_edges_in(0, time.time() + 1)
print(f"blocking edges recorded when collector started mid-block: {len(mid_rows)}")
assert mid_rows, (
    "a collector starting during an active block must still record it -- "
    "priming may suppress session noise, never blocking edges"
)

th2.join()
tw2.join()
conn2.close()
store2.close()
print("PASS: in-progress blocking chain is captured even when the collector starts mid-incident")


# --- Regression: a long-running block that began before the requested
# window must still appear, with its state reconstructed as of that
# window rather than using knowledge of its later resolution.
print("\n--- regression: block overlaps a later historical window ---")
shutil.rmtree("/tmp/duration_smoke_overlap", ignore_errors=True)
store3 = Store("/tmp/duration_smoke_overlap/store.sqlite3")
block_start = time.time() - 1200
block_end = block_start + 900
store3.write_blocking_edges(
    [(700, "web-7", 600, "batch", "analytics_role", "REINDEX TABLE orders")],
    ts=block_start,
)
store3.mark_blocking_edges_ended({(700, 600)}, ts=block_end)
store3.write_puma_stat(
    host="web-7",
    backlog=9,
    pool_capacity=0,
    max_threads=16,
    running=16,
    rss_mb=None,
    ts=block_start + 450,
)

window_start = block_start + 300
window_end = block_start + 600
result3 = explain_mod.explain(store3, window_start, window_end)
out3 = explain_mod.render(result3, window_start, window_end)
print(out3)

blocking3 = [c for c in result3["contributors"] if c["category"] == "blocking"]
assert blocking3, "a block spanning the entire window must not disappear"
b3 = blocking3[0]
assert b3["still_active"], "the block was active at this historical window's end"
assert not b3["currently_unresolved"], "the store knows it resolved later"
assert 599 < b3["held_seconds"] < 601
assert "already active at window start" in out3
assert "Puma backlog spike on web-7" in out3, (
    "corroboration during a continuing block must not require proximity to its old start"
)
assert "pg_cancel_backend(600)" not in out3, (
    "a historical block known to have resolved later must not get a live kill command"
)

result4 = explain_mod.explain(store3, block_start + 300, block_start + 950)
b4 = [c for c in result4["contributors"] if c["category"] == "blocking"][0]
assert not b4["still_active"], "the later window includes the known resolution"
assert 899 < b4["held_seconds"] < 901
store3.close()
print("PASS: overlapping locks remain visible with window-correct state and remediation")


# --- Regression: if collection stops while an edge is active, NULL
# ended_ts means "resolution unknown", not "still blocking forever".
print("\n--- regression: unresolved edge followed by collector coverage gap ---")
shutil.rmtree("/tmp/duration_smoke_abandoned", ignore_errors=True)
store4 = Store("/tmp/duration_smoke_abandoned/store.sqlite3")
base = (int(time.time() // 60) - 20) * 60
store4.write_blocking_edges(
    [(800, "web-8", 650, "batch", "analytics_role", "REINDEX TABLE orders")],
    ts=base + 5,
)
store4.write_heartbeat("postgres", detail="ok", ts=base + 10)
store4.write_heartbeat("postgres", detail="ok", ts=base + 70)

abandoned = explain_mod.explain(store4, base, base + 600)
abandoned_out = explain_mod.render(abandoned, base, base + 600)
print(abandoned_out)
abandoned_blockers = [
    c for c in abandoned["contributors"] if c["category"] == "blocking"
]
assert abandoned_blockers, "the observed portion of an abandoned edge should remain evidence"
abandoned_blocker = abandoned_blockers[0]
assert abandoned_blocker["resolution_unknown"]
assert not abandoned_blocker["still_active"]
assert not abandoned_blocker["currently_unresolved"]
assert 114 < abandoned_blocker["held_seconds"] < 116
assert "resolution unknown after collector coverage stopped" in abandoned_out
assert "before coverage stopped" in abandoned_out
assert "pg_cancel_backend(650)" not in abandoned_out

# A later window must not resurrect the stale edge as a live blocker.
later = explain_mod.explain(store4, base + 300, base + 600)
assert not [c for c in later["contributors"] if c["category"] == "blocking"]

# Continuous rediscovery keeps the original start, while the same PID pair
# observed after a real gap becomes a separate episode.
store4.write_blocking_edges(
    [(800, "web-8", 650, "batch", "analytics_role", "REINDEX TABLE orders")],
    ts=base + 75,
)
assert len(store4.blocking_edges_in(0, base + 100)) == 1
store4.write_blocking_edges(
    [(800, "web-8", 650, "batch", "analytics_role", "REINDEX TABLE orders")],
    ts=base + 305,
)

# A long reconnect gap clears the collector's in-memory edge identity so
# its priming poll can record the still-present edge as a new episode.
collector4 = PostgresCollector("unused", store4)
collector4._last_blocking_keys = {(800, 650)}
collector4._prepare_for_connection(now=base + 305)
assert not collector4._last_blocking_keys

store4.mark_blocking_edges_ended({(800, 650)}, ts=base + 360)
episodes = store4.blocking_edges_in(0, base + 600)
assert len(episodes) == 2, episodes
assert episodes[0][7] is None, "the abandoned episode remains explicitly unknown"
assert episodes[1][7] == base + 360, "the later episode resolves independently"
store4.close()
print("PASS: abandoned edges are bounded by coverage and cannot look live forever")
