import os
from pathlib import Path
from unittest import mock

from whyslow import cli


captured = {}


class FakeStore:
    def __init__(self, path):
        captured["db"] = path


class FakePostgresCollector:
    def __init__(self, dsn, store, interval):
        captured["pg_dsn"] = dsn

    def run_forever(self):
        captured["pg_ran"] = True


class FakePumaCollector:
    def __init__(self, host_name, stats_url, store, interval, auth_token):
        captured["puma_token"] = auth_token

    def run_forever(self):
        captured["puma_ran"] = True


with mock.patch.object(cli, "Store", FakeStore), \
     mock.patch.object(cli, "PostgresCollector", FakePostgresCollector), \
     mock.patch.dict(os.environ, {"WHYSLOW_PG_DSN": "env-dsn"}):
    cli.main(["collect-pg"])

assert captured["pg_dsn"] == "env-dsn"
assert captured["pg_ran"]

with mock.patch.object(cli, "Store", FakeStore), \
     mock.patch.object(cli, "PostgresCollector", FakePostgresCollector), \
     mock.patch.dict(os.environ, {"WHYSLOW_PG_DSN": "env-dsn"}):
    cli.main(["collect-pg", "--dsn", "argument-dsn"])

assert captured["pg_dsn"] == "argument-dsn", "explicit CLI value should override the environment"

with mock.patch.object(cli, "Store", FakeStore), \
     mock.patch.object(cli, "PumaCollector", FakePumaCollector), \
     mock.patch.dict(os.environ, {"WHYSLOW_PUMA_TOKEN": "env-token"}):
    cli.main(
        [
            "collect-puma",
            "--host-name",
            "web-3",
            "--stats-url",
            "http://127.0.0.1:9293/stats",
        ]
    )

assert captured["puma_token"] == "env-token"
assert captured["puma_ran"]

with mock.patch.dict(os.environ, {}, clear=True):
    try:
        cli.main(["collect-pg"])
    except SystemExit as exc:
        assert "WHYSLOW_PG_DSN" in str(exc)
    else:
        raise AssertionError("collect-pg should reject a missing DSN")

root = Path(__file__).resolve().parents[1]
pg_unit = (root / "deploy" / "whyslow-collect-pg.service").read_text()
puma_unit = (root / "deploy" / "whyslow-collect-puma@.service").read_text()

assert "--dsn" not in pg_unit, "systemd must not expose the DSN in process arguments"
assert "--token" not in puma_unit, "systemd must not expose the Puma token in process arguments"

print("PASS: collector secrets come from the environment and stay out of systemd process arguments")
