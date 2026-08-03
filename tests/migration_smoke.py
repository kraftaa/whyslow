import shutil
import sqlite3
import subprocess
import sys
import time

from whyslow.storage import SCHEMA_VERSION, Store

DB_PATH = "/tmp/migration_smoke/store.sqlite3"
shutil.rmtree("/tmp/migration_smoke", ignore_errors=True)

import os
os.makedirs("/tmp/migration_smoke", exist_ok=True)

# Simulate a database created by an OLDER version, before ended_ts existed.
old_conn = sqlite3.connect(DB_PATH)
old_conn.executescript("""
CREATE TABLE blocking_edges (
    ts REAL NOT NULL,
    blocked_pid INTEGER NOT NULL,
    blocked_app TEXT,
    blocking_pid INTEGER NOT NULL,
    blocking_app TEXT,
    blocking_usename TEXT,
    blocking_query TEXT
);
CREATE TABLE collector_heartbeats (
    collector TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    detail TEXT
);
""")
old_conn.execute(
    "INSERT INTO blocking_edges VALUES (?,?,?,?,?,?,?)",
    (time.time(), 512, "web-3", 210, "batch", "analytics_role", "REINDEX TABLE orders"),
)
old_conn.execute(
    "INSERT INTO collector_heartbeats VALUES (?,?,?)",
    ("postgres", time.time(), "interval=1.0s"),
)
old_conn.commit()

cols_before = {r[1] for r in old_conn.execute("PRAGMA table_info(blocking_edges)").fetchall()}
print(f"columns before upgrade: {sorted(cols_before)}")
assert "ended_ts" not in cols_before
heartbeat_cols_before = {
    r[1] for r in old_conn.execute("PRAGMA table_info(collector_heartbeats)").fetchall()
}
assert "expected_interval" not in heartbeat_cols_before
old_conn.close()

# Opening with the current Store must migrate in place, not crash and not
# lose the existing row. CREATE TABLE IF NOT EXISTS silently does nothing
# on an existing table, so without an explicit migration this would break.
store = Store(DB_PATH)
cols_after = {r[1] for r in store.conn.execute("PRAGMA table_info(blocking_edges)").fetchall()}
print(f"columns after upgrade:  {sorted(cols_after)}")
assert "ended_ts" in cols_after, "migration must add the ended_ts column"
heartbeat_cols_after = {
    r[1] for r in store.conn.execute("PRAGMA table_info(collector_heartbeats)").fetchall()
}
assert "expected_interval" in heartbeat_cols_after
assert "instance_role" in heartbeat_cols_after
assert store.schema_version() == SCHEMA_VERSION
stored_version = store.conn.execute(
    "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
).fetchone()[0]
assert stored_version == str(SCHEMA_VERSION)

rows = store.blocking_edges_in(0, time.time() + 1)
print(f"pre-existing rows preserved: {len(rows)}")
assert len(rows) == 1, "migration must not lose existing data"
assert rows[0][7] is None, "pre-existing rows should have NULL ended_ts"

# And the migrated database must still be fully usable.
store.write_blocking_edges([(600, "web-1", 700, "batch", "etl_role", "COPY x")], ts=time.time())
store.mark_blocking_edges_ended([(600, 700)], ts=time.time() + 5)
rows = store.blocking_edges_in(0, time.time() + 10)
resolved = [r for r in rows if r[7] is not None]
assert resolved, "writes and end-marking must work after migration"

# Idempotent: opening again must not fail or duplicate the column.
store.close()
store2 = Store(DB_PATH)
cols_again = {r[1] for r in store2.conn.execute("PRAGMA table_info(blocking_edges)").fetchall()}
assert cols_again == cols_after, "migration must be idempotent"
heartbeat_cols_again = {
    r[1] for r in store2.conn.execute("PRAGMA table_info(collector_heartbeats)").fetchall()
}
assert heartbeat_cols_again == heartbeat_cols_after
store2.close()

# A store written by a newer whyslow must be refused before WAL/schema/
# permission mutation. Silently opening it could corrupt data whose layout
# this version does not understand.
newer_path = "/tmp/migration_smoke/newer.sqlite3"
newer = sqlite3.connect(newer_path)
newer.execute(
    "CREATE TABLE schema_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
)
newer.execute(
    "INSERT INTO schema_metadata VALUES ('schema_version', ?)",
    (str(SCHEMA_VERSION + 1),),
)
newer.commit()
assert newer.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
newer.close()
before = open(newer_path, "rb").read()

refused = subprocess.run(
    [sys.executable, "-m", "whyslow.cli", "status", "--db", newer_path],
    text=True,
    capture_output=True,
)
assert refused.returncode != 0
assert "newer than supported" in refused.stderr
assert "Traceback" not in refused.stderr
after = open(newer_path, "rb").read()
assert after == before, "refusing a newer store must not mutate its bytes"
newer = sqlite3.connect(newer_path)
assert newer.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
newer.close()

# A malformed store that claims v1 but lacks the v1 tables must roll back
# every pending step. In particular, it must not advance metadata to v2
# before the missing table makes that migration fail.
broken_path = "/tmp/migration_smoke/broken-v1.sqlite3"
broken = sqlite3.connect(broken_path)
broken.execute(
    "CREATE TABLE schema_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
)
broken.execute("INSERT INTO schema_metadata VALUES ('schema_version', '1')")
broken.commit()
broken.close()

broken_result = subprocess.run(
    [sys.executable, "-m", "whyslow.cli", "status", "--db", broken_path],
    text=True,
    capture_output=True,
)
assert broken_result.returncode != 0
assert "schema was left unchanged" in broken_result.stderr
assert "Traceback" not in broken_result.stderr
broken = sqlite3.connect(broken_path)
assert broken.execute(
    "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
).fetchone()[0] == "1"
assert broken.execute(
    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='collector_memberships'"
).fetchone() is None
broken.close()

print("\nPASS: an existing pre-ended_ts database migrates in place, keeps its data, "
      "stays usable, migrations roll back, and newer stores are refused")
