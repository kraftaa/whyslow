"""Extract observable Codex tool activity into a portable trajectory timeline.

Codex keeps a structured local JSONL session in ``$CODEX_HOME/sessions``.  This
adapter reads only tool calls and their observable outputs.  It deliberately
ignores messages and reasoning records.
"""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import re
from typing import Any


TIMELINE_SCHEMA_VERSION = "whyslow-command-timeline/1"
MAX_RESULT_BYTES = 64 * 1024


def is_codex_command(command: list[str]) -> bool:
    """Return whether the responder command directly launches Codex."""
    return bool(command) and Path(command[0]).name.lower() in {"codex", "codex.exe"}


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


def _markdown(timeline: list[dict], session_id: str | None) -> str:
    lines = ["# Agent command timeline", "", f"Codex session: `{session_id or 'unknown'}`", ""]
    call_number = 0
    for event in timeline:
        timestamp = event["timestamp"]
        if event["type"] == "tool_call":
            call_number += 1
            kind = event.get("kind")
            tool = event.get("tool", "unknown")
            lines.extend([f"## {call_number}. {timestamp} — {kind or tool}", ""])
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
            lines.extend([f"Result: {event.get('summary', 'completed')}", ""])
    return "\n".join(lines).rstrip() + "\n"


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

    jsonl_path = bundle_dir / "timeline.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as handle:
        for event in timeline:
            handle.write(json.dumps(event, sort_keys=True) + "\n")
    (bundle_dir / "timeline.md").write_text(_markdown(timeline, session_id), encoding="utf-8")
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
