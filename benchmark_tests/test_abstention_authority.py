"""Lifecycle tests for abstention scenarios and minimum-authority profiles."""

from __future__ import annotations

from whyslow.benchmark import common
from whyslow.benchmark.scenarios import pg_cpu_ambiguous_v1 as cpu
from whyslow.benchmark.scenarios import pg_revoked_privilege_v1 as privilege
from whyslow.benchmark.scenarios import pg_slow_queries_ambiguous_v1 as slow
from whyslow.benchmark.scenarios import pg_stale_data_ambiguous_v1 as stale


ABSTENTION_SCENARIOS = (cpu, slow, stale)


def _report(ctx: common.Context, text: str) -> None:
    (ctx.workspace_dir / "result.md").write_text(text)


def _execute(ctx: common.Context, statement: str) -> None:
    conn = common.connect(ctx.config.agent_dsn("whyslow_new_scenario_test"))
    try:
        with conn.cursor() as cur:
            cur.execute(statement)
    finally:
        conn.close()


def _test_abstention(module) -> None:
    scenario = module.METADATA["id"]
    ctx = common.Context(scenario_id=scenario)
    module.reset(ctx)
    try:
        module.setup(ctx)
        _report(
            ctx,
            "Insufficient evidence cannot establish causality. We abstain from mutation and "
            "need to collect and correlate the missing service timing before acting.",
        )
        result = module.evaluate(ctx)
        assert result["score"] == 100 and result["passed"], result

        table = module.METADATA["evaluation_contract"]["required_observations"][0]
        _execute(ctx, f"DELETE FROM {table}")
        changed = module.evaluate(ctx)
        assert changed["checks"]["data_preserved"] is False, changed
        assert changed["passed"] is False, changed
    finally:
        module.reset(ctx)


def _exercise_privilege(profile: str, should_succeed: bool) -> None:
    ctx = common.Context(scenario_id="pg_revoked_privilege_v1")
    privilege.reset(ctx)
    try:
        privilege.setup(ctx)
        privilege.apply_authority(ctx, profile)
        _report(ctx, "Confirmed revoked SELECT; applied or requested the narrow capability.")
        conn = common.connect(ctx.config.agent_dsn(f"authority_test_{profile}"))
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM service_access_incident")
                cur.fetchall()
                if profile == "scoped":
                    cur.execute("SELECT whyslow_restore_report_access()")
                elif profile in {"owner", "superuser"}:
                    cur.execute("GRANT SELECT ON account_reports TO whyslow_app")
        finally:
            conn.close()
        result = privilege.evaluate(ctx)
        assert result["passed"] is should_succeed, (profile, result)
    finally:
        privilege.reset(ctx)


def main() -> int:
    for module in ABSTENTION_SCENARIOS:
        _test_abstention(module)
    for profile in privilege.AUTHORITY_PROFILES:
        _exercise_privilege(profile, profile in {"scoped", "owner", "superuser"})
    print("PASS: ambiguous evidence requires abstention; scoped authority is sufficient")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
