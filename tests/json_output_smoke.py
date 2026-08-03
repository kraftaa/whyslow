import json
import shutil
import subprocess
import sys
import time

from whyslow.json_output import OUTPUT_SCHEMA_VERSION
from whyslow.storage import Store


ROOT = "/tmp/whyslow-json-output"
DB_PATH = f"{ROOT}/store.sqlite3"
shutil.rmtree(ROOT, ignore_errors=True)

t0 = int(time.time()) - 300
store = Store(DB_PATH)
store.write_sessions([
    (101, "client backend", "z_role", "web-2", "active", "cpu", "SELECT 1"),
    (102, "client backend", "a_role", "web-1", "active", "other", "SELECT 2"),
], ts=t0 + 10)
store.write_cloudwatch_metric(
    "CPUUtilization", 91.0, ts=t0 + 20, source_instance="writer-a"
)
store.write_event("deploy", "v1 released", "sha=abc", ts=t0 + 30)
store.write_heartbeat(
    "postgres", detail="interval=60s", expected_interval=60, ts=time.time()
)
store.close()


def run(*args):
    return subprocess.run(
        [sys.executable, "-m", "whyslow.cli", *args, "--db", DB_PATH],
        text=True,
        capture_output=True,
    )


def payload(result):
    assert result.stdout.lstrip().startswith("{"), result.stdout
    assert "Incident Summary" not in result.stdout
    return json.loads(result.stdout)


start = str(t0)
end = str(t0 + 60)

explain = run("explain", "--from", start, "--to", end, "--json")
assert explain.returncode == 0, explain.stderr
explain_json = payload(explain)
assert explain_json["schema_version"] == OUTPUT_SCHEMA_VERSION
assert explain_json["type"] == "explain"
assert explain_json["window"] == {"start_ts": float(start), "end_ts": float(end)}
cloudwatch = next(
    row for row in explain_json["timeline"]
    if row["evidence"].get("kind") == "cloudwatch"
)
assert cloudwatch["evidence"]["source_instance"] == "writer-a"
assert set(cloudwatch) == {"ts", "message", "evidence"}

# Explicit windows make explain output byte-for-byte deterministic, which is
# useful for snapshots, incident attachments, and content hashing.
explain_again = run("explain", "--from", start, "--to", end, "--json")
assert explain_again.stdout == explain.stdout

diff = run(
    "diff",
    "--baseline-from", str(t0 - 60),
    "--baseline-to", start,
    "--from", start,
    "--to", end,
    "--json",
)
assert diff.returncode == 0, diff.stderr
diff_json = payload(diff)
assert diff_json["schema_version"] == OUTPUT_SCHEMA_VERSION
assert diff_json["type"] == "diff"
assert diff_json["incident"]["roles"] == ["a_role", "z_role"]
assert diff_json["incident"]["apps"] == ["web-1", "web-2"]
assert set(diff_json["windows"]) == {"baseline", "incident"}

status = run("status", "--json")
assert status.returncode == 0, status.stderr
status_json = payload(status)
assert status_json["schema_version"] == OUTPUT_SCHEMA_VERSION
assert status_json["type"] == "status"
assert status_json["ok"] is True
assert status_json["collectors"][0]["name"] == "postgres"

# JSON also works through the default-action route (`whyslow --last ...`).
default_explain = run("--from", start, "--to", end, "--json")
assert default_explain.returncode == 0, default_explain.stderr
assert payload(default_explain)["type"] == "explain"

# Monitoring keeps its useful exit code without sacrificing the JSON body.
store = Store(DB_PATH)
store.write_heartbeat(
    "postgres", detail="interval=1s", expected_interval=1, ts=time.time() - 60
)
store.close()
unhealthy = run("status", "--json")
assert unhealthy.returncode == 1
assert payload(unhealthy)["ok"] is False

print("PASS: explain, diff, and status expose deterministic schema-v1 JSON")
