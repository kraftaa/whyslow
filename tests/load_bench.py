import time
import threading
import psycopg2

from whyslow.collector_postgres import PostgresCollector, SESSIONS_SQL, BLOCKING_SQL
from whyslow.storage import Store

DSN = "dbname=postgres user=postgres password=postgres host=127.0.0.1"
N_SESSIONS = 90  # near max_connections=100, leaving headroom for the collector's own connection

barrier = threading.Event()
stop = threading.Event()
conns = []


def hold_open_session(i):
    conn = psycopg2.connect(DSN, application_name=f"load-{i}")
    conn.autocommit = False
    cur = conn.cursor()
    cur.execute("BEGIN;")
    cur.execute("SELECT 1;")  # enters a transaction, will show as non-idle
    conns.append(conn)
    barrier.wait()
    # Stay in a live, non-idle transaction for the whole measurement window
    # by re-issuing tiny queries in a loop instead of a single early sleep.
    while not stop.is_set():
        cur.execute("SELECT pg_sleep(0.05);")


threads = [threading.Thread(target=hold_open_session, args=(i,)) for i in range(N_SESSIONS)]
for t in threads:
    t.start()

time.sleep(1.5)  # let connections establish
barrier.set()
time.sleep(0.5)

store = Store("/tmp/load_test/store.sqlite3")
collector = PostgresCollector(DSN, store, interval=1.0)
conn = collector._connect()
conn.autocommit = True

# Isolate raw SQL round-trip time vs Python-side diff/dict-building time.
with conn.cursor() as cur:
    t0 = time.perf_counter()
    cur.execute(SESSIONS_SQL)
    sessions = cur.fetchall()
    t1 = time.perf_counter()
    cur.execute(BLOCKING_SQL)
    edges = cur.fetchall()
    t2 = time.perf_counter()

print(f"rows fetched: {len(sessions)} sessions, {len(edges)} blocking edges")
print(f"SQL round-trip (sessions):  {(t1 - t0) * 1000:.2f} ms")
print(f"SQL round-trip (blocking):  {(t2 - t1) * 1000:.2f} ms")

# Now time poll_once() end to end, repeated, to isolate the Python-side cost
# (diffing/set-building) once the SQL cost is already known.
times = []
for _ in range(20):
    t0 = time.perf_counter()
    collector.poll_once(conn)
    times.append((time.perf_counter() - t0) * 1000)

print(f"\npoll_once() full cost over 20 polls (SQL + Python diff logic):")
print(f"  min={min(times):.2f}ms  avg={sum(times)/len(times):.2f}ms  max={max(times):.2f}ms")
print(f"  budget available per poll at interval=1.0s: 1000ms")

stop.set()
for c in conns:
    try:
        c.close()
    except Exception:
        pass
conn.close()
