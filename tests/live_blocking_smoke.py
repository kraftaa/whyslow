import threading
import time
import psycopg2

from whyslow.storage import Store
from whyslow.collector_postgres import PostgresCollector

DSN = "dbname=postgres user=postgres password=postgres host=127.0.0.1"
DB_PATH = "/tmp/whyslow_smoke/store.sqlite3"


def session_a_holds_lock(start_barrier, release_after):
    conn = psycopg2.connect(DSN, application_name="web-3")
    conn.autocommit = False
    cur = conn.cursor()
    cur.execute("BEGIN;")
    cur.execute("UPDATE orders SET val = val + 1 WHERE id = 1;")
    print("[session A] lock acquired, holding...")
    start_barrier.set()
    time.sleep(release_after)
    conn.commit()
    print("[session A] committed, released lock")
    conn.close()


def session_b_gets_blocked(start_barrier):
    start_barrier.wait()
    time.sleep(0.5)  # let A settle
    conn = psycopg2.connect(DSN, application_name="analytics_role_session")
    conn.autocommit = True
    cur = conn.cursor()
    print("[session B] attempting update, will block...")
    t0 = time.time()
    cur.execute("UPDATE orders SET val = val + 1 WHERE id = 1;")
    print(f"[session B] unblocked after {time.time() - t0:.1f}s")
    conn.close()


def run_collector_for(seconds):
    store = Store(DB_PATH)
    collector = PostgresCollector(DSN, store, interval=0.5)
    conn = collector._connect()
    conn.autocommit = True
    end = time.time() + seconds
    while time.time() < end:
        n_s, n_e = collector.poll_once(conn)
        if n_s or n_e:
            print(f"[collector] +{n_s} sessions, +{n_e} blocking edges")
        time.sleep(0.5)
    conn.close()
    store.close()


if __name__ == "__main__":
    import shutil
    shutil.rmtree("/tmp/whyslow_smoke", ignore_errors=True)

    barrier = threading.Event()
    t_start = time.time()
    print(f"smoke test start_ts={t_start}")

    collector_thread = threading.Thread(target=run_collector_for, args=(8,))
    a_thread = threading.Thread(target=session_a_holds_lock, args=(barrier, 3))
    b_thread = threading.Thread(target=session_b_gets_blocked, args=(barrier,))

    collector_thread.start()
    a_thread.start()
    b_thread.start()

    a_thread.join()
    b_thread.join()
    collector_thread.join()

    t_end = time.time()
    print(f"smoke test end_ts={t_end}")

    # Assert, don't just print -- this needs to fail loudly in CI.
    from whyslow.storage import Store
    from whyslow import explain as explain_mod

    store = Store(DB_PATH)
    edges = store.blocking_edges_in(t_start, t_end)
    assert edges, "expected at least one real blocking edge captured by the live collector"

    result = explain_mod.explain(store, t_start, t_end)
    assert result["contributors"], "expected explain() to find a contributor from the real blocking edge"
    print(explain_mod.render(result, t_start, t_end))
    print("\nPASS: live Postgres blocking chain captured and explained")
