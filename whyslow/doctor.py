"""Deployment and runtime readiness checks for whyslow."""

import json
import os
import shutil
import stat
import time
import urllib.request
from datetime import datetime, timedelta, timezone

from . import status as status_mod


MIN_FREE_BYTES = 100 * 1024 * 1024
REQUIRED_SCHEMA = {
    "session_changes": {"ts", "pid", "category", "query"},
    "blocking_edges": {"ts", "blocked_pid", "blocking_pid", "blocking_query", "ended_ts"},
    "puma_stats": {"ts", "host", "backlog", "pool_capacity"},
    "cloudwatch_metrics": {"ts", "metric", "value"},
    "events": {"ts", "source", "kind", "payload"},
    "collector_heartbeats": {"collector", "ts", "expected_interval", "instance_role"},
    "collector_coverage": {"collector", "minute_bucket"},
    "collector_memberships": {"collector", "started_minute", "retired_minute"},
}


def _check(name, state, detail):
    return {"name": name, "state": state, "detail": detail}


def doctor(store, *, dsn=None, puma_url=None, puma_token=None,
           db_instance_id=None, region=None, now=None):
    now = now or time.time()
    checks = []
    checks.extend(_check_store(store))
    checks.extend(_check_collectors(store, now))
    checks.append(_check_postgres(dsn))
    checks.append(_check_puma(puma_url, puma_token))
    checks.append(_check_cloudwatch(db_instance_id, region))
    return {
        "ok": not any(check["state"] == "fail" for check in checks),
        "checks": checks,
        "checked_at": now,
    }


def _check_store(store):
    checks = []
    quick_check = store.conn.execute("PRAGMA quick_check").fetchone()[0]
    checks.append(_check(
        "sqlite_integrity",
        "pass" if quick_check == "ok" else "fail",
        "quick_check=ok" if quick_check == "ok" else f"quick_check={quick_check}",
    ))

    journal_mode = store.conn.execute("PRAGMA journal_mode").fetchone()[0].lower()
    checks.append(_check(
        "sqlite_wal",
        "pass" if journal_mode == "wal" else "fail",
        f"journal_mode={journal_mode}",
    ))

    missing = []
    for table, required_columns in REQUIRED_SCHEMA.items():
        columns = {
            row[1]
            for row in store.conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        absent = sorted(required_columns - columns)
        if absent:
            missing.append(f"{table}({', '.join(absent)})")
    checks.append(_check(
        "schema",
        "fail" if missing else "pass",
        f"missing: {'; '.join(missing)}" if missing else "required tables and columns present",
    ))

    if str(store.path) == ":memory:":
        checks.append(_check("store_permissions", "warn", "in-memory database"))
        disk_path = os.getcwd()
    else:
        mode = stat.S_IMODE(store.path.stat().st_mode)
        original_mode = store.original_mode
        unsafe_original = original_mode is not None and original_mode != 0o600
        permissions_ok = mode == 0o600 and not unsafe_original
        detail = f"mode={oct(mode)}; expected 0o600"
        if unsafe_original:
            detail = (
                f"mode was {oct(original_mode)} before open; corrected to {oct(mode)}"
            )
        checks.append(_check(
            "store_permissions",
            "pass" if permissions_ok else "fail",
            detail,
        ))
        disk_path = store.path.parent

    free = shutil.disk_usage(disk_path).free
    checks.append(_check(
        "disk_space",
        "pass" if free >= MIN_FREE_BYTES else "fail",
        f"{free / (1024 * 1024):.0f} MiB free; minimum {MIN_FREE_BYTES // (1024 * 1024)} MiB",
    ))
    return checks


def _check_collectors(store, now):
    result = status_mod.status(store, now=now)
    by_name = {collector["name"]: collector for collector in result["collectors"]}
    checks = []
    postgres = by_name.get("postgres")
    if postgres is None:
        checks.append(_check("postgres_collector", "fail", "not active"))
    elif postgres["stale"]:
        checks.append(_check(
            "postgres_collector", "fail",
            f"stale; last heartbeat {postgres['age_seconds']:.0f}s ago",
        ))
    elif postgres.get("instance_role") == "replica":
        checks.append(_check("postgres_collector", "fail", "connected to a replica"))
    else:
        checks.append(_check(
            "postgres_collector", "pass",
            f"alive; last heartbeat {postgres['age_seconds']:.0f}s ago",
        ))

    for role, matcher in (
        ("puma_collectors", lambda name: name.startswith("puma:")),
        ("cloudwatch_collector", lambda name: name == "cloudwatch"),
    ):
        matching = [collector for name, collector in by_name.items() if matcher(name)]
        if not matching:
            checks.append(_check(role, "warn", "not configured"))
        elif any(collector["stale"] for collector in matching):
            stale = ", ".join(c["name"] for c in matching if c["stale"])
            checks.append(_check(role, "fail", f"stale: {stale}"))
        else:
            checks.append(_check(role, "pass", f"{len(matching)} active collector(s)"))
    return checks


def _check_postgres(dsn):
    if not dsn:
        return _check("postgres_connection", "warn", "not checked; set WHYSLOW_PG_DSN")
    try:
        import psycopg2

        conn = psycopg2.connect(dsn, connect_timeout=5)
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_is_in_recovery(), current_database(), "
                    "(SELECT rolsuper FROM pg_roles WHERE rolname = current_user) "
                    "OR pg_has_role(current_user, 'pg_read_all_stats', 'member')"
                )
                in_recovery, database, has_stats_visibility = cur.fetchone()
                cur.execute("SELECT count(*) FROM pg_stat_activity")
                cur.fetchone()
        finally:
            conn.close()
        if in_recovery:
            return _check(
                "postgres_connection", "fail",
                f"database={database}; connected to a replica/reader",
            )
        if not has_stats_visibility:
            return _check(
                "postgres_connection", "fail",
                f"database={database}; writer reachable but pg_read_all_stats is missing",
            )
        return _check(
            "postgres_connection", "pass",
            f"database={database}; writer reachable with stats visibility",
        )
    except Exception as exc:
        return _check("postgres_connection", "fail", _safe_error(exc))


