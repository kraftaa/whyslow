"""Structured trajectory capture for external benchmark responders.

The runner captures observable behavior only: structured tool events when an
adapter or agent protocol is available, terminal input/output, process metadata,
PostgreSQL server statements (Docker mode), workspace changes, and deterministic
final-state evaluation. It never captures private model reasoning.
"""

from __future__ import annotations

import difflib
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import selectors
import shutil
import signal
import subprocess
import sys
import time
import uuid
import platform

from whyslow import __version__
from . import common
from . import agent_timeline
from . import trajectory_score

SCHEMA_VERSION = "whyslow-trajectory/1"
DEFAULT_TIMEOUT_SECONDS = 600.0
DEFAULT_MAX_OUTPUT_BYTES = 10 * 1024 * 1024
MAX_DIFF_FILE_BYTES = 256 * 1024


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class EventWriter:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = path.open("a", encoding="utf-8")
        self._started = time.monotonic()
        self._sequence = 0

    def write(self, event_type: str, **fields) -> None:
        self._sequence += 1
        value = {
            "sequence": self._sequence,
            "timestamp": _utc_now(),
            "elapsed_seconds": round(time.monotonic() - self._started, 6),
            "type": event_type,
            **fields,
        }
        self._handle.write(json.dumps(value, sort_keys=True) + "\n")
        self._handle.flush()

    def close(self) -> None:
        self._handle.close()


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot_workspace(root: Path) -> tuple[dict, dict[str, str]]:
    """Return a serializable manifest and bounded text content for diffs."""
    manifest: dict[str, dict] = {}
    text_content: dict[str, str] = {}
    if not root.exists():
        return manifest, text_content
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        if path.is_symlink():
            manifest[relative] = {"type": "symlink", "target": os.readlink(path)}
            continue
        if not path.is_file():
            continue
        stat = path.stat()
        manifest[relative] = {
            "type": "file",
            "size": stat.st_size,
            "sha256": _file_digest(path),
            "mode": oct(stat.st_mode & 0o777),
        }
        if stat.st_size <= MAX_DIFF_FILE_BYTES:
            try:
                text_content[relative] = path.read_text()
            except (OSError, UnicodeDecodeError):
                pass
    return manifest, text_content


def workspace_patch(before: dict[str, str], after: dict[str, str]) -> str:
    chunks = []
    for relative in sorted(set(before) | set(after)):
        old = before.get(relative, "").splitlines(keepends=True)
        new = after.get(relative, "").splitlines(keepends=True)
        if old == new:
            continue
        chunks.extend(
            difflib.unified_diff(
                old,
                new,
                fromfile=f"a/{relative}" if relative in before else "/dev/null",
                tofile=f"b/{relative}" if relative in after else "/dev/null",
            )
        )
    return "".join(chunks)


def _stop_process_group(proc: subprocess.Popen, *, grace_seconds: float = 3.0) -> None:
    """Stop the command and children kept in the session created for the run."""
    if os.name != "posix":
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=grace_seconds)
            except subprocess.TimeoutExpired:
                proc.kill()
        return

    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    if proc.poll() is None:
        try:
            proc.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            pass
    # A child may still hold the PTY/pipe after the leader exits.
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


class _OutputRecorder:
    def __init__(
        self,
        terminal,
        events: EventWriter,
        *,
        max_output_bytes: int,
        mirror,
    ):
        self.terminal = terminal
        self.events = events
        self.max_output_bytes = max_output_bytes
        self.mirror = mirror
        self.written = 0
        self.truncated = False
        self.line_buffer = b""

    def record(self, chunk: bytes) -> None:
        if self.mirror is not None:
            try:
                self.mirror.write(chunk)
                self.mirror.flush()
            except (BrokenPipeError, OSError):
                pass
        remaining = self.max_output_bytes - self.written
        kept = chunk[: max(0, remaining)]
        if kept:
            self.terminal.write(kept)
            self.terminal.flush()
            self.written += len(kept)
            self.line_buffer += kept
            while b"\n" in self.line_buffer:
                line, self.line_buffer = self.line_buffer.split(b"\n", 1)
                self.events.write("agent_output", text=line.decode("utf-8", errors="replace"))
        if len(kept) != len(chunk) and not self.truncated:
            self.truncated = True
            self.events.write("agent_output_truncated", max_output_bytes=self.max_output_bytes)

    def finish(self) -> None:
        if self.line_buffer:
            self.events.write(
                "agent_output", text=self.line_buffer.decode("utf-8", errors="replace")
            )


