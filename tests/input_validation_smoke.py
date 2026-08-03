import math
import shutil
import subprocess
import sys
from pathlib import Path

from whyslow.cli import parse_duration, parse_time
from whyslow.collector_postgres import PostgresCollector
from whyslow.collector_puma import PumaCollector
from whyslow.storage import Store


ROOT = "/tmp/whyslow-input-validation"
DB = f"{ROOT}/store.sqlite3"
shutil.rmtree(ROOT, ignore_errors=True)


def run(*args):
    return subprocess.run(
        [sys.executable, "-m", "whyslow.cli", *args],
        text=True,
        capture_output=True,
    )


invalid_commands = [
    ("--last", "nanh", "--db", DB),
    ("--last", "infh", "--db", DB),
    ("--last", "0m", "--db", DB),
    ("--last", "32d", "--db", DB),
    (
        "explain", "--from", "2026-01-01T00:00:00Z",
        "--to", "2026-03-01T00:00:00Z", "--db", DB,
    ),
    ("collect-pg", "--dsn", "unused", "--interval", "nan", "--db", DB),
    (
        "collect-puma", "--host-name", "web-1", "--stats-url",
        "ftp://web-1/stats", "--db", DB,
    ),
    (
        "collect-puma", "--host-name", "bad host", "--stats-url",
        "http://127.0.0.1/stats", "--db", DB,
    ),
    (
        "collect-puma", "--host-name", "web-1", "--stats-url",
        "https://user:secret@web-1/stats", "--db", DB,
    ),
    (
        "collect-puma", "--host-name", "web-1", "--stats-url",
        "https://web-1/stats?token=secret", "--db", DB,
    ),
    (
        "collect-cw", "--db-instance-id", "bad--identifier", "--db", DB,
    ),
    (
        "collect-cw", "--db-cluster-id", "bad--cluster", "--db", DB,
    ),
    (
        "collect-cw", "--db-cluster-id", "cluster-1",
        "--db-instance-id", "writer-1", "--db", DB,
    ),
    (
        "collect-cw", "--db-instance-id", "writer-1", "--interval", "3601",
        "--db", DB,
    ),
    ("event", "--source", "bad source", "--kind", "deploy", "--db", DB),
    ("event", "--source", "deploy", "--kind", "x" * 257, "--db", DB),
    ("event", "--source", "deploy", "--kind", "forged\nline", "--db", DB),
    ("event", "--source", "deploy", "--payload", "x" * 4097, "--db", DB),
    ("event", "--source", "deploy", "--payload", "ansi\x1b[31m", "--db", DB),
    ("event", "--source", "deploy", "--at", "nan", "--db", DB),
    ("retire", "bad collector name", "--db", DB),
    ("doctor", "--db-instance-id", "1starts-with-number", "--db", DB),
]

for command in invalid_commands:
    result = run(*command)
    assert result.returncode != 0, command
    assert "Traceback" not in result.stderr, (command, result.stderr)

# Invalid report/event values are resolved before Store construction.
assert not Path(DB).exists(), "invalid input must not create or mutate the evidence store"

assert parse_duration("31d") == 31 * 86400
for value in ("nan", "inf", "-inf", "1e308"):
    try:
        parse_time(value)
    except SystemExit:
        pass
    else:
        raise AssertionError(f"non-finite timestamp accepted: {value}")

# Library callers receive the same protections as CLI users.
store = Store(DB)
for interval in (0, -1, math.nan, math.inf, 3601):
    try:
        PostgresCollector("unused", store, interval=interval)
    except ValueError:
        pass
    else:
        raise AssertionError(f"PostgresCollector accepted interval={interval}")

for url in ("ftp://host/stats", "https://user:pass@host/stats", "https://host/stats?t=x"):
    try:
        PumaCollector("web-1", url, store)
    except ValueError:
        pass
    else:
        raise AssertionError(f"PumaCollector accepted unsafe URL: {url}")

try:
    store.write_event("deploy", "forged\nline", None)
except ValueError:
    pass
else:
    raise AssertionError("Store.write_event bypassed event-text validation")
store.close()

print("PASS: pathological CLI and collector inputs fail before side effects")
