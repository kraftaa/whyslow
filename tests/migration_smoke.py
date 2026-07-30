import shutil
import sqlite3
import time

from whyslow.storage import Store

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

print("\nPASS: an existing pre-ended_ts database migrates in place, keeps its data, "
      "stays usable, and re-opening is idempotent")
