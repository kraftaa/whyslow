import time
import threading
import psycopg2

DSN = "dbname=postgres user=postgres password=postgres host=127.0.0.1"
N_BLOCKED = 10  # small N so the raw output is human-readable

barrier = threading.Event()
release = threading.Event()
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
        cur.execute("UPDATE orders SET val = val + 1 WHERE id = 1;")
    except Exception:
        pass
    conn.close()


holder_thread = threading.Thread(target=holder)
holder_thread.start()
barrier.wait()

waiter_threads = [threading.Thread(target=blocked_waiter, args=(i,)) for i in range(N_BLOCKED)]
for t in waiter_threads:
    t.start()

time.sleep(3.0)

diag_conn = psycopg2.connect(DSN, application_name="diagnostic")
diag_conn.autocommit = True
cur = diag_conn.cursor()

cur.execute("""
    SELECT pid, application_name, pg_blocking_pids(pid)
    FROM pg_stat_activity
    WHERE application_name LIKE 'waiter-%' OR application_name = 'holder'
    ORDER BY application_name;
""")
print(f"{'app':<12} {'pid':<8} blocking_pids")
for pid, app, blockers in cur.fetchall():
    print(f"{app:<12} {pid:<8} {blockers}")

release.set()
for t in waiter_threads:
    t.join(timeout=5)
holder_thread.join(timeout=5)
diag_conn.close()
