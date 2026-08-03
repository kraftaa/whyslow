"""Explicit epoch-zero timestamps must never be mistaken for omitted values."""

from pathlib import Path
import subprocess
import sys
import tempfile

from whyslow import doctor as doctor_mod
from whyslow import status as status_mod
from whyslow.collector_postgres import PostgresCollector
from whyslow.storage import Store


with tempfile.TemporaryDirectory(prefix="whyslow-timestamp-zero-") as temp:
    path = Path(temp) / "store.sqlite3"
    store = Store(path)

    store.write_sessions(
        [(1, "client backend", "role", "web-1", "active", "cpu", "SELECT 1")],
        ts=0,
    )
    store.write_blocking_edges(
        [(2, "web-1", 1, "batch", "role", "SELECT 1")],
        ts=0,
    )
    store.mark_blocking_edges_ended([(2, 1)], ts=0)
    store.write_puma_stat("web-1", 0, 16, 16, 1, 100.0, ts=0)
    store.write_cloudwatch_metric("CPUUtilization", 1.0, ts=0, source_instance="writer-1")
    store.write_event("deploy", "epoch", None, ts=0)
    store.write_heartbeat("postgres", ts=0, instance_role="primary", expected_interval=1)

    assert store.sessions_in(0, 0)[0][0] == 0
    assert store.blocking_edges_in(0, 0)[0][0] == 0
    assert store.blocking_edges_in(0, 0)[0][7] == 0
    assert store.puma_stats_in(0, 0)[0][0] == 0
    assert store.cloudwatch_metrics_in(0, 0)[0][0] == 0
    assert store.events_in(0, 0)[0][0] == 0
    assert store.get_heartbeats()[0][1] == 0

    status = status_mod.status(store, now=0)
    assert status["now"] == 0
    assert status["collectors"][0]["age_seconds"] == 0
    assert not status["collectors"][0]["stale"]

    readiness = doctor_mod.doctor(store, now=0)
    assert readiness["checked_at"] == 0

    assert store.retire_collector("postgres", ts=0)
    assert store.get_collector_memberships() == [("postgres", 0, 0)]

    # A false fallback to the current clock would delete this row as ancient.
    store.prune(now=0)
    assert store.sessions_in(0, 0)

    class GapProbe:
        def __init__(self):
            self.end = None

        def get_heartbeats(self):
            return [("postgres", 0, None, "primary", 1)]

        def collector_coverage_gap_after(self, collector, start, end):
            assert collector == "postgres"
            assert start == 0
            self.end = end
            return None

    probe = GapProbe()
    PostgresCollector("unused", probe)._prepare_for_connection(now=0)
    assert probe.end == 0
    store.close()

    # Exercise the public CLI path that originally exposed the bug.
    cli_path = Path(temp) / "cli.sqlite3"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "whyslow.cli",
            "event",
            "--source",
            "deploy",
            "--kind",
            "epoch",
            "--at",
            "0",
            "--db",
            str(cli_path),
        ],
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    cli_store = Store(cli_path)
    assert cli_store.events_in(0, 0) == [(0.0, "deploy", "epoch", None)]
    cli_store.close()

print("PASS: explicit epoch-zero timestamps survive storage, checks, pruning, and CLI input")
