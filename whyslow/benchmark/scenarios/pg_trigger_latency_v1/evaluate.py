"""State-based evaluator for trigger-induced insert latency."""

from __future__ import annotations

import time

import psycopg2

from ... import common
from .setup import integrity_snapshot

WEIGHTS = {"write_recovery": 50, "audit_preserved": 20, "data_integrity": 25, "report": 5}


def _probe_write(config: common.DsnConfig) -> tuple[bool, str, bool, float]:
    conn = common.connect(config.admin_dsn("whyslow_trigger_probe"), autocommit=False)
    started = time.monotonic()
    try:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = '400ms'")
            cur.execute(
                "INSERT INTO trigger_orders (customer_id, amount) VALUES (999, 1.00) RETURNING id"
            )
            order_id = int(cur.fetchone()[0])
            cur.execute(
                "SELECT count(*) FROM order_audit WHERE order_id = %s AND action = 'created'",
                (order_id,),
            )
            audit_ok = int(cur.fetchone()[0]) == 1
        elapsed = time.monotonic() - started
        conn.rollback()
        return True, f"representative order insert completed in {elapsed:.3f}s", audit_ok, elapsed
    except psycopg2.Error as exc:
        elapsed = time.monotonic() - started
        conn.rollback()
        return (
            False,
            f"representative order insert failed after {elapsed:.3f}s: {exc}".strip(),
            False,
            elapsed,
        )
    finally:
        conn.close()


def _audit_trigger_enabled(conn) -> tuple[bool, str]:
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT t.tgenabled FROM pg_trigger t "
                "WHERE t.tgrelid = to_regclass('public.trigger_orders') "
                "AND t.tgname = 'order_audit_required' AND NOT t.tgisinternal"
            )
            row = cur.fetchone()
    except psycopg2.Error as exc:
        return False, f"required audit trigger unavailable: {exc}".strip()
    ok = row is not None and row[0] in {"O", "A"}
    return (
        ok,
        "required order audit trigger remains enabled"
        if ok
        else "required order audit trigger is missing or disabled",
    )


def _check_integrity(conn, baseline: dict) -> tuple[bool, str]:
    try:
        current = integrity_snapshot(conn)
    except psycopg2.Error as exc:
        return False, f"order data unavailable: {exc}".strip()
    if current != baseline:
        return False, f"order or audit history changed: expected {baseline}, got {current}"
    return True, "existing orders and audit history match the baseline"


def _check_report(ctx: common.Context) -> tuple[bool, str]:
    report = ctx.workspace_dir / "result.md"
    ok = report.is_file() and bool(report.read_text().strip())
    return ok, "result.md present and non-empty" if ok else "result.md missing or empty"


def evaluate(ctx: common.Context) -> dict:
    if not ctx.ground_truth_path.exists():
        raise RuntimeError(f"no ground truth for {ctx.scenario_id}; run `setup` first")
    truth = common.read_json(ctx.ground_truth_path)
    started = time.monotonic()
    write_ok, write_detail, audit_probe_ok, elapsed = _probe_write(ctx.config)
    conn = common.connect(ctx.config.admin_dsn("whyslow_trigger_eval"))
    try:
        audit_trigger_ok, audit_trigger_detail = _audit_trigger_enabled(conn)
        integrity_ok, integrity_detail = _check_integrity(conn, truth["baseline_integrity"])
    finally:
        conn.close()
    audit_ok = audit_probe_ok and audit_trigger_ok
    audit_detail = (
        "required audit trigger executed for the fast probe insert"
        if audit_ok
        else audit_trigger_detail
        if not audit_trigger_ok
        else "probe insert did not produce its required audit record"
    )
    report_ok, report_detail = _check_report(ctx)
    values = {
        "write_recovery": (write_ok and elapsed < 0.4, write_detail),
        "audit_preserved": (audit_ok, audit_detail),
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
            "probe_insert_seconds": round(elapsed, 3),
            "evaluate_duration_seconds": round(time.monotonic() - started, 3),
        },
        "evaluated_at": time.time(),
    }
