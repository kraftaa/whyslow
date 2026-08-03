"""Stable, machine-readable command output.

The human renderers are intentionally prose-first and may evolve for
readability. These documents are versioned so automation never has to parse
that prose or guess when a field changes shape.
"""

import json


OUTPUT_SCHEMA_VERSION = 1


def _ready(value):
    """Convert internal tuples/sets into deterministic JSON values."""
    if isinstance(value, dict):
        return {key: _ready(item) for key, item in value.items()}
    if isinstance(value, set):
        return [_ready(item) for item in sorted(value)]
    if isinstance(value, (list, tuple)):
        return [_ready(item) for item in value]
    return value


def explain_document(result, start_ts, end_ts):
    timeline = [
        {"ts": ts, "message": message, "evidence": _ready(evidence)}
        for ts, message, evidence in result["timeline"]
    ]
    return {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "type": "explain",
        "window": {"start_ts": start_ts, "end_ts": end_ts},
        "coverage": _ready(result["coverage"]),
        "contributors": _ready(result["contributors"]),
        "timeline": timeline,
    }


def diff_document(result, baseline_start, baseline_end, incident_start, incident_end):
    return {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "type": "diff",
        "windows": {
            "baseline": {"start_ts": baseline_start, "end_ts": baseline_end},
            "incident": {"start_ts": incident_start, "end_ts": incident_end},
        },
        "baseline": _ready(result["baseline"]),
        "incident": _ready(result["incident"]),
    }


def status_document(result, ok):
    return {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "type": "status",
        "ok": ok,
        "observed_at": result["now"],
        "collectors": _ready(result["collectors"]),
        "retired_collectors": _ready(result["retired_collectors"]),
        "coverage": _ready(result["coverage"]),
    }


def dumps(document):
    return json.dumps(document, indent=2, sort_keys=True, allow_nan=False)
