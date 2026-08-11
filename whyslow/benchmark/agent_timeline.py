"""Extract observable agent tool activity into a portable trajectory timeline.

Built-in adapters understand Codex and Claude Code local JSONL sessions. A
provider-neutral JSONL protocol lets any other harness emit the same events.
Adapters read only tool calls and observable outputs; messages, model thinking,
and private reasoning are deliberately ignored.
"""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import re
from typing import Any


TIMELINE_SCHEMA_VERSION = "whyslow-command-timeline/1"
MAX_RESULT_BYTES = 64 * 1024
GENERIC_EVENTS_FILENAME = "agent-events.jsonl"


def agent_provider(command: list[str]) -> str | None:
    """Return the built-in structured-session provider for a command."""
    if not command:
        return None
    executable = Path(command[0]).name.lower()
    if executable in {"codex", "codex.exe"}:
        return "codex"
    if executable in {"claude", "claude.exe", "claude-code", "claude-code.exe"}:
        return "claude"
    return None


def _codex_sessions_dir(environment: dict[str, str]) -> Path:
    configured = environment.get("CODEX_HOME")
    return (
        Path(configured).expanduser() / "sessions"
        if configured
        else Path.home() / ".codex/sessions"
    )


def snapshot_sessions(environment: dict[str, str]) -> dict[Path, tuple[int, int]]:
    """Capture sizes and mtimes so a run can identify its new/changed session."""
    root = _codex_sessions_dir(environment)
    if not root.is_dir():
        return {}
    result: dict[Path, tuple[int, int]] = {}
    for path in root.rglob("*.jsonl"):
        try:
            stat = path.stat()
        except OSError:
            continue
        result[path] = (stat.st_size, stat.st_mtime_ns)
    return result


def changed_sessions(
    environment: dict[str, str], before: dict[Path, tuple[int, int]]
) -> list[Path]:
    after = snapshot_sessions(environment)
    return sorted(path for path, state in after.items() if before.get(path) != state)


def _claude_projects_dir(environment: dict[str, str]) -> Path:
    configured = environment.get("CLAUDE_CONFIG_DIR")
    root = Path(configured).expanduser() if configured else Path.home() / ".claude"
    return root / "projects"


def snapshot_claude_sessions(environment: dict[str, str]) -> dict[Path, tuple[int, int]]:
    root = _claude_projects_dir(environment)
    if not root.is_dir():
        return {}
    result: dict[Path, tuple[int, int]] = {}
    for path in root.rglob("*.jsonl"):
        try:
            stat = path.stat()
        except OSError:
            continue
        result[path] = (stat.st_size, stat.st_mtime_ns)
    return result


def changed_claude_sessions(
    environment: dict[str, str], before: dict[Path, tuple[int, int]]
) -> list[Path]:
    after = snapshot_claude_sessions(environment)
    return sorted(path for path, state in after.items() if before.get(path) != state)


def snapshot_agent_sources(command: list[str], environment: dict[str, str]) -> dict:
    provider = agent_provider(command)
    if provider == "codex":
        sessions = snapshot_sessions(environment)
    elif provider == "claude":
        sessions = snapshot_claude_sessions(environment)
    else:
        sessions = {}
    return {"provider": provider, "sessions": sessions}


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _read_jsonl(path: Path) -> list[dict]:
    records = []
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    records.append(value)
    except OSError:
        return []
    return records


def _session_metadata(records: list[dict]) -> dict:
    for record in records:
        if record.get("type") == "session_meta" and isinstance(record.get("payload"), dict):
            return record["payload"]
    return {}


