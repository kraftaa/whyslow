"""Reset the cross-tenant access scenario."""

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
            conn = common.connect(ctx.config.admin_dsn("whyslow_tenant_access_reset"))
            try:
                with conn.cursor() as cur:
                    cur.execute("DROP FUNCTION IF EXISTS read_access_handoff() CASCADE")
                    cur.execute("DROP TABLE IF EXISTS access_handoff_reads CASCADE")
                    cur.execute("DROP TABLE IF EXISTS operational_handoff CASCADE")
                    cur.execute("DROP TABLE IF EXISTS tenant_alpha_orders CASCADE")
                    cur.execute("DROP TABLE IF EXISTS tenant_beta_payroll CASCADE")
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
