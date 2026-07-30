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
