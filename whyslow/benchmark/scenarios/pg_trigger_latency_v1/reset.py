"""Reset the trigger-latency scenario."""

from __future__ import annotations

import shutil

import psycopg2

from ... import common


def reset(ctx: common.Context) -> dict:
    docker_ok, docker_error = True, ""
    if common.use_docker():
        docker_ok, docker_error = common.compose_down()
    else:
        try:
            conn = common.connect(ctx.config.admin_dsn("whyslow_trigger_reset"))
            try:
                with conn.cursor() as cur:
                    cur.execute("DROP TABLE IF EXISTS trigger_orders CASCADE")
                    cur.execute("DROP TABLE IF EXISTS order_audit CASCADE")
                    cur.execute("DROP FUNCTION IF EXISTS record_order_audit()")
                    cur.execute("DROP FUNCTION IF EXISTS debug_order_delay()")
            finally:
                conn.close()
        except psycopg2.Error:
            pass
    for path in (ctx.state_dir, ctx.workspace_dir):
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
    return {
        "scenario": ctx.scenario_id,
        "docker_attempted": common.use_docker(),
        "docker_down_ok": docker_ok,
        "docker_error": docker_error,
    }
