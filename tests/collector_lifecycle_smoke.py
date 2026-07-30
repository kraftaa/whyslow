import shutil
import subprocess
import sys
import time

from whyslow import explain as explain_mod
from whyslow import status as status_mod
from whyslow.storage import Store


ROOT = "/tmp/whyslow-collector-lifecycle"
DB_PATH = f"{ROOT}/store.sqlite3"
shutil.rmtree(ROOT, ignore_errors=True)
now = time.time()

store = Store(DB_PATH)

# web-4 joins halfway through the window. It should be judged only for the
# period when it was actually a fleet member.
for minute in range(61):
    store.write_heartbeat(
        "puma:web-3",
        detail="ok",
        expected_interval=60,
        ts=now - 3600 + minute * 60,
    )
for minute in range(31, 61):
    store.write_heartbeat(
        "puma:web-4",
        detail="ok",
        expected_interval=60,
        ts=now - 3600 + minute * 60,
    )

report = explain_mod.explain(store, now - 3600, now)
puma = report["coverage"]["roles"]["puma"]
assert puma["ok"], puma
assert puma["collectors"] == ["puma:web-3", "puma:web-4"], puma
assert report["coverage"]["per_collector"]["puma:web-4"]["expected_minutes"] == 30
store.close()

# Exercise the public CLI, not only the storage method.
retired = subprocess.run(
    [
        sys.executable,
        "-m",
        "whyslow.cli",
        "retire",
        "puma:web-4",
        "--at",
        str(now),
        "--db",
        DB_PATH,
    ],
    text=True,
    capture_output=True,
)
assert retired.returncode == 0, retired.stderr
assert "retired collector: puma:web-4" in retired.stdout

store = Store(DB_PATH)
current = status_mod.status(store, now=now + 61)
assert [c["name"] for c in current["collectors"]] == ["puma:web-3"], current
assert [c["name"] for c in current["retired_collectors"]] == ["puma:web-4"]
assert "Retired collectors" in status_mod.render(current)

# Retirement preserves the old report, but web-4 is no longer expected in
# a later window.
historical = explain_mod.explain(store, now - 3600, now)
assert historical["coverage"]["roles"]["puma"]["ok"], historical["coverage"]

future = explain_mod.explain(store, now + 60, now + 600)
future_puma = future["coverage"]["roles"]["puma"]
assert "puma:web-4" not in future_puma["collectors"], future_puma

# A resumed collector automatically becomes active again.
store.write_heartbeat(
    "puma:web-4",
    detail="rejoined",
    expected_interval=60,
    ts=now + 300,
)
reactivated = status_mod.status(store, now=now + 301)
assert [c["name"] for c in reactivated["collectors"]] == [
    "puma:web-3",
    "puma:web-4",
], reactivated
assert not reactivated["retired_collectors"], reactivated

# The retirement gap remains excluded after reactivation; rejoining must
# not retroactively make the collector expected during its absence.
gap = explain_mod.explain(store, now + 60, now + 240)
assert "puma:web-4" not in gap["coverage"]["roles"]["puma"]["collectors"], gap
store.close()

unknown = subprocess.run(
    [
        sys.executable,
        "-m",
        "whyslow.cli",
        "retire",
        "puma:does-not-exist",
        "--db",
        DB_PATH,
    ],
    text=True,
    capture_output=True,
)
assert unknown.returncode != 0
assert "unknown collector" in unknown.stderr

future_retirement = subprocess.run(
    [
        sys.executable,
        "-m",
        "whyslow.cli",
        "retire",
        "puma:web-3",
        "--at",
        str(time.time() + 3600),
        "--db",
        DB_PATH,
    ],
    text=True,
    capture_output=True,
)
assert future_retirement.returncode != 0
assert "cannot be in the future" in future_retirement.stderr

print("PASS: collector join, retirement, historical coverage, and reactivation work")
