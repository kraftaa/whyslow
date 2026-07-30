"""
whyslow status -- is this thing actually collecting?

The single most important question for a tool that can only explain
incidents from the moment it started running. Because collectors write
diffs, zero rows is ambiguous (quiet database vs. dead collector), so
liveness comes from heartbeats, not from row counts.
"""

import time

# A collector is considered stale if its heartbeat is older than this
# multiple of its expected interval. Generous, to avoid false alarms from
# a slow poll or a brief reconnect.
STALE_MULTIPLIER = 10
DEFAULT_EXPECTED_INTERVAL = 60  # conservative fallback if unknown


def _fmt_age(seconds):
    if seconds < 60:
        return f"{seconds:.0f}s ago"
    if seconds < 3600:
        return f"{seconds/60:.0f}m ago"
    if seconds < 86400:
        return f"{seconds/3600:.1f}h ago"
    return f"{seconds/86400:.1f}d ago"


def _fmt_ts(ts):
    if ts is None:
        return "never"
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def status(store, now=None):
    now = now or time.time()
    heartbeats = store.get_heartbeats()
    coverage = store.data_coverage()

    collectors = []
    for collector, ts, detail, instance_role in heartbeats:
        age = now - ts
        # Postgres/Puma default to 1s, CloudWatch to 60s -- use the
        # conservative fallback rather than guessing per collector.
        expected = 1 if (collector.startswith("puma") or collector == "postgres") else DEFAULT_EXPECTED_INTERVAL
        stale = age > (expected * STALE_MULTIPLIER)
        collectors.append({
            "name": collector,
            "last_heartbeat_ts": ts,
            "age_seconds": age,
            "stale": stale,
            "detail": detail,
            "instance_role": instance_role,
        })

    return {"collectors": collectors, "coverage": coverage, "now": now}


def render(result):
    lines = []
    now = result["now"]

    lines.append("Collectors")
    if not result["collectors"]:
        lines.append("  ✗ NO COLLECTORS HAVE EVER RUN against this database.")
        lines.append("    Nothing is being recorded. `whyslow` will find nothing.")
        lines.append("    Start one: whyslow collect-pg --dsn ...")
    for c in result["collectors"]:
        mark = "✗" if c["stale"] else "✓"
        state = "STALE" if c["stale"] else "alive"
        role = c.get("instance_role")
        role_note = ""
        if role == "replica":
            mark = "✗"
            role_note = "  [REPLICA -- cannot see write-lock contention!]"
        elif role == "primary":
            role_note = "  [primary]"
        lines.append(
            f"  {mark} {c['name']:<24} {state:<6} last heartbeat {_fmt_age(c['age_seconds'])}"
            + (f"  ({c['detail']})" if c["detail"] else "")
            + role_note
        )

    if any(c.get("instance_role") == "replica" for c in result["collectors"]):
        lines.append("")
        lines.append("  WARNING: a collector on a replica / Aurora reader endpoint will")
        lines.append("  never see write-lock contention. Repoint it at the WRITER endpoint.")
    if any(c["stale"] for c in result["collectors"]):
        lines.append("")
        lines.append("  WARNING: a stale collector means incidents during that gap")
        lines.append("  cannot be explained. Check the process is still running.")

    lines.append("")
    lines.append("Data coverage")
    any_data = False
    for table, cov in result["coverage"].items():
        if cov["count"] == 0:
            lines.append(f"  {table:<22} (empty)")
            continue
        any_data = True
        span_hours = (cov["max_ts"] - cov["min_ts"]) / 3600
        lines.append(
            f"  {table:<22} {cov['count']:>7} rows   "
            f"{_fmt_ts(cov['min_ts'])} -> {_fmt_ts(cov['max_ts'])}  ({span_hours:.1f}h)"
        )

    live_collectors = [c for c in result["collectors"] if not c["stale"]]
    if not any_data and live_collectors:
        lines.append("")
        lines.append("  Note: empty tables with a live collector is NORMAL -- collectors")
        lines.append("  write diffs, so a quiet, healthy database produces no rows.")
        lines.append("  The heartbeat above is what confirms collection is working.")
    elif not any_data and result["collectors"]:
        lines.append("")
        lines.append("  Empty tables AND no live collector: nothing is being recorded.")

    return "\n".join(lines)
