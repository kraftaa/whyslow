"""End-to-end and timeout tests for structured trajectory capture."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from whyslow.benchmark.runner import EventWriter, capture_command

REPO_ROOT = Path(__file__).resolve().parents[1]
FAKE_AGENT = REPO_ROOT / "benchmark_tests" / "fake_missing_index_agent.py"


def _test_timeout() -> None:
    with tempfile.TemporaryDirectory(prefix="whyslow-capture-") as temporary:
        bundle = Path(temporary)
        events = EventWriter(bundle / "events.jsonl")
        try:
            result = capture_command(
                [sys.executable, "-u", "-c", "import time; print('started'); time.sleep(5)"],
                cwd=bundle,
                bundle_dir=bundle,
                events=events,
                timeout=0.2,
                environment=os.environ.copy(),
            )
        finally:
            events.close()
        assert result["timed_out"] is True, result
        assert result["duration_seconds"] < 4, result
        assert "started" in (bundle / "terminal.log").read_text(), result
        event_types = [
            json.loads(line)["type"] for line in (bundle / "events.jsonl").read_text().splitlines()
        ]
        assert "agent_started" in event_types, event_types
        assert "agent_finished" in event_types, event_types


def _test_background_child_cannot_hold_capture_open() -> None:
    with tempfile.TemporaryDirectory(prefix="whyslow-capture-child-") as temporary:
        bundle = Path(temporary)
        events = EventWriter(bundle / "events.jsonl")
        child_command = (
            "import subprocess, sys; "
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(5)']); "
            "print('parent finished')"
        )
        try:
            result = capture_command(
                [sys.executable, "-u", "-c", child_command],
                cwd=bundle,
                bundle_dir=bundle,
                events=events,
                timeout=0.2,
                environment=os.environ.copy(),
            )
        finally:
            events.close()
        assert result["timed_out"] is True, result
        assert result["duration_seconds"] < 4, result
        assert "parent finished" in (bundle / "terminal.log").read_text(), result


def _test_full_run() -> None:
    with tempfile.TemporaryDirectory(prefix="whyslow-trajectory-") as temporary:
        environment = os.environ.copy()
        environment["WHYSLOW_BENCH_HOME"] = temporary
        command = [
            sys.executable,
            "-m",
            "whyslow.cli",
            "benchmark",
            "run",
            "pg_missing_index_v1",
            "--timeout",
            "30",
            "--reset-after",
            "--",
            sys.executable,
            str(FAKE_AGENT),
        ]
        completed = subprocess.run(
            command,
            cwd=REPO_ROOT,
            env=environment,
            text=True,
            capture_output=True,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert "100/100 (PASS)" in completed.stdout, completed.stdout

        trajectories = list((Path(temporary) / "trajectories" / "pg_missing_index_v1").iterdir())
        assert len(trajectories) == 1, trajectories
        bundle = trajectories[0]
        required = {
            "metadata.json",
            "events.jsonl",
            "terminal.log",
            "workspace-before.json",
            "workspace-after.json",
            "workspace.patch",
            "evaluation.json",
            "result.md",
        }
        assert required <= {path.name for path in bundle.iterdir()}, list(bundle.iterdir())

        metadata = json.loads((bundle / "metadata.json").read_text())
        evaluation = json.loads((bundle / "evaluation.json").read_text())
        assert metadata["schema_version"] == "whyslow-trajectory/1", metadata
        assert metadata["agent"]["exit_code"] == 0, metadata
        assert metadata["agent"]["timed_out"] is False, metadata
        assert metadata["reset_after"] is True, metadata
        assert evaluation["score"] == 100 and evaluation["passed"], evaluation
        assert "result.md" in (bundle / "workspace.patch").read_text()
        assert "created targeted index" in (bundle / "terminal.log").read_text()
        event_types = [
            json.loads(line)["type"] for line in (bundle / "events.jsonl").read_text().splitlines()
        ]
        for expected in (
            "setup_started",
            "agent_started",
            "agent_output",
            "agent_finished",
            "workspace_captured",
            "evaluation_finished",
            "reset_finished",
        ):
            assert expected in event_types, (expected, event_types)


def main() -> int:
    _test_timeout()
    _test_background_child_cannot_hold_capture_open()
    _test_full_run()
    print("PASS: trajectory runner captures, evaluates, times out, and resets")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
