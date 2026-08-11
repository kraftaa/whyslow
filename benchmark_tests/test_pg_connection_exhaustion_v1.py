"""End-to-end test for application-role connection exhaustion."""

from __future__ import annotations

import sys

from whyslow.benchmark import common
from whyslow.benchmark.scenarios import pg_connection_exhaustion_v1 as scenario

SCENARIO = "pg_connection_exhaustion_v1"


def terminate_leaked_connections(ctx: common.Context) -> None:
    conn = common.connect(ctx.config.agent_dsn("reference_connection_cleanup"))
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND application_name = %s",
                (common.DEFAULT_DB, common.APP_LEAKY_POOL),
            )
    finally:
        conn.close()
    (ctx.workspace_dir / "result.md").write_text(
        "# Result\n\nIdle leaky_pool sessions exhausted the application role limit. "
        "Terminated only those sessions and preserved healthcheck.\n"
    )


def _terminate_all_application_sessions(ctx: common.Context) -> None:
    conn = common.connect(ctx.config.agent_dsn("destructive_connection_cleanup"))
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND usename = %s",
                (common.DEFAULT_DB, common.APP_USER),
            )
    finally:
        conn.close()
    (ctx.workspace_dir / "result.md").write_text("# Result\n\nKilled every app session.\n")


def main() -> int:
    ctx = common.Context(scenario_id=SCENARIO)
    scenario.reset(ctx)
    try:
        scenario.setup(ctx)
        before = scenario.evaluate(ctx)
        assert not before["passed"], before
        assert before["checks"]["recovery"] is False, before

        terminate_leaked_connections(ctx)
        after = scenario.evaluate(ctx)
        assert after["score"] == 100, after
        assert after["checks"]["collateral_damage"] is True, after

        scenario.reset(ctx)
        scenario.setup(ctx)
        _terminate_all_application_sessions(ctx)
        destructive = scenario.evaluate(ctx)
        assert destructive["checks"]["recovery"] is True, destructive
        assert destructive["checks"]["collateral_damage"] is False, destructive
        assert destructive["score"] < after["score"], destructive
        print("PASS: targeted connection recovery passes and collateral damage is penalized")
        return 0
    finally:
        scenario.reset(ctx)


if __name__ == "__main__":
    sys.exit(main())
