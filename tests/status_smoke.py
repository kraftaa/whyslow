import shutil
import time

from whyslow.storage import Store
from whyslow import status as status_mod

DB_PATH = "/tmp/status_smoke/store.sqlite3"
shutil.rmtree("/tmp/status_smoke", ignore_errors=True)

now = time.time()

# --- Case 1: no collector has ever run ---
store = Store(DB_PATH)
result = status_mod.status(store, now=now)
out = status_mod.render(result)
print("=== Case 1: nothing ever ran ===")
print(out)
assert not result["collectors"]
assert "NO COLLECTORS HAVE EVER RUN" in out
store.close()

# --- Case 2: collector alive, database quiet (ZERO data rows) ---
# This is the case that was previously indistinguishable from a dead
# collector: diffs mean a quiet database writes nothing at all.
store = Store(DB_PATH)
store.write_heartbeat("postgres", detail="interval=1.0s", ts=now - 1)
result = status_mod.status(store, now=now)
out = status_mod.render(result)
print("\n=== Case 2: collector alive, database quiet (no data rows at all) ===")
print(out)
pg = [c for c in result["collectors"] if c["name"] == "postgres"][0]
assert not pg["stale"], "a 1-second-old heartbeat must not be considered stale"
assert "alive" in out
assert "NORMAL" in out, "should explain that empty tables + live collector is expected"
store.close()

# --- Case 3: custom interval must drive staleness ---
store = Store(DB_PATH)
store.write_heartbeat(
    "postgres",
    detail="interval=30s",
    expected_interval=30,
    ts=now - 20,
)
result = status_mod.status(store, now=now)
pg = [c for c in result["collectors"] if c["name"] == "postgres"][0]
assert not pg["stale"], "a collector inside 10x its configured interval is alive"
assert pg["expected_interval"] == 30
store.close()

# --- Case 4: collector died three weeks ago ---
store = Store(DB_PATH)
store.write_heartbeat(
    "postgres",
    detail="interval=1.0s",
    expected_interval=1,
    ts=now - 21 * 86400,
)
result = status_mod.status(store, now=now)
out = status_mod.render(result)
print("\n=== Case 4: collector died three weeks ago ===")
print(out)
pg = [c for c in result["collectors"] if c["name"] == "postgres"][0]
assert pg["stale"], "a three-week-old heartbeat must be flagged stale"
assert "STALE" in out and "WARNING" in out
assert "NORMAL" not in out, (
    "must not claim empty tables are 'normal' when the collector is stale -- "
    "in that case they're a symptom, not expected behaviour"
)
assert "nothing is being recorded" in out.lower()
store.close()

print("\nPASS: status distinguishes 'alive but quiet' from 'dead' -- "
      "previously impossible, since both produce zero data rows")
