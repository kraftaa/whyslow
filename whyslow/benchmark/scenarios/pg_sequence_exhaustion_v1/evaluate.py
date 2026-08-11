"""State-based evaluator for the exhausted-sequence scenario."""

from __future__ import annotations

import time

import psycopg2

from ... import common
from .setup import integrity_snapshot

WEIGHTS = {"write_recovery": 50, "sequence_capacity": 20, "data_integrity": 25, "report": 5}


def _check_write(config: common.DsnConfig) -> tuple[bool, str]:
    conn = common.connect(config.admin_dsn("whyslow_sequence_probe"), autocommit=False)
    try:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = '3000ms'")
            cur.execute(
                "INSERT INTO invoices (customer_id, amount, status) "
                "VALUES (999, 1.00, 'probe') RETURNING id"
            )
            new_id = int(cur.fetchone()[0])
        conn.rollback()
        return True, f"representative invoice insert succeeded with generated id {new_id}"
    except psycopg2.Error as exc:
        conn.rollback()
        return False, f"representative invoice insert failed: {exc}".strip()
    finally:
        conn.close()


def _check_capacity(conn) -> tuple[bool, str]:
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT max_value::numeric, last_value::numeric "
                "FROM pg_sequences WHERE schemaname = 'public' "
                "AND sequencename = 'invoice_id_seq'"
            )
            row = cur.fetchone()
    except psycopg2.Error as exc:
        return False, f"invoice sequence unavailable: {exc}".strip()
    if row is None:
        return False, "invoice_id_seq is missing"
    max_value, last_value = int(row[0]), int(row[1])
    ok = max_value >= 1_000_000 and last_value >= 5
    detail = (
        f"sequence capacity restored (last={last_value}, max={max_value})"
        if ok
        else f"sequence remains unsafe (last={last_value}, max={max_value})"
    )
    return ok, detail


def _check_integrity(conn, baseline: dict) -> tuple[bool, str]:
    try:
        current = integrity_snapshot(conn)
    except psycopg2.Error as exc:
        return False, f"invoices unavailable: {exc}".strip()
    if current != baseline:
        return False, f"existing invoices changed: expected {baseline}, got {current}"
    return True, "all existing invoice rows and identifiers match the baseline"


def _check_report(ctx: common.Context) -> tuple[bool, str]:
    report = ctx.workspace_dir / "result.md"
    ok = report.is_file() and bool(report.read_text().strip())
    return ok, "result.md present and non-empty" if ok else "result.md missing or empty"


def evaluate(ctx: common.Context) -> dict:
    if not ctx.ground_truth_path.exists():
        raise RuntimeError(f"no ground truth for {ctx.scenario_id}; run `setup` first")
    truth = common.read_json(ctx.ground_truth_path)
    started = time.monotonic()
    conn = common.connect(ctx.config.admin_dsn("whyslow_sequence_eval"))
    try:
        write_ok, write_detail = _check_write(ctx.config)
        capacity_ok, capacity_detail = _check_capacity(conn)
        integrity_ok, integrity_detail = _check_integrity(conn, truth["baseline_integrity"])
    finally:
        conn.close()
    report_ok, report_detail = _check_report(ctx)
    values = {
        "write_recovery": (write_ok, write_detail),
        "sequence_capacity": (capacity_ok, capacity_detail),
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
        "observability": {"evaluate_duration_seconds": round(time.monotonic() - started, 3)},
        "evaluated_at": time.time(),
    }
