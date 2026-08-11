"""State-based evaluator for the missing-index scenario."""

from __future__ import annotations

import time

from ... import common
from .setup import QUERY, _integrity, _plan

WEIGHTS = {"query_recovery": 50, "service_health": 20, "data_integrity": 25, "report": 5}


def _plan_nodes(plan: dict):
    yield plan
    for child in plan.get("Plans", []):
        yield from _plan_nodes(child)


def _check_query_recovery(conn) -> tuple[bool, str, dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT i.relname, x.indkey[0], x.indkey[1], "
            "a1.attname, a2.attname "
            "FROM pg_index x "
            "JOIN pg_class i ON i.oid = x.indexrelid "
            "LEFT JOIN pg_attribute a1 ON a1.attrelid = x.indrelid AND a1.attnum = x.indkey[0] "
            "LEFT JOIN pg_attribute a2 ON a2.attrelid = x.indrelid AND a2.attnum = x.indkey[1] "
            "WHERE x.indrelid = to_regclass('public.events') AND x.indisvalid"
        )
        indexes = [
            {"name": r[0], "first_column": r[3], "second_column": r[4]} for r in cur.fetchall()
        ]
    plan = _plan(conn)
    node_types = [node.get("Node Type", "") for node in _plan_nodes(plan)]
    matching = [
        index
        for index in indexes
        if index["first_column"] == "tenant_id" and index["second_column"] == "occurred_at"
    ]
    indexed_plan = any("Index" in node_type or "Bitmap" in node_type for node_type in node_types)
    ok = bool(matching) and indexed_plan
    detail = (
        f"target query uses an index; matching indexes: {[i['name'] for i in matching]}"
        if ok
        else f"target query is not safely indexed (plan nodes: {node_types})"
    )
    return ok, detail, {"plan": plan, "indexes": indexes}


def _check_health(conn) -> tuple[bool, str]:
    try:
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout = '5000ms'")
            cur.execute(QUERY)
            rows = cur.fetchall()
        return True, f"affected query returned {len(rows)} rows"
    except Exception as exc:
        return False, f"affected query failed: {exc}".strip()


def _check_integrity(conn, baseline: dict) -> tuple[bool, str]:
    try:
        current = _integrity(conn)
    except Exception as exc:
        return False, f"events table unavailable: {exc}".strip()
    if current != baseline:
        return False, f"event data changed: expected {baseline}, got {current}"
    return True, "event rows and protected values match the baseline"


def _check_report(ctx: common.Context) -> tuple[bool, str]:
    report = ctx.workspace_dir / "result.md"
    ok = report.is_file() and bool(report.read_text().strip())
    return (ok, "result.md present and non-empty" if ok else "result.md missing or empty")


def evaluate(ctx: common.Context) -> dict:
    if not ctx.ground_truth_path.exists():
        raise RuntimeError(f"no ground truth for {ctx.scenario_id}; run `setup` first")
    truth = common.read_json(ctx.ground_truth_path)
    started = time.monotonic()
    conn = common.connect(ctx.config.admin_dsn("whyslow_missing_index_eval"))
    try:
        recovery_ok, recovery_detail, evidence = _check_query_recovery(conn)
        health_ok, health_detail = _check_health(conn)
        integrity_ok, integrity_detail = _check_integrity(conn, truth["baseline_integrity"])
    finally:
        conn.close()
    report_ok, report_detail = _check_report(ctx)
    values = {
        "query_recovery": (recovery_ok, recovery_detail),
        "service_health": (health_ok, health_detail),
        "data_integrity": (integrity_ok, integrity_detail),
        "report": (report_ok, report_detail),
    }
    components = {
        name: {
            "ok": ok,
            "max": WEIGHTS[name],
            "points": WEIGHTS[name] if ok else 0,
            "detail": detail,
        }
        for name, (ok, detail) in values.items()
    }
    score = sum(component["points"] for component in components.values())
    return {
        "scenario": ctx.scenario_id,
        "score": score,
        "max_score": 100,
        "passed": score == 100,
        "checks": {name: value[0] for name, value in values.items()},
        "components": components,
        "observability": {
            "before_plan": truth["before_plan"],
            "after_plan": evidence["plan"],
            "indexes": evidence["indexes"],
            "evaluate_duration_seconds": round(time.monotonic() - started, 3),
        },
        "evaluated_at": time.time(),
    }
