"""End-to-end test for the missing-index benchmark."""

from __future__ import annotations

import sys

from whyslow.benchmark import common
from whyslow.benchmark.scenarios import pg_missing_index_v1 as scenario

SCENARIO = "pg_missing_index_v1"


def _remediate(ctx: common.Context) -> None:
    conn = common.connect(ctx.config.agent_dsn("reference_missing_index"))
    try:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE INDEX events_tenant_occurred_idx ON events (tenant_id, occurred_at DESC)"
            )
            cur.execute("ANALYZE events")
    finally:
        conn.close()
    (ctx.workspace_dir / "result.md").write_text(
        "# Result\n\nThe tenant-history query lacked a composite index. "
        "Added and validated `(tenant_id, occurred_at DESC)`.\n"
    )


def main() -> int:
    ctx = common.Context(scenario_id=SCENARIO)
    scenario.reset(ctx)
    try:
        scenario.setup(ctx)
        before = scenario.evaluate(ctx)
        assert not before["passed"], before
        assert before["checks"]["query_recovery"] is False, before

        _remediate(ctx)
        after = scenario.evaluate(ctx)
        assert after["score"] == 100, after
        assert after["checks"]["query_recovery"] is True, after

        conn = common.connect(ctx.config.agent_dsn("destructive_missing_index"))
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM events WHERE tenant_id = 1")
        finally:
            conn.close()
        destructive = scenario.evaluate(ctx)
        assert destructive["checks"]["data_integrity"] is False, destructive
        assert destructive["score"] < after["score"], destructive
        print("PASS: missing-index recovery is detected and data damage is penalized")
        return 0
    finally:
        scenario.reset(ctx)


if __name__ == "__main__":
    sys.exit(main())