def _check_puma(url, token):
    if not url:
        return _check("puma_endpoint", "warn", "not checked; set WHYSLOW_PUMA_STATS_URL")
    try:
        request = urllib.request.Request(url)
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode())
        valid = (
            isinstance(payload, dict)
            and (
                "worker_status" in payload
                or {"running", "max_threads", "pool_capacity", "backlog"} <= set(payload)
            )
        )
        if not valid:
            return _check("puma_endpoint", "fail", "unexpected /stats response shape")
        return _check("puma_endpoint", "pass", "reachable; valid /stats response")
    except Exception as exc:
        return _check("puma_endpoint", "fail", _safe_error(exc))


def _check_cloudwatch(db_instance_id, region):
    if not db_instance_id:
        return _check("cloudwatch_api", "warn", "not checked; set WHYSLOW_DB_INSTANCE_ID")
    try:
        import boto3

        client = boto3.client("cloudwatch", region_name=region)
        end = datetime.now(timezone.utc) - timedelta(minutes=2)
        response = client.get_metric_statistics(
            Namespace="AWS/RDS",
            MetricName="CPUUtilization",
            Dimensions=[{"Name": "DBInstanceIdentifier", "Value": db_instance_id}],
            StartTime=end - timedelta(minutes=10),
            EndTime=end,
            Period=60,
            Statistics=["Average"],
        )
        count = len(response.get("Datapoints", []))
        return _check(
            "cloudwatch_api",
            "pass" if count else "fail",
            f"API reachable; {count} CPU datapoint(s)" if count
            else "API reachable but no recent CPU datapoints; verify the DB instance identifier",
        )
    except ImportError:
        return _check("cloudwatch_api", "fail", "install whyslow[cloudwatch]")
    except Exception as exc:
        return _check("cloudwatch_api", "fail", _safe_error(exc))


def _safe_error(exc):
    # Exception strings can echo URLs or DSNs. Never copy them into doctor
    # output, which is likely to be pasted into an incident ticket.
    return f"{type(exc).__name__}: check configuration and service reachability"


def render(result):
    marks = {"pass": "✓", "warn": "!", "fail": "✗"}
    lines = ["whyslow doctor", ""]
    for check in result["checks"]:
        lines.append(
            f"  {marks[check['state']]} {check['name']:<24} "
            f"{check['state'].upper():<4}  {check['detail']}"
        )
    lines.extend(["", "Result: READY" if result["ok"] else "Result: NOT READY"])
    return "\n".join(lines)
