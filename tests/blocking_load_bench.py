import time
import threading
import psycopg2

from whyslow.collector_postgres import PostgresCollector, BLOCKING_SQL
from whyslow.storage import Store

DSN = "dbname=postgres user=postgres password=postgres host=127.0.0.1"
N_BLOCKED = 80

barrier = threading.Event()
release = threading.Event()
stop = threading.Event()
conns = []


def holder():
    conn = psycopg2.connect(DSN, application_name="holder")
    conn.autocommit = False
    cur = conn.cursor()
    cur.execute("BEGIN;")
    cur.execute("UPDATE orders SET val = val + 1 WHERE id = 1;")
    conns.append(conn)
    barrier.set()
    release.wait()
    conn.commit()
    conn.close()


def blocked_waiter(i):
    conn = psycopg2.connect(DSN, application_name=f"waiter-{i}")
    conn.autocommit = True
    cur = conn.cursor()
    barrier.wait()
    try:
        cur.execute("UPDATE orders SET val = val + 1 WHERE id = 1;")  # blocks here
    except Exception:
        pass
    conn.close()


holder_thread = threading.Thread(target=holder)
holder_thread.start()
barrier.wait()

waiter_threads = [threading.Thread(target=blocked_waiter, args=(i,)) for i in range(N_BLOCKED)]
for t in waiter_threads:
    t.start()

time.sleep(5.0)  # let all waiters actually queue up and block

store = Store("/tmp/blocking_load_test/store.sqlite3")
collector = PostgresCollector(DSN, store, interval=1.0)
conn = collector._connect()
conn.autocommit = True

with conn.cursor() as cur:
    cur.execute(
        "SELECT count(*) FROM pg_stat_activity WHERE cardinality(pg_blocking_pids(pid)) > 0;"
    )
    actually_blocked = cur.fetchone()[0]
print(f"sessions confirmed blocked at measurement time: {actually_blocked}")

with conn.cursor() as cur:
    t0 = time.perf_counter()
    cur.execute(BLOCKING_SQL)
    edges = cur.fetchall()
    sql_ms = (time.perf_counter() - t0) * 1000

print(f"blocked waiters requested: {N_BLOCKED}")
print(f"blocking edges returned:   {len(edges)}")
print(f"BLOCKING_SQL round-trip:   {sql_ms:.2f} ms")

times = []
for _ in range(10):
    t0 = time.perf_counter()
    collector.poll_once(conn)
    times.append((time.perf_counter() - t0) * 1000)

print(f"\npoll_once() under {len(edges)} real blocking edges, over 10 polls:")
print(f"  min={min(times):.2f}ms  avg={sum(times)/len(times):.2f}ms  max={max(times):.2f}ms")
print(f"  budget available per poll at interval=1.0s: 1000ms")

release.set()
for t in waiter_threads:
    t.join(timeout=5)
holder_thread.join(timeout=5)
conn.close()