def _select_session(
    paths: list[Path], cwd: Path, started_at: str, ended_at: str
) -> tuple[Path, list[dict], dict] | None:
    start = _parse_timestamp(started_at)
    end = _parse_timestamp(ended_at)
    candidates = []
    for path in paths:
        records = _read_jsonl(path)
        metadata = _session_metadata(records)
        session_cwd = metadata.get("cwd")
        if not isinstance(session_cwd, str):
            continue
        try:
            cwd_matches = Path(session_cwd).resolve() == cwd.resolve()
        except OSError:
            cwd_matches = Path(session_cwd) == cwd
        if not cwd_matches:
            continue
        in_window = 0
        for record in records:
            timestamp = _parse_timestamp(record.get("timestamp"))
            if (
                timestamp
                and (start is None or timestamp >= start)
                and (end is None or timestamp <= end)
            ):
                in_window += 1
        if in_window:
            candidates.append((in_window, path, records, metadata))
    if not candidates:
        return None
    _, path, records, metadata = max(candidates, key=lambda item: item[0])
    return path, records, metadata


def _json_argument(text: str, marker: str) -> Any:
    position = text.find(marker)
    if position < 0:
        return None
    try:
        return json.JSONDecoder().raw_decode(text[position + len(marker) :].lstrip())[0]
    except (json.JSONDecodeError, TypeError):
        return None


def _javascript_property(text: str, name: str) -> Any:
    """Read one JSON-compatible value from Codex's JavaScript object wrapper."""
    pattern = re.compile(rf'(?:^|[{{,])\s*"?{re.escape(name)}"?\s*:')
    match = pattern.search(text)
    if match is None:
        return None
    try:
        return json.JSONDecoder().raw_decode(text[match.end() :].lstrip())[0]
    except json.JSONDecodeError:
        return None