def _capture_noninteractive(
    command: list[str],
    cwd: Path,
    environment: dict[str, str],
    timeout: float,
    recorder: _OutputRecorder,
) -> tuple[int, bool]:
    proc = subprocess.Popen(
        command,
        cwd=cwd,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    selector = selectors.DefaultSelector()
    assert proc.stdout is not None
    selector.register(proc.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout
    timed_out = False
    try:
        while proc.poll() is None or selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                _stop_process_group(proc)
                break
            events = selector.select(timeout=max(0.0, min(0.2, remaining)))
            for key, _ in events:
                chunk = os.read(key.fileobj.fileno(), 65536)
                if chunk:
                    recorder.record(chunk)
                else:
                    selector.unregister(key.fileobj)
            if proc.poll() is not None and not selector.get_map():
                break
    finally:
        selector.close()
        _stop_process_group(proc, grace_seconds=0.2)
    return int(proc.returncode if proc.returncode is not None else -1), timed_out


def _capture_interactive(
    command: list[str],
    cwd: Path,
    environment: dict[str, str],
    timeout: float,
    recorder: _OutputRecorder,
    events: EventWriter,
) -> tuple[int, bool]:
    import fcntl
    import pty
    import select
    import termios
    import tty

    master, slave = pty.openpty()
    stdin_fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(stdin_fd)
    try:
        try:
            size = fcntl.ioctl(stdin_fd, termios.TIOCGWINSZ, b"\0" * 8)
            fcntl.ioctl(slave, termios.TIOCSWINSZ, size)
        except OSError:
            pass
        proc = subprocess.Popen(
            command,
            cwd=cwd,
            env=environment,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            start_new_session=True,
        )
        os.close(slave)
        tty.setraw(stdin_fd)
        deadline = time.monotonic() + timeout
        timed_out = False
        master_open = True
        while proc.poll() is None or master_open:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                _stop_process_group(proc)
                break
            readable, _, _ = select.select(
                [fd for fd in (master if master_open else None, stdin_fd) if fd is not None],
                [],
                [],
                max(0.0, min(0.2, remaining)),
            )
            if master_open and master in readable:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    chunk = b""
                if chunk:
                    recorder.record(chunk)
                else:
                    master_open = False
            if stdin_fd in readable and proc.poll() is None:
                user_input = os.read(stdin_fd, 4096)
                if user_input:
                    os.write(master, user_input)
                    events.write("agent_input", text=user_input.decode("utf-8", errors="replace"))
            if proc.poll() is not None and not master_open:
                break
        _stop_process_group(proc, grace_seconds=0.2)
        return int(proc.returncode if proc.returncode is not None else -1), timed_out
    finally:
        termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_settings)
        try:
            os.close(master)
        except OSError:
            pass


def capture_command(
    command: list[str],
    *,
    cwd: Path,
    bundle_dir: Path,
    events: EventWriter,
    timeout: float,
    environment: dict[str, str],
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
) -> dict:
    """Run one responder command and capture its terminal trajectory."""
    if not command:
        raise ValueError("agent command must not be empty")
    environment = environment.copy()
    environment["WHYSLOW_BENCH_TIMELINE_PATH"] = str(
        bundle_dir / agent_timeline.GENERIC_EVENTS_FILENAME
    )
    timeline_snapshot = agent_timeline.snapshot_agent_sources(command, environment)
    started_at = _utc_now()
    started = time.monotonic()
    events.write("agent_started", command=command, cwd=str(cwd), timeout_seconds=timeout)
    with (bundle_dir / "terminal.log").open("wb") as terminal:
        mirror = getattr(sys.stdout, "buffer", None)
        recorder = _OutputRecorder(
            terminal,
            events,
            max_output_bytes=max_output_bytes,
            mirror=mirror,
        )
        interactive = os.name == "posix" and sys.stdin.isatty()
        if interactive:
            exit_code, timed_out = _capture_interactive(
                command, cwd, environment, timeout, recorder, events
            )
        else:
            exit_code, timed_out = _capture_noninteractive(
                command, cwd, environment, timeout, recorder
            )
        recorder.finish()
    result = {
        "command": command,
        "started_at": started_at,
        "ended_at": _utc_now(),
        "duration_seconds": round(time.monotonic() - started, 3),
        "exit_code": exit_code,
        "timed_out": timed_out,
        "interactive_pty": interactive,
        "terminal_output_truncated": recorder.truncated,
        "terminal_bytes_captured": recorder.written,
    }
    try:
        result["command_timeline"] = agent_timeline.write_agent_timeline(
            command=command,
            bundle_dir=bundle_dir,
            cwd=cwd,
            environment=environment,
            source_snapshot=timeline_snapshot,
            started_at=result["started_at"],
            ended_at=result["ended_at"],
        )
    except Exception as exc:
        result["command_timeline"] = {
            "captured": False,
            "reason": f"timeline adapter error: {type(exc).__name__}: {exc}",
        }
    events.write("agent_finished", **result)
    return result


def _agent_environment(ctx: common.Context, run_id: str) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "PGHOST": ctx.config.host,
            "PGPORT": str(ctx.config.port),
            "PGDATABASE": ctx.config.dbname,
            "PGUSER": common.AGENT_USER,
            "PGPASSWORD": common.AGENT_PASSWORD,
            "WHYSLOW_BENCH_SCENARIO": ctx.scenario_id,
            "WHYSLOW_BENCH_RUN_ID": run_id,
            "WHYSLOW_BENCH_WORKSPACE": str(ctx.workspace_dir),
        }
    )
    return environment


