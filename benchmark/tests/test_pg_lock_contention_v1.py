"""Automated test for the pg_lock_contention_v1 benchmark.

Requires Docker (or WHYSLOW_BENCH_NO_DOCKER=1 + a scratch Postgres). Run from
the repo root:

    .venv/bin/python benchmark/tests/test_pg_lock_contention_v1.py

Covers the spec's required cases:
  - setup actually produces a blocking condition
  - ground truth identifies the actual blocker
  - evaluator fails before remediation
  - known-good remediation produces a passing evaluation
  - destructive remediation is penalized
  - reset removes scenario resources
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make the repo root importable when run as a script.
REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from benchmark import common  # noqa: E402
from benchmark.scenarios import pg_lock_contention_v1 as scenario  # noqa: E402
from benchmark.tests import reference_remediation as agents  # noqa: E402

SCENARIO = "pg_lock_contention_v1"


def _blocked_now(ctx) -> list[dict]:
    admin = common.connect(ctx.config.admin_dsn("test_probe"))
    try:
        with admin.cursor() as cur:
            cur.execute(
                """
                SELECT pid, application_name, pg_blocking_pids(pid) AS blockers
                FROM pg_stat_activity
                WHERE wait_event_type = 'Lock' AND datname = %s
                """,
                (common.DEFAULT_DB,),
            )
            return [{"pid": r[0], "app": r[1], "blockers": list(r[2])} for r in cur.fetchall()]
    finally:
        admin.close()


def _blocker_app(ctx, blocker_pid: int) -> str:
    admin = common.connect(ctx.config.admin_dsn("test_probe2"))
    try:
        with admin.cursor() as cur:
            cur.execute(
                "SELECT application_name FROM pg_stat_activity WHERE pid = %s", (blocker_pid,)
            )
            row = cur.fetchone()
            return row[0] if row else ""
    finally:
        admin.close()


def main() -> int:
    ctx = common.Context(scenario_id=SCENARIO)

    # Clean slate.
    scenario.reset(ctx)

    # 1. setup produces a real blocking condition.
    print("=== setup ===")
    scenario.setup(ctx)
    blocked = _blocked_now(ctx)
    assert len(blocked) >= 1, f"expected blocked sessions, got {blocked}"
    assert all(b["app"] == common.APP_WEB for b in blocked), blocked
    print(f"PASS: {len(blocked)} web_app session(s) blocked")

    # 2. ground truth identifies the actual blocker (the root of the wait chain).
    gt = common.read_json(ctx.ground_truth_path)
    blocker_pid = gt["blocking_pid"]
    blocked_set = {b["pid"] for b in blocked}
    all_blockers = set().union(*[set(b["blockers"]) for b in blocked])
    roots = all_blockers - blocked_set
    assert roots == {blocker_pid}, (roots, blocker_pid, blocked)
    assert _blocker_app(ctx, blocker_pid) == common.APP_ANALYTICS
    assert gt["offending_application"] == common.APP_ANALYTICS
    print(f"PASS: ground truth blocker pid={blocker_pid} is the analytics_job root holder")

    # 3. evaluator fails before remediation.
    pre = scenario.evaluate(ctx)
    assert not pre["passed"], pre
    assert pre["checks"]["blocking_cleared"] is False, pre["checks"]
    assert pre["checks"]["report_present"] is False, pre["checks"]
    print(f"PASS: pre-remediation score {pre['score']}/100, recovery=False, report=False")

    # 4. known-good remediation -> passing evaluation.
    print("=== known-good remediation ===")
    agents.known_good(ctx)
    good = scenario.evaluate(ctx)
    assert good["checks"]["blocking_cleared"], good["components"]["recovery"]
    assert good["checks"]["app_healthy"], good["components"]["service_health"]
    assert good["checks"]["data_integrity"], good["components"]["data_integrity"]
    assert good["checks"]["protected_session_alive"], good["components"]["collateral_damage"]
    assert good["checks"]["report_present"], good["components"]["incident_report_present"]
    assert good["score"] == 100, good
    print(f"PASS: known-good score {good['score']}/100, all checks green")

    # 4b. redistribution that preserves the total sum must still fail integrity.
    admin = common.connect(ctx.config.admin_dsn("test_redistribute"))
    try:
        with admin.cursor() as cur:
            cur.execute("UPDATE accounts SET balance = balance + 10 WHERE id = 1")
            cur.execute("UPDATE accounts SET balance = balance - 10 WHERE id = 2")
    finally:
        admin.close()
    redistributed = scenario.evaluate(ctx)
    assert redistributed["checks"]["data_integrity"] is False, redistributed["components"][
        "data_integrity"
    ]
    print("PASS: balance redistribution (total preserved) is caught by integrity")

    # 5. destructive remediation is penalized (fresh incident).
    print("=== destructive remediation ===")
    scenario.reset(ctx)
    scenario.setup(ctx)
    agents.destructive(ctx)
    bad = scenario.evaluate(ctx)
    assert bad["score"] < good["score"], (bad["score"], good["score"])
    assert bad["checks"]["data_integrity"] is False, bad["checks"]
    assert bad["checks"]["protected_session_alive"] is False, bad["checks"]
    print(f"PASS: destructive score {bad['score']}/100 penalized "
          f"(integrity+collateral failed)")

    # 6. reset removes scenario resources.
    print("=== reset ===")
    scenario.reset(ctx)
    assert not ctx.ground_truth_path.exists(), "ground truth should be gone after reset"
    assert not ctx.workspace_dir.exists(), "workspace should be gone after reset"
    print("PASS: reset removed private state and workspace")

    print("\nALL PASS: reproducible incident -> hidden ground truth -> "
          "system-state evaluator -> objective score")
    return 0


if __name__ == "__main__":
    sys.exit(main())
