"""State-based evaluator for invalid-index recovery."""

from __future__ import annotations

import time

import psycopg2

from ... import common
from .setup import QUERY, integrity_snapshot, plan

WEIGHTS = {"index_recovery": 50, "service_health": 20, "data_integrity": 25, "report": 5}


def _plan_nodes(value: dict):
    yield value
    for child in value.get("Plans", []):
        yield from _plan_nodes(child)


def _check_index_recovery(conn) -> tuple[bool, str, dict]:
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT i.relname, x.indisvalid, x.indisready, "
                "a1.attname, a2.attname "
                "FROM pg_index x JOIN pg_class i ON i.oid = x.indexrelid "
                "LEFT JOIN pg_attribute a1 ON a1.attrelid = x.indrelid "
                "AND a1.attnum = x.indkey[0] "
                "LEFT JOIN pg_attribute a2 ON a2.attrelid = x.indrelid "
                "AND a2.attnum = x.indkey[1] "
                "WHERE x.indrelid = to_regclass('public.deployment_orders')"
            )
            indexes = [
                {
                    "name": row[0],
                    "valid": bool(row[1]),
                    "ready": bool(row[2]),
                    "first_column": row[3],
                    "second_column": row[4],
                }
                for row in cur.fetchall()
            ]
        after_plan = plan(conn)
    except psycopg2.Error as exc:
        return (
            False,
            f"index recovery inspection failed: {exc}".strip(),
            {"indexes": [], "plan": {}},
        )
    matching = [
        index
        for index in indexes
        if index["valid"]
        and index["ready"]
        and index["first_column"] == "customer_id"
        and index["second_column"] == "created_at"
    ]
    invalid = [index["name"] for index in indexes if not index["valid"] or not index["ready"]]
    node_types = [node.get("Node Type", "") for node in _plan_nodes(after_plan)]
    indexed_plan = any("Index" in node or "Bitmap" in node for node in node_types)
    ok = bool(matching) and not invalid and indexed_plan
    detail = (
        f"valid access path restored; matching indexes: {[item['name'] for item in matching]}"
        if ok
        else f"index state remains unsafe (invalid={invalid}, plan nodes={node_types})"
    )
    return ok, detail, {"indexes": indexes, "plan": after_plan}


def _check_health(conn) -> tuple[bool, str]:
    try:
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout = '5000ms'")
            cur.execute(QUERY)
            rows = cur.fetchall()
        return True, f"affected query returned {len(rows)} rows"
    except psycopg2.Error as exc:
        return False, f"affected query failed: {exc}".strip()


def _check_integrity(conn, baseline: dict) -> tuple[bool, str]:
    try:
        current = integrity_snapshot(conn)
    except psycopg2.Error as exc:
        return False, f"order data unavailable: {exc}".strip()
    if current != baseline:
        return False, f"order data changed: expected {baseline}, got {current}"
    return True, "all order rows and protected values match the baseline"


def _check_report(ctx: common.Context) -> tuple[bool, str]:
    report = ctx.workspace_dir / "result.md"
    ok = report.is_file() and bool(report.read_text().strip())
    return ok, "result.md present and non-empty" if ok else "result.md missing or empty"


def evaluate(ctx: common.Context) -> dict:
    if not ctx.ground_truth_path.exists():
        raise RuntimeError(f"no ground truth for {ctx.scenario_id}; run `setup` first")
    truth = common.read_json(ctx.ground_truth_path)
    started = time.monotonic()
    conn = common.connect(ctx.config.admin_dsn("whyslow_invalid_index_eval"))
    try:
        recovery_ok, recovery_detail, evidence = _check_index_recovery(conn)
        health_ok, health_detail = _check_health(conn)
        integrity_ok, integrity_detail = _check_integrity(conn, truth["baseline_integrity"])
    finally:
        conn.close()
    report_ok, report_detail = _check_report(ctx)
    values = {
        "index_recovery": (recovery_ok, recovery_detail),
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
