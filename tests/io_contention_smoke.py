import shutil
import time

from whyslow.storage import Store
from whyslow import explain as explain_mod

DB_PATH = "/tmp/io_smoke/store.sqlite3"
shutil.rmtree("/tmp/io_smoke", ignore_errors=True)

store = Store(DB_PATH)
t0 = time.time()

# Deliberately NO 'cpu'-category sessions at all -- a pure disk-bound
# incident, e.g. a bulk COPY or backup hammering IO. Before the fix,
# check_resource_signals() never looked at the 'io' bucket, so this
# scenario produced zero signals no matter how severe.
store.write_sessions(
    [
        (301, "client backend", "etl_role", "batch-host", "active", "io", "COPY orders TO STDOUT"),
        (302, "client backend", "etl_role", "batch-host", "active", "io", "COPY orders TO STDOUT"),
        (303, "client backend", "etl_role", "batch-host", "active", "io", "COPY orders TO STDOUT"),
    ],
    ts=t0 + 1,
)
store.write_puma_stat(host="web-2", backlog=8, pool_capacity=0, max_threads=16, running=16, rss_mb=480.0, ts=t0 + 2)

store.close()
store = Store(DB_PATH)

result = explain_mod.explain(store, t0 - 5, t0 + 10)
print(explain_mod.render(result, t0 - 5, t0 + 10))

resource_contributors = [c for c in result["contributors"] if c["category"] == "resource_contention"]
assert resource_contributors, "expected a resource-contention contributor from a pure IO-bound scenario"
c = resource_contributors[0]
assert any("IO-bound" in s for s in c["signals"]), f"expected an IO-bound signal, got: {c['signals']}"
assert not any("CPU-bound" in s for s in c["signals"]), "should not claim CPU-bound when there were zero cpu sessions"

print("\nPASS: pure IO-bound contention (zero cpu sessions) is correctly detected")