def run_trajectory(
    scenario_module,
    ctx: common.Context,
    command: list[str],
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    reset_after: bool = False,
) -> dict:
    """Set up, run, capture, evaluate, and optionally reset one scenario."""
    if timeout <= 0:
        raise ValueError("timeout must be greater than zero")
    run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:8]
    bundle_dir = common.benchmark_home() / "trajectories" / ctx.scenario_id / run_id
    bundle_dir.mkdir(parents=True, exist_ok=False)
    events = EventWriter(bundle_dir / "events.jsonl")
    run_started_at = _utc_now()
    run_started = time.monotonic()
    setup_info = None
    agent = None
    evaluation = None
    trajectory_evaluation = None
    reset_info = None
    postgres_log = {"captured": False, "reason": "agent did not run"}
    try:
        events.write("setup_started", scenario=ctx.scenario_id, run_id=run_id)
        setup_info = scenario_module.setup(ctx)
        events.write("setup_finished", setup=setup_info)
        before_manifest, before_text = snapshot_workspace(ctx.workspace_dir)
        common.write_json(bundle_dir / "workspace-before.json", before_manifest)

        agent = capture_command(
            command,
            cwd=ctx.workspace_dir,
            bundle_dir=bundle_dir,
            events=events,
            timeout=timeout,
            environment=_agent_environment(ctx, run_id),
        )

        postgres_log = {"captured": False, "reason": "non-Docker benchmark mode"}
        if common.use_docker():
            ok, log_text = common.compose_logs(since=agent["started_at"])
            (bundle_dir / "postgres.log").write_text(log_text)
            postgres_log = {"captured": ok, "path": "postgres.log", "bytes": len(log_text)}
        events.write("postgres_log_captured", **postgres_log)

        after_manifest, after_text = snapshot_workspace(ctx.workspace_dir)
        common.write_json(bundle_dir / "workspace-after.json", after_manifest)
        (bundle_dir / "workspace.patch").write_text(workspace_patch(before_text, after_text))
        events.write(
            "workspace_captured",
            before_files=len(before_manifest),
            after_files=len(after_manifest),
        )

        events.write("evaluation_started")
        evaluation = scenario_module.evaluate(ctx)
        common.write_json(bundle_dir / "evaluation.json", evaluation)
        common.write_json(ctx.results_dir / f"{ctx.scenario_id}-{run_id}.json", evaluation)
        report = ctx.workspace_dir / "result.md"
        if report.is_file():
            shutil.copy2(report, bundle_dir / "result.md")
        events.write(
            "evaluation_finished",
            score=evaluation["score"],
            max_score=evaluation["max_score"],
            passed=evaluation["passed"],
        )
        events.write("trajectory_evaluation_started")
        trajectory_evaluation = trajectory_score.evaluate_trajectory(bundle_dir, agent)
        common.write_json(bundle_dir / "trajectory-evaluation.json", trajectory_evaluation)
        events.write(
            "trajectory_evaluation_finished",
            available=trajectory_evaluation["available"],
            score=trajectory_evaluation.get("score"),
            passed=trajectory_evaluation.get("passed"),
        )
        if reset_after:
            events.write("reset_started")
            reset_info = scenario_module.reset(ctx)
            events.write("reset_finished", reset=reset_info)

        exit_code = (
            0
            if evaluation["passed"]
            and agent["exit_code"] == 0
            and not agent["timed_out"]
            and (not reset_after or reset_info.get("docker_down_ok", True))
            else 1
        )
        metadata = {
            "schema_version": SCHEMA_VERSION,
            "whyslow_version": __version__,
            "run_id": run_id,
            "scenario": ctx.scenario_id,
            "status": "completed",
            "started_at": run_started_at,
            "ended_at": _utc_now(),
            "duration_seconds": round(time.monotonic() - run_started, 3),
            "runtime": {
                "python": platform.python_version(),
                "platform": platform.platform(),
                "docker_mode": common.use_docker(),
            },
            "agent": agent,
            "setup": setup_info,
            "evaluation": {
                "score": evaluation["score"],
                "max_score": evaluation["max_score"],
                "passed": evaluation["passed"],
                "path": "evaluation.json",
            },
            "trajectory_evaluation": {
                "available": trajectory_evaluation["available"],
                "score": trajectory_evaluation.get("score"),
                "max_score": trajectory_evaluation.get("max_score", 100),
                "passed": trajectory_evaluation.get("passed"),
                "path": "trajectory-evaluation.json",
                "affects_final_state_score": False,
            },
            "postgres_log": postgres_log,
            "reset_after": reset_after,
            "reset": reset_info,
            "exit_code": exit_code,
            "artifacts": {
                "events": "events.jsonl",
                "terminal": "terminal.log",
                "workspace_before": "workspace-before.json",
                "workspace_after": "workspace-after.json",
                "workspace_patch": "workspace.patch",
                "result": "result.md" if (bundle_dir / "result.md").is_file() else None,
                "timeline_jsonl": (
                    "timeline.jsonl" if (bundle_dir / "timeline.jsonl").is_file() else None
                ),
                "timeline_markdown": (
                    "timeline.md" if (bundle_dir / "timeline.md").is_file() else None
                ),
                "agent_events": (
                    agent_timeline.GENERIC_EVENTS_FILENAME
                    if (bundle_dir / agent_timeline.GENERIC_EVENTS_FILENAME).is_file()
                    else None
                ),
                "trajectory_evaluation": "trajectory-evaluation.json",
            },
        }
        events.write("runner_finished", bundle=str(bundle_dir), exit_code=exit_code)
        common.write_json(bundle_dir / "metadata.json", metadata)
        return {
            "bundle": str(bundle_dir),
            "metadata": metadata,
            "evaluation": evaluation,
            "trajectory_evaluation": trajectory_evaluation,
            "exit_code": exit_code,
        }
    except Exception as exc:
        events.write("runner_error", error_type=type(exc).__name__, message=str(exc))
        if reset_after and reset_info is None:
            try:
                events.write("reset_started", reason="runner_error")
                reset_info = scenario_module.reset(ctx)
                events.write("reset_finished", reset=reset_info)
            except Exception as reset_exc:
                events.write(
                    "reset_failed",
                    error_type=type(reset_exc).__name__,
                    message=str(reset_exc),
                )
        common.write_json(
            bundle_dir / "metadata.json",
            {
                "schema_version": SCHEMA_VERSION,
                "whyslow_version": __version__,
                "run_id": run_id,
                "scenario": ctx.scenario_id,
                "started_at": run_started_at,
                "ended_at": _utc_now(),
                "duration_seconds": round(time.monotonic() - run_started, 3),
                "status": "error",
                "error": {"type": type(exc).__name__, "message": str(exc)},
                "setup": setup_info,
                "agent": agent,
                "trajectory_evaluation": trajectory_evaluation,
                "postgres_log": postgres_log,
                "reset_after": reset_after,
                "reset": reset_info,
            },
        )
        raise
    finally:
        events.close()
