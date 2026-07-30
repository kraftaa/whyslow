import shutil
import subprocess
import sys
import time

from whyslow.storage import Store, RETENTION_SECONDS

shutil.rmtree("/tmp/prune_smoke", ignore_errors=True)
store = Store("/tmp/prune_smoke/store.sqlite3")

now = time.time()
old_ts = now - max(RETENTION_SECONDS.values()) - 3600  # past every table's retention window
recent_ts = now - 60  # 1 minute ago, well within retention

store.write_sessions([(1, "client backend", "role", "app", "active", "cpu", "SELECT 1")], ts=old_ts)
store.write_sessions([(2, "client backend", "role", "app", "active", "cpu", "SELECT 1")], ts=recent_ts)

store.write_blocking_edges([(1, "app", 2, "app", "role", "SELECT 1")], ts=old_ts)
store.write_puma_stat(host="h", backlog=1, pool_capacity=1, max_threads=1, running=1, rss_mb=1.0, ts=old_ts)

before = len(store.sessions_in(0, now + 1))
assert before == 2, f"expected 2 rows before prune, got {before}"

deleted = store.prune(now=now)
print(f"deleted: {deleted}")

after = store.sessions_in(0, now + 1)
assert len(after) == 1, f"expected 1 row after prune (old one removed), got {len(after)}"
assert after[0][1] == 2, "expected the surviving row to be the recent one (pid 2), not the old one"

assert deleted["session_changes"] == 1
assert deleted["blocking_edges"] == 1
assert deleted["puma_stats"] == 1

# The CLI makes retention independent of Postgres collector health.
store.write_sessions(
    [(3, "client backend", "role", "app", "active", "cpu", "SELECT 3")],
    ts=old_ts,
)
store.close()

result = subprocess.run(
    [sys.executable, "-m", "whyslow.cli", "prune", "--db", "/tmp/prune_smoke/store.sqlite3"],
    capture_output=True,
    text=True,
)
assert result.returncode == 0, result.stderr
assert "session_changes=1" in result.stdout

store = Store("/tmp/prune_smoke/store.sqlite3")
assert not store.sessions_in(0, old_ts + 1)
store.close()

print("PASS: Store and CLI pruning remove expired rows and keep recent ones")
