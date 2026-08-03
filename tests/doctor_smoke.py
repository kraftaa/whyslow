import json
import os
import shutil
import stat
import subprocess
import sys
import time

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

import boto3
from moto import mock_aws

from fake_puma import start as start_fake_puma
from whyslow import doctor as doctor_mod
from whyslow.storage import Store


ROOT = "/tmp/whyslow-doctor"
DB_PATH = f"{ROOT}/store.sqlite3"
DSN = "dbname=postgres user=postgres password=postgres host=127.0.0.1"
shutil.rmtree(ROOT, ignore_errors=True)

store = Store(DB_PATH)
now = time.time()
store.write_heartbeat(
    "postgres",
    detail="interval=1.0s",
    instance_role="primary",
    expected_interval=1,
    ts=now,
)

healthy = doctor_mod.doctor(store, dsn=DSN, now=now + 1)
by_name = {check["name"]: check for check in healthy["checks"]}
assert healthy["ok"], healthy
for name in (
    "sqlite_integrity",
    "sqlite_wal",
    "schema_version",
    "schema",
    "store_permissions",
    "postgres_collector",
    "postgres_connection",
):
    assert by_name[name]["state"] == "pass", by_name[name]
assert by_name["puma_endpoint"]["state"] == "warn"
assert by_name["cloudwatch_api"]["state"] == "warn"
assert "Result: READY" in doctor_mod.render(healthy)

# Optional dependency checks exercise real protocol clients against local
# fakes, including the expected Puma JSON shape and CloudWatch dimension.
puma_server = start_fake_puma(9303)
try:
    puma_check = doctor_mod._check_puma("http://127.0.0.1:9303/stats", None)
    assert puma_check["state"] == "pass", puma_check
finally:
    puma_server.shutdown()

with mock_aws():
    cloudwatch = boto3.client("cloudwatch", region_name="us-east-1")
    cloudwatch.put_metric_data(
        Namespace="AWS/RDS",
        MetricData=[{
            "MetricName": "CPUUtilization",
            "Dimensions": [
                {"Name": "DBInstanceIdentifier", "Value": "doctor-db"}
            ],
            "Timestamp": time.time() - 180,
            "Value": 42.0,
            "Unit": "Percent",
        }],
    )
    cloudwatch_check = doctor_mod._check_cloudwatch("doctor-db", "us-east-1")
    assert cloudwatch_check["state"] == "pass", cloudwatch_check

# Permission regressions are failures, not cosmetic warnings.
os.chmod(DB_PATH, 0o644)
unsafe = doctor_mod.doctor(store, now=now + 1)
unsafe_permissions = next(
    check for check in unsafe["checks"] if check["name"] == "store_permissions"
)
assert not unsafe["ok"]
assert unsafe_permissions["state"] == "fail"
store.close()

# Opening the store repairs its mode, but doctor still reports that it was
# unsafe on entry instead of silently turning the check green.
permission_cli = subprocess.run(
    [sys.executable, "-m", "whyslow.cli", "doctor", "--json", "--db", DB_PATH],
    text=True,
    capture_output=True,
)
assert permission_cli.returncode == 1
permission_payload = json.loads(permission_cli.stdout)
permission_check = next(
    check for check in permission_payload["checks"]
    if check["name"] == "store_permissions"
)
assert permission_check["state"] == "fail"
assert "before open" in permission_check["detail"]

# Exercise the public CLI and stable JSON result. Keep the DSN in the
# environment so it never appears in process arguments.
env = os.environ.copy()
env["WHYSLOW_PG_DSN"] = DSN
cli = subprocess.run(
    [sys.executable, "-m", "whyslow.cli", "doctor", "--json", "--db", DB_PATH],
    text=True,
    capture_output=True,
    env=env,
)
assert cli.returncode == 0, cli.stderr
payload = json.loads(cli.stdout)
assert payload["ok"]
assert all(set(check) == {"name", "state", "detail"} for check in payload["checks"])

# A stale required collector makes doctor fail and produces no secret echo.
store = Store(DB_PATH)
store.write_heartbeat(
    "postgres",
    detail="interval=1.0s",
    instance_role="primary",
    expected_interval=1,
    ts=time.time() - 60,
)
store.close()
failed = subprocess.run(
    [sys.executable, "-m", "whyslow.cli", "doctor", "--json", "--db", DB_PATH],
    text=True,
    capture_output=True,
    env=env,
)
assert failed.returncode == 1
failed_payload = json.loads(failed.stdout)
assert not failed_payload["ok"]
assert "password=postgres" not in failed.stdout + failed.stderr

mode = stat.S_IMODE(os.stat(DB_PATH).st_mode)
assert mode == 0o600
print("PASS: doctor validates readiness, emits JSON, fails safely, and hides credentials")
