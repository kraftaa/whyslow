"""Regression tests for scenario-agnostic benchmark CLI output."""

from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import sys

from whyslow.benchmark.cli import _print_evaluate, _print_reset, _print_setup


def _capture(function, payload: dict) -> str:
    output = StringIO()
    with redirect_stdout(output):
        function(payload)
    return output.getvalue()


def main() -> int:
    missing_index = _capture(
        _print_setup,
        {
            "scenario": "pg_missing_index_v1",
            "workspace": "/tmp/workspace",
            "rows": 50000,
        },
    )
    assert "scenario ready: pg_missing_index_v1" in missing_index
    assert "rows: 50000" in missing_index
    assert "agent workspace:  /tmp/workspace" in missing_index

    connection_exhaustion = _capture(
        _print_setup,
        {
            "scenario": "pg_connection_exhaustion_v1",
            "workspace": "/tmp/workspace",
            "connection_limit": 8,
            "leaked_connections": 7,
        },
    )
    assert "connection limit: 8" in connection_exhaustion
    assert "leaked connections: 7" in connection_exhaustion

    evaluation = _capture(
        _print_evaluate,
        {
            "scenario": "pg_missing_index_v1",
            "score": 100,
            "max_score": 100,
            "passed": True,
            "components": {
                "query_recovery": {
                    "ok": True,
                    "points": 50,
                    "max": 50,
                    "detail": "target query uses an index",
                }
            },
        },
    )
    assert "100/100 (PASS)" in evaluation
    assert "query_recovery" in evaluation

    stateless_reset = _capture(
        _print_reset,
        {
            "scenario": "pg_missing_index_v1",
            "docker_attempted": True,
            "docker_down_ok": True,
            "docker_error": "",
        },
    )
    assert "reset pg_missing_index_v1: docker down" in stateless_reset

    actor_reset = _capture(
        _print_reset,
        {
            "scenario": "pg_connection_exhaustion_v1",
            "actor_processes_killed": 8,
            "backends_terminated": 0,
            "docker_attempted": False,
            "docker_down_ok": True,
        },
    )
    assert "8 actor process(es) stopped" in actor_reset
    assert "0 backend(s) terminated" in actor_reset

    print("PASS: setup, evaluate, and reset output accepts every scenario result shape")
    return 0


if __name__ == "__main__":
    sys.exit(main())
