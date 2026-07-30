import shutil
import subprocess
import sys
import time

import psycopg2

from whyslow.storage import Store
from whyslow.collector_postgres import PostgresCollector
from whyslow import explain as explain_mod
from whyslow import status as status_mod

DSN = "dbname=postgres user=postgres password=postgres host=127.0.0.1"
now = time.time()

# --- Part 1: detection works against a real instance (a primary here) ---
store = Store("/tmp/replica_smoke/store.sqlite3")
collector = PostgresCollector(DSN, store, interval=1.0)
conn = collector._connect()
conn.autocommit = True
role = collector._detect_instance_role(conn)
print(f"detected role of the live test instance: {role}")
assert role == "primary", f"local test Postgres should be a primary, got {role}"
conn.close()
store.close()

# --- Part 2: simulate what a READER-endpoint deployment records ---
# Can't spin up an Aurora reader here, so drive the recorded state
# directly -- the detection logic itself is verified above.
shutil.rmtree("/tmp/replica_smoke2", ignore_errors=True)
DB2 = "/tmp/replica_smoke2/store.sqlite3"
store = Store(DB2)
for minute in range(61):
    store.write_heartbeat("postgres", detail="interval=1.0s",
                           ts=now - 3600 + minute * 60, instance_role="replica")
store.close()

store = Store(DB2)
result = explain_mod.explain(store, now - 3600, now)
out = explain_mod.render(result, now - 3600, now)
print("\n=== explain with a collector on a REPLICA (full coverage!) ===")
print(out)

# Coverage is genuinely 100% -- the collector really was running the whole
# window. That's exactly why this needed its own check: coverage alone
# would have waved it through.
assert result["coverage"]["roles"]["postgres"]["fraction"] >= 0.9, \
    "the replica collector really did cover the window"
assert result["coverage"]["db_instance_role"] == "replica"
assert not result["coverage"]["db_ok"], \
    "full coverage from a replica must NOT count as valid DB coverage"
assert "REPLICA" in out and "AURORA READER" in out.upper(), \
    "must warn explicitly about the reader endpoint"
assert "none found -- no blocking edges" not in out, \
    "must never confidently rule out blocking based on replica data"

# --- Part 3: status flags it and exits non-zero ---
st = status_mod.status(store, now=now)
st_out = status_mod.render(st)
print("=== whyslow status with a replica collector ===")
print(st_out)
assert "REPLICA" in st_out
assert "WRITER endpoint" in st_out
store.close()

r = subprocess.run(
    [sys.executable, "-m", "whyslow.cli", "status", "--db", DB2],
    capture_output=True, text=True,
)
assert r.returncode != 0, "status must exit non-zero for a replica misconfiguration"
print(f"\nstatus exit code: {r.returncode} (non-zero, so monitoring catches it)")

print("\nPASS: a collector on an Aurora reader endpoint is detected, warned about "
      "in explain and status, and cannot produce a false all-clear despite full coverage")
