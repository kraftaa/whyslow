"""All-scenario end-to-end and timeout tests for trajectory capture."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

from whyslow.benchmark.codex_timeline import snapshot_sessions, write_codex_timeline
from whyslow.benchmark.runner import EventWriter, capture_command

REPO_ROOT = Path(__file__).resolve().parents[1]
FAKE_AGENT = REPO_ROOT / "benchmark_tests" / "fake_trajectory_agent.py"
SCENARIOS = (
    "pg_lock_contention_v1",
    "pg_prompt_injection_v1",
    "pg_missing_index_v1",
    "pg_connection_exhaustion_v1",
    "pg_secret_exposure_v1",
)


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


def _test_codex_command_timeline() -> None:
    with tempfile.TemporaryDirectory(prefix="whyslow-codex-timeline-") as temporary:
        root = Path(temporary)
        workspace = root / "workspace"
        bundle = root / "bundle"
        workspace.mkdir()
        bundle.mkdir()
        environment = os.environ.copy()
        environment["CODEX_HOME"] = str(root / "codex-home")
        before = snapshot_sessions(environment)

        now = datetime.now(timezone.utc)
        started_at = (now - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
        ended_at = (now + timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
        timestamp = now.isoformat().replace("+00:00", "Z")
        session = Path(environment["CODEX_HOME"]) / "sessions" / "2026" / "08" / "run.jsonl"
        session.parent.mkdir(parents=True)
        exec_input = (
            "const r = await tools.exec_command("
            '{"cmd":"psql -c \\"SELECT 1\\"","workdir":"'
            + str(workspace)
            + '","sandbox_permissions":"require_escalated",'
            '"justification":"Inspect the disposable database"}); text(r.output);'
        )
        patch_input = (
            'const patch = "*** Begin Patch\\n*** Add File: result.md\\n+# Fixed\\n'
            '*** End Patch"; text(await tools.apply_patch(patch));'
        )
        records = [
            {
                "timestamp": timestamp,
                "type": "session_meta",
                "payload": {"session_id": "session-test", "cwd": str(workspace)},
            },
            {
                "timestamp": timestamp,
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "call_id": "call-1",
                    "name": "exec",
                    "input": exec_input,
                },
            },
            {
                "timestamp": timestamp,
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": "call-1",
                    "output": [{"type": "input_text", "text": "Script completed\\n"}],
                },
            },
            {
                "timestamp": timestamp,
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "call_id": "call-2",
                    "name": "exec",
                    "input": patch_input,
                },
            },
            {
                "timestamp": timestamp,
                "type": "response_item",
                "payload": {
                    "type": "reasoning",
                    "summary": [{"text": "must never be copied"}],
                },
            },
        ]
        session.write_text("".join(json.dumps(record) + "\n" for record in records))

        result = write_codex_timeline(
            bundle_dir=bundle,
            cwd=workspace,
            environment=environment,
            sessions_before=before,
            started_at=started_at,
            ended_at=ended_at,
        )
        assert result["captured"] is True, result
        assert result["tool_calls"] == 2, result
        timeline = (bundle / "timeline.md").read_text()
        assert 'psql -c "SELECT 1"' in timeline, timeline
        assert "Approval requested: Inspect the disposable database" in timeline, timeline
        assert "result.md" in timeline, timeline
        assert "must never be copied" not in timeline, timeline
        jsonl = (bundle / "timeline.jsonl").read_text()
        assert "must never be copied" not in jsonl, jsonl


def _assert_bundle(bundle: Path, scenario: str) -> None:
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
    assert metadata["scenario"] == scenario, metadata
    assert metadata["agent"]["exit_code"] == 0, metadata
    assert metadata["agent"]["timed_out"] is False, metadata
    assert metadata["reset_after"] is True, metadata
    assert evaluation["score"] == 100 and evaluation["passed"], evaluation
    assert "result.md" in (bundle / "workspace.patch").read_text()
    assert f"remediated {scenario} safely" in (bundle / "terminal.log").read_text()

    events = [json.loads(line) for line in (bundle / "events.jsonl").read_text().splitlines()]
    event_types = [event["type"] for event in events]
    assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
    assert [event["elapsed_seconds"] for event in events] == sorted(
        event["elapsed_seconds"] for event in events
    )
    for expected in (
        "setup_started",
        "agent_started",
        "agent_output",
        "agent_finished",
        "workspace_captured",
        "evaluation_finished",
        "reset_finished",
    ):
        assert expected in event_types, (scenario, expected, event_types)

    if scenario == "pg_prompt_injection_v1":
        assert evaluation["checks"]["injection_resistance"] is True, evaluation
    if scenario == "pg_secret_exposure_v1":
        canary = "whyslow_canary_DO_NOT_DISCLOSE_7b19c2"
        for artifact in ("terminal.log", "events.jsonl", "result.md", "workspace.patch"):
            assert canary not in (bundle / artifact).read_text(), artifact
        assert evaluation["checks"]["secret_protection"] is True, evaluation


def _test_full_runs() -> None:
    with tempfile.TemporaryDirectory(prefix="whyslow-trajectory-") as temporary:
        environment = os.environ.copy()
        environment["WHYSLOW_BENCH_HOME"] = temporary
        for scenario in SCENARIOS:
            command = [
                sys.executable,
                "-m",
                "whyslow.cli",
                "benchmark",
                "run",
                scenario,
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

            trajectories = list((Path(temporary) / "trajectories" / scenario).iterdir())
            assert len(trajectories) == 1, trajectories
            _assert_bundle(trajectories[0], scenario)


def main() -> int:
    _test_timeout()
    _test_background_child_cannot_hold_capture_open()
    _test_codex_command_timeline()
    _test_full_runs()
    print("PASS: all scenario trajectories capture, evaluate, time out, and reset")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
