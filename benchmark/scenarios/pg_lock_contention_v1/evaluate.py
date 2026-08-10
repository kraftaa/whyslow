"""Deterministic system-state evaluator for pg_lock_contention_v1.

Scores what the system looks like *after* the agent acted, not which commands
the agent used. A valid agent may remediate however it likes.

    recovery                 40
    service_health           20
    data_integrity           20
    collateral_damage        15
    incident_report_present   5
                             ---
                             100
"""

from __future__ import annotations

import time

import psycopg2

from ... import common

WEIGHTS = {
    "recovery": 40,
    "service_health": 20,
    "data_integrity": 20,
    "collateral_damage": 15,
    "incident_report_present": 5,
}


def _check_recovery(config: common.DsnConfig, ground_truth: dict) -> tuple[bool, str]:
    """No session is still blocked, and a fresh contended write completes."""
    admin = common.connect(config.admin_dsn("whyslow_bench_eval"))
    try:
        with admin.cursor() as cur:
            cur.execute(
                """
                SELECT count(*)
                FROM pg_stat_activity
                WHERE wait_event_type = 'Lock'
                  AND application_name = ANY(%s)
                  AND datname = %s
                """,
                (list(common.SCENARIO_APPS), common.DEFAULT_DB),
            )
            still_blocked = int(cur.fetchone()[0])
        if still_blocked:
            return False, f"{still_blocked} scenario session(s) still blocked on a lock"

        # Probe the contended write path directly: if the holder is still there,
        # this waits and hits lock_timeout. Rolled back so we never mutate.
        probe = common.connect(config.admin_dsn("whyslow_bench_eval_probe"), autocommit=False)
        try:
            with probe.cursor() as cur:
                cur.execute("SET lock_timeout = '3000ms'")
                cur.execute(
                    "UPDATE accounts SET touch_count = touch_count WHERE id = %s",
                    (ground_truth["contended_row_id"],),
                )
            probe.rollback()
        except psycopg2.errors.LockNotAvailable:
            probe.rollback()
            return False, "contended row is still locked (probe hit lock_timeout)"
        finally:
            probe.close()
        return True, "no blocked sessions; contended row is writable again"
    finally:
        admin.close()


def _check_service_health(config: common.DsnConfig) -> tuple[bool, str]:
    """A representative application read completes within a short timeout."""
    conn = common.connect(config.admin_dsn("whyslow_bench_health"), autocommit=False)
    try:
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout = '5000ms'")
            cur.execute("SELECT count(*), coalesce(sum(balance), 0)::text FROM accounts")
            row = cur.fetchone()
        conn.rollback()
        return True, f"representative query returned {row[0]} rows"
    except psycopg2.Error as exc:
        conn.rollback()
        return False, f"representative query failed/timed out: {exc}".strip()
    finally:
        conn.close()


def _check_data_integrity(config: common.DsnConfig, ground_truth: dict) -> tuple[bool, str]:
    conn = common.connect(config.admin_dsn("whyslow_bench_integrity"))
    try:
        now = common.integrity_snapshot(conn)
    finally:
        conn.close()
    baseline = ground_truth["baseline_integrity"]
    # touch_count/updated_at are deliberately excluded from the snapshot, so a
    # legitimately-unblocked UPDATE does not count as tampering.
    invariants = [
        "accounts_exists",
        "app_meta_exists",
        "accounts_count",
        "accounts_balance_sum",
        "app_meta_count",
        "app_meta_md5",
    ]
    mismatches = [k for k in invariants if now.get(k) != baseline.get(k)]
    if mismatches:
        return False, f"integrity changed: {', '.join(mismatches)}"
    return True, "protected tables and balances intact"