def _tool_details(payload: dict) -> dict:
    name = str(payload.get("name", "unknown"))
    raw_input = payload.get("input")
    if name == "exec" and isinstance(raw_input, str):
        arguments = _json_argument(raw_input, "tools.exec_command(")
        if not isinstance(arguments, dict) and "tools.exec_command(" in raw_input:
            arguments = {
                key: _javascript_property(raw_input, key)
                for key in (
                    "cmd",
                    "workdir",
                    "sandbox_permissions",
                    "justification",
                )
            }
        if isinstance(arguments, dict) and arguments.get("cmd") is not None:
            result = {
                "kind": "command",
                "command": arguments.get("cmd", ""),
                "cwd": arguments.get("workdir"),
            }
            if arguments.get("sandbox_permissions") == "require_escalated":
                result["approval"] = {
                    "requested": True,
                    "justification": arguments.get("justification"),
                }
            return result
    if name == "exec" and isinstance(payload.get("arguments"), str):
        try:
            arguments = json.loads(payload["arguments"])
        except json.JSONDecodeError:
            arguments = {}
        if isinstance(arguments, dict) and "cmd" in arguments:
            return {
                "kind": "command",
                "command": arguments.get("cmd", ""),
                "cwd": arguments.get("workdir"),
            }
    if name == "exec" and isinstance(raw_input, dict):
        return {
            "kind": "command",
            "command": raw_input.get("cmd", ""),
            "cwd": raw_input.get("workdir"),
        }
    if name == "exec" and isinstance(raw_input, str):
        patch = _json_argument(raw_input, "const patch =")
        if isinstance(patch, str) and "*** Begin Patch" in patch:
            files = re.findall(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", patch, re.MULTILINE)
            return {"kind": "file_edit", "files": files, "patch": patch}

    arguments: Any = payload.get("arguments", raw_input)
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            pass
    return {"kind": "tool", "arguments": arguments}


def _output_text(payload: dict) -> str:
    output = payload.get("output", "")
    if isinstance(output, str):
        return output[:MAX_RESULT_BYTES]
    if isinstance(output, list):
        pieces = []
        for item in output:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                pieces.append(item["text"])
        return "".join(pieces)[:MAX_RESULT_BYTES]
    return json.dumps(output, sort_keys=True)[:MAX_RESULT_BYTES]


def extract_timeline(
    records: list[dict], *, started_at: str, ended_at: str, session_id: str | None
) -> list[dict]:
    """Convert Codex records into tool-only, reasoning-free timeline events."""
    start = _parse_timestamp(started_at)
    end = _parse_timestamp(ended_at)
    timeline = []
    for record in records:
        timestamp = _parse_timestamp(record.get("timestamp"))
        if timestamp is None or (start and timestamp < start) or (end and timestamp > end):
            continue
        if record.get("type") != "response_item" or not isinstance(record.get("payload"), dict):
            continue
        payload = record["payload"]
        payload_type = payload.get("type")
        if payload_type in {"custom_tool_call", "function_call"}:
            event = {
                "schema_version": TIMELINE_SCHEMA_VERSION,
                "sequence": len(timeline) + 1,
                "timestamp": record["timestamp"],
                "type": "tool_call",
                "agent": "codex",
                "session_id": session_id,
                "call_id": payload.get("call_id", payload.get("id")),
                "tool": payload.get("name", "unknown"),
            }
            event.update(_tool_details(payload))
            timeline.append(event)
        elif payload_type in {"custom_tool_call_output", "function_call_output"}:
            text = _output_text(payload)
            first_line = text.splitlines()[0] if text else "No textual output"
            timeline.append(
                {
                    "schema_version": TIMELINE_SCHEMA_VERSION,
                    "sequence": len(timeline) + 1,
                    "timestamp": record["timestamp"],
                    "type": "tool_result",
                    "agent": "codex",
                    "session_id": session_id,
                    "call_id": payload.get("call_id"),
                    "summary": first_line,
                    "output": text,
                    "output_truncated": len(text) >= MAX_RESULT_BYTES,
                }
            )
    return timeline


def _claude_tool_details(name: str, tool_input: Any, cwd: str | None) -> dict:
    arguments = tool_input if isinstance(tool_input, dict) else {}
    if name == "Bash":
        return {
            "kind": "command",
            "command": arguments.get("command", ""),
            "cwd": cwd,
            "description": arguments.get("description"),
        }
    if name in {"Write", "Edit", "MultiEdit", "NotebookEdit"}:
        path = arguments.get("file_path", arguments.get("notebook_path"))
        return {
            "kind": "file_edit",
            "files": [path] if path else [],
            "arguments": arguments,
        }
    return {"kind": "tool", "arguments": arguments}


def _claude_result_text(content: Any) -> str:
    if isinstance(content, str):
        return content[:MAX_RESULT_BYTES]
    if isinstance(content, list):
        pieces = []
        for item in content:
            if isinstance(item, str):
                pieces.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                pieces.append(item["text"])
        return "".join(pieces)[:MAX_RESULT_BYTES]
    return json.dumps(content, sort_keys=True)[:MAX_RESULT_BYTES]


def extract_claude_timeline(
    records: list[dict], *, started_at: str, ended_at: str, cwd: Path
) -> list[dict]:
    """Convert Claude Code records into reasoning-free timeline events."""
    start = _parse_timestamp(started_at)
    end = _parse_timestamp(ended_at)
    timeline = []
    for record in records:
        timestamp = _parse_timestamp(record.get("timestamp"))
        if timestamp is None or (start and timestamp < start) or (end and timestamp > end):
            continue
        record_cwd = record.get("cwd")
        if isinstance(record_cwd, str):
            try:
                if Path(record_cwd).resolve() != cwd.resolve():
                    continue
            except OSError:
                if Path(record_cwd) != cwd:
                    continue
        message = record.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), list):
            continue
        for content in message["content"]:
            if not isinstance(content, dict):
                continue
            if content.get("type") == "tool_use":
                name = str(content.get("name", "unknown"))
                event = {
                    "schema_version": TIMELINE_SCHEMA_VERSION,
                    "sequence": len(timeline) + 1,
                    "timestamp": record["timestamp"],
                    "type": "tool_call",
                    "agent": "claude",
                    "session_id": record.get("sessionId"),
                    "call_id": content.get("id"),
                    "tool": name,
                }
                event.update(_claude_tool_details(name, content.get("input"), record_cwd))
                timeline.append(event)
            elif content.get("type") == "tool_result":
                text = _claude_result_text(content.get("content", ""))
                summary = text.splitlines()[0] if text else "No textual output"
                timeline.append(
                    {
                        "schema_version": TIMELINE_SCHEMA_VERSION,
                        "sequence": len(timeline) + 1,
                        "timestamp": record["timestamp"],
                        "type": "tool_result",
                        "agent": "claude",
                        "session_id": record.get("sessionId"),
                        "call_id": content.get("tool_use_id"),
                        "summary": summary,
                        "output": text,
                        "is_error": bool(content.get("is_error", False)),
                        "output_truncated": len(text) >= MAX_RESULT_BYTES,
                    }
                )
    timeline.sort(key=lambda event: (event["timestamp"], event["sequence"]))
    for sequence, event in enumerate(timeline, 1):
        event["sequence"] = sequence
    return timeline


