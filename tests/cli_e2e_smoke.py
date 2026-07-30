import shutil
import subprocess
import sys
import threading
import time

import psycopg2

from whyslow.storage import Store
from whyslow.collector_postgres import PostgresCollector

DSN = "dbname=postgres user=postgres password=postgres host=127.0.0.1"
DB_PATH = "/tmp/cli_e2e/store.sqlite3"
shutil.rmtree("/tmp/cli_e2e", ignore_errors=True)


def run_collector_briefly(seconds):
    """Run the REAL collector loop (including heartbeat writes) briefly."""
    store = Store(DB_PATH)
    collector = PostgresCollector(DSN, store, interval=0.5)
    conn = collector._connect()
    conn.autocommit = True
    collector.poll_once(conn, prime_only=True)
    end = time.time() + seconds
    while time.time() < end:
        collector.poll_once(conn)
        store.write_heartbeat("postgres", detail="interval=0.5s")
        time.sleep(0.5)
    conn.close()
    store.close()


# Generate a real blocking chain so there's something to explain.
holder_done = threading.Event()


def holder():
    conn = psycopg2.connect(DSN, application_name="web-1")
    conn.autocommit = False
    cur = conn.cursor()
    cur.execute("BEGIN;")
    cur.execute("UPDATE orders SET val = val + 1 WHERE id = 1;")
    time.sleep(2.5)
    conn.commit()
    conn.close()
    holder_done.set()


def waiter():
    time.sleep(0.5)
    conn = psycopg2.connect(DSN, application_name="analytics-batch")
    conn.autocommit = True
    cur = conn.cursor()
    try:
        cur.execute("UPDATE orders SET val = val + 1 WHERE id = 1;")
    except Exception:
        pass
    conn.close()


t_collector = threading.Thread(target=run_collector_briefly, args=(4,))
t_holder = threading.Thread(target=holder)
t_waiter = threading.Thread(target=waiter)
t_collector.start(); t_holder.start(); t_waiter.start()
t_holder.join(); t_waiter.join(); t_collector.join()


def run_cli(*args):
    result = subprocess.run(
        [sys.executable, "-m", "whyslow.cli", *args, "--db", DB_PATH],
        capture_output=True, text=True,
    )
    return result


print("=== whyslow status ===")
r = run_cli("status")
print(r.stdout)
assert "postgres" in r.stdout, "status should report the postgres collector"
assert "alive" in r.stdout, "collector heartbeat was just written, should be alive"
assert r.returncode == 0, f"status should exit 0 when collectors are healthy (got {r.returncode})"

print("=== whyslow --last 5m ===")
r = run_cli("explain", "--last", "5m")
print(r.stdout)
assert r.returncode == 0, f"explain --last failed: {r.stderr}"
assert "Incident Summary" in r.stdout
assert "blocked_by" in r.stdout, "should have captured the real blocking chain"

print("=== whyslow diff --last 2m --baseline-last 2m ===")
r = run_cli("diff", "--last", "2m", "--baseline-last", "2m")
print(r.stdout)
assert r.returncode == 0, f"diff --last failed: {r.stderr}"
assert "Blocking edges" in r.stdout

print("=== error handling: neither --last nor --from/--to ===")
r = run_cli("explain")
assert r.returncode != 0, "should fail when no window is specified"
assert "--last" in (r.stdout + r.stderr), "error should mention the --last option"
print((r.stdout + r.stderr).strip())

print("\nPASS: status, --last relative windows, and diff --baseline-last all work end-to-end via the real CLI")