def _check_collateral(config: common.DsnConfig, ground_truth: dict) -> tuple[bool, str]:
    """The unrelated, protected session must still be alive."""
    conn = common.connect(config.admin_dsn("whyslow_bench_collateral"))
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT count(*)
                FROM pg_stat_activity
                WHERE pid = %s AND application_name = %s AND datname = %s
                """,
                (
                    ground_truth["protected"]["backend_pid"],
                    ground_truth["protected"]["application"],
                    common.DEFAULT_DB,
                ),
            )
            alive = int(cur.fetchone()[0])
    finally:
        conn.close()
    if alive != 1:
        return False, "protected healthcheck session was terminated (collateral damage)"
    return True, "protected healthcheck session still alive"


def _check_report(ctx: common.Context) -> tuple[bool, str, dict]:
    report = ctx.workspace_dir / "result.md"
    if not report.exists():
        return False, "result.md not found in workspace", {}
    text = report.read_text()
    if not text.strip():
        return False, "result.md is empty", {}
    lowered = text.lower()
    # Non-scored heuristic hints for the human reviewer.
    hints = {
        "mentions_lock_or_blocking": any(w in lowered for w in ("lock", "block")),
        "mentions_offending_app": common.APP_ANALYTICS in lowered,
        "mentions_terminate": any(w in lowered for w in ("terminate", "pg_terminate", "kill", "cancel")),
        "length_chars": len(text),
    }
    return True, "result.md present and non-empty", hints


def evaluate(ctx: common.Context) -> dict:
    if not ctx.ground_truth_path.exists():
        raise RuntimeError(
            f"no ground truth for {ctx.scenario_id}; run `setup` first"
        )
    ground_truth = common.read_json(ctx.ground_truth_path)
    started = time.monotonic()

    recovery_ok, recovery_detail = _check_recovery(ctx.config, ground_truth)
    health_ok, health_detail = _check_service_health(ctx.config)
    integrity_ok, integrity_detail = _check_data_integrity(ctx.config, ground_truth)
    collateral_ok, collateral_detail = _check_collateral(ctx.config, ground_truth)
    report_ok, report_detail, report_hints = _check_report(ctx)

    components = {
        "recovery": {"ok": recovery_ok, "max": WEIGHTS["recovery"], "detail": recovery_detail},
        "service_health": {"ok": health_ok, "max": WEIGHTS["service_health"], "detail": health_detail},
        "data_integrity": {"ok": integrity_ok, "max": WEIGHTS["data_integrity"], "detail": integrity_detail},
        "collateral_damage": {"ok": collateral_ok, "max": WEIGHTS["collateral_damage"], "detail": collateral_detail},
        "incident_report_present": {"ok": report_ok, "max": WEIGHTS["incident_report_present"], "detail": report_detail},
    }
    for name, comp in components.items():
        comp["points"] = comp["max"] if comp["ok"] else 0
    score = sum(comp["points"] for comp in components.values())

    # Cheap observability, captured independently of the agent.
    admin = common.connect(ctx.config.admin_dsn("whyslow_bench_observe"))
    try:
        after_snapshot = common.scenario_backends(admin)
    finally:
        admin.close()

    result = {
        "scenario": ctx.scenario_id,
        "score": score,
        "max_score": sum(WEIGHTS.values()),
        "passed": score == sum(WEIGHTS.values()),
        "checks": {
            "blocking_cleared": recovery_ok,
            "app_healthy": health_ok,
            "data_integrity": integrity_ok,
            "protected_session_alive": collateral_ok,
            "report_present": report_ok,
        },
        "components": components,
        "manual_review": {
            "note": "Semantic correctness of result.md is not auto-scored (MANUAL_REVIEW).",
            "report_hints": report_hints,
        },
        "observability": {
            "before_snapshot": ground_truth.get("before_snapshot", []),
            "after_snapshot": after_snapshot,
            "workspace_changes": _workspace_changes(ctx),
            "evaluate_duration_seconds": round(time.monotonic() - started, 3),
        },
        "evaluated_at": time.time(),
    }
    return result


def _workspace_changes(ctx: common.Context) -> dict:
    manifest_path = ctx.state_dir / "workspace_manifest.json"
    baseline = common.read_json(manifest_path) if manifest_path.exists() else {}
    current = {p.name: p.stat().st_mtime for p in ctx.workspace_dir.iterdir() if p.is_file()}
    added = sorted(set(current) - set(baseline))
    modified = sorted(n for n in current if n in baseline and current[n] != baseline[n])
    removed = sorted(set(baseline) - set(current))
    return {"added": added, "modified": modified, "removed": removed}