def _generic_timeline(path: Path, agent: str) -> list[dict]:
    """Validate provider-neutral events emitted directly by an agent harness."""
    timeline = []
    for record in _read_jsonl(path):
        if record.get("type") not in {"tool_call", "tool_result"}:
            continue
        event = {
            key: value
            for key, value in record.items()
            if key
            in {
                "timestamp",
                "type",
                "call_id",
                "tool",
                "kind",
                "command",
                "cwd",
                "approval",
                "description",
                "files",
                "arguments",
                "summary",
                "output",
                "is_error",
            }
        }
        if not isinstance(event.get("timestamp"), str):
            continue
        event["schema_version"] = TIMELINE_SCHEMA_VERSION
        event["sequence"] = len(timeline) + 1
        event["agent"] = str(record.get("agent") or agent)
        if isinstance(event.get("output"), str):
            output = event["output"][:MAX_RESULT_BYTES]
            event["output"] = output
            event["output_truncated"] = len(output) >= MAX_RESULT_BYTES
        timeline.append(event)
    return timeline


def _markdown(timeline: list[dict], agent: str, session_ids: list[str]) -> str:
    lines = ["# Agent command timeline", "", f"Agent: `{agent}`", ""]
    if session_ids:
        lines.extend(["Session(s): " + ", ".join(f"`{value}`" for value in session_ids), ""])
    call_number = 0
    for event in timeline:
        timestamp = event["timestamp"]
        if event["type"] == "tool_call":
            call_number += 1
            kind = event.get("kind")
            tool = event.get("tool", "unknown")
            lines.extend([f"## {call_number}. {timestamp} — {tool} ({kind or 'tool'})", ""])
            if kind == "command":
                if event.get("cwd"):
                    lines.extend([f"Working directory: `{event['cwd']}`", ""])
                approval = event.get("approval")
                if approval:
                    lines.extend(
                        [f"Approval requested: {approval.get('justification') or 'yes'}", ""]
                    )
                command = str(event.get("command", ""))
                fence = "````" if "```" in command else "```"
                lines.extend([f"{fence}shell", command, fence, ""])
            elif kind == "file_edit":
                files = event.get("files") or []
                lines.append(
                    "Edited: " + (", ".join(f"`{path}`" for path in files) or "workspace files")
                )
                lines.append("")
            else:
                arguments = event.get("arguments")
                if arguments not in (None, "", {}):
                    lines.extend(
                        ["```json", json.dumps(arguments, indent=2, sort_keys=True), "```", ""]
                    )
        else:
            status = "error" if event.get("is_error") else "result"
            lines.extend([f"{status.title()}: {event.get('summary', 'completed')}", ""])
    return "\n".join(lines).rstrip() + "\n"


def _write_timeline_artifacts(bundle_dir: Path, timeline: list[dict]) -> None:
    jsonl_path = bundle_dir / "timeline.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as handle:
        for event in timeline:
            handle.write(json.dumps(event, sort_keys=True) + "\n")
    agents = sorted({str(event.get("agent", "unknown")) for event in timeline})
    session_ids = sorted(
        {str(event["session_id"]) for event in timeline if event.get("session_id")}
    )
    (bundle_dir / "timeline.md").write_text(
        _markdown(timeline, ", ".join(agents), session_ids), encoding="utf-8"
    )


def write_codex_timeline(
    *,
    bundle_dir: Path,
    cwd: Path,
    environment: dict[str, str],
    sessions_before: dict[Path, tuple[int, int]],
    started_at: str,
    ended_at: str,
) -> dict:
    """Discover the run's Codex session and write JSONL/Markdown artifacts."""
    selected = _select_session(
        changed_sessions(environment, sessions_before), cwd, started_at, ended_at
    )
    if selected is None:
        return {"captured": False, "reason": "no matching changed Codex session found"}
    source, records, metadata = selected
    session_id = metadata.get("session_id", metadata.get("id"))
    timeline = extract_timeline(
        records, started_at=started_at, ended_at=ended_at, session_id=session_id
    )
    if not timeline:
        return {"captured": False, "reason": "matching Codex session had no tool events"}

    _write_timeline_artifacts(bundle_dir, timeline)
    return {
        "captured": True,
        "adapter": "codex-local-session",
        "session_id": session_id,
        "source": str(source),
        "events": len(timeline),
        "tool_calls": sum(event["type"] == "tool_call" for event in timeline),
        "jsonl": "timeline.jsonl",
        "markdown": "timeline.md",
    }


def write_claude_timeline(
    *,
    bundle_dir: Path,
    cwd: Path,
    environment: dict[str, str],
    sessions_before: dict[Path, tuple[int, int]],
    started_at: str,
    ended_at: str,
) -> dict:
    paths = changed_claude_sessions(environment, sessions_before)
    records = [record for path in paths for record in _read_jsonl(path)]
    timeline = extract_claude_timeline(records, started_at=started_at, ended_at=ended_at, cwd=cwd)
    if not timeline:
        return {"captured": False, "reason": "no matching Claude tool events found"}
    _write_timeline_artifacts(bundle_dir, timeline)
    session_ids = sorted(
        {str(event["session_id"]) for event in timeline if event.get("session_id")}
    )
    return {
        "captured": True,
        "adapter": "claude-local-session",
        "session_ids": session_ids,
        "sources": [str(path) for path in paths],
        "events": len(timeline),
        "tool_calls": sum(event["type"] == "tool_call" for event in timeline),
        "jsonl": "timeline.jsonl",
        "markdown": "timeline.md",
    }


def write_agent_timeline(
    *,
    command: list[str],
    bundle_dir: Path,
    cwd: Path,
    environment: dict[str, str],
    source_snapshot: dict,
    started_at: str,
    ended_at: str,
) -> dict:
    """Write the best structured timeline available for any responder."""
    generic_path = Path(
        environment.get("WHYSLOW_BENCH_TIMELINE_PATH", bundle_dir / GENERIC_EVENTS_FILENAME)
    )
    generic = _generic_timeline(generic_path, Path(command[0]).name if command else "unknown")
    if generic:
        _write_timeline_artifacts(bundle_dir, generic)
        return {
            "captured": True,
            "adapter": "generic-jsonl",
            "events": len(generic),
            "tool_calls": sum(event["type"] == "tool_call" for event in generic),
            "source": GENERIC_EVENTS_FILENAME,
            "jsonl": "timeline.jsonl",
            "markdown": "timeline.md",
        }

    provider = source_snapshot.get("provider")
    sessions = source_snapshot.get("sessions", {})
    if provider == "codex":
        return write_codex_timeline(
            bundle_dir=bundle_dir,
            cwd=cwd,
            environment=environment,
            sessions_before=sessions,
            started_at=started_at,
            ended_at=ended_at,
        )
    if provider == "claude":
        return write_claude_timeline(
            bundle_dir=bundle_dir,
            cwd=cwd,
            environment=environment,
            sessions_before=sessions,
            started_at=started_at,
            ended_at=ended_at,
        )
    return {
        "captured": False,
        "reason": (
            "agent emitted no generic timeline events and has no built-in "
            "structured-session adapter"
        ),
    }
