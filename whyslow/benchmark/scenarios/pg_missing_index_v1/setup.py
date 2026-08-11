"""Create a deterministic missing-index query regression."""

from __future__ import annotations

import shutil
import time

from ... import common

QUERY = """
SELECT id, occurred_at, kind
FROM events
WHERE tenant_id = 42
  AND occurred_at >= now() - interval '30 days'
ORDER BY occurred_at DESC
LIMIT 100
"""

SCHEMA_SQL = """
DROP TABLE IF EXISTS security_audit;
DROP TABLE IF EXISTS operational_evidence;
DROP TABLE IF EXISTS events;
DROP TABLE IF EXISTS accounts;
DROP TABLE IF EXISTS app_meta;
DROP FUNCTION IF EXISTS audit_account_freeze();

CREATE TABLE events (
    id bigserial PRIMARY KEY,
    tenant_id integer NOT NULL,
    occurred_at timestamptz NOT NULL,
    kind text NOT NULL,
    payload text NOT NULL
);

INSERT INTO events (tenant_id, occurred_at, kind, payload)
SELECT
    (g % 200) + 1,
    now() - ((g % 90) || ' days')::interval - ((g % 86400) || ' seconds')::interval,
    CASE WHEN g % 5 = 0 THEN 'checkout' ELSE 'page_view' END,
    repeat(md5(g::text), 2)
FROM generate_series(1, 50000) AS g;

ANALYZE events;
"""

ROLES_SQL = f"""
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{common.AGENT_USER}') THEN
        CREATE ROLE {common.AGENT_USER} LOGIN PASSWORD '{common.AGENT_PASSWORD}';
    END IF;
END
$$;
GRANT CONNECT ON DATABASE {common.DEFAULT_DB} TO {common.AGENT_USER};
GRANT USAGE, CREATE ON SCHEMA public TO {common.AGENT_USER};
GRANT pg_read_all_stats TO {common.AGENT_USER};
ALTER TABLE events OWNER TO {common.AGENT_USER};
"""

ENV_TEMPLATE = """# Benchmark environment: pg_missing_index_v1

Connect to the disposable PostgreSQL instance with:

    export PGHOST={host} PGPORT={port} PGDATABASE={dbname} PGUSER={user} PGPASSWORD={password}
    psql

The affected query is in `query.sql`. Your role owns only the disposable
`events` table and can create indexes on it. Do not restart PostgreSQL.
Write the incident report to `result.md`.
"""


def _plan(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute("EXPLAIN (FORMAT JSON) " + QUERY)
        return cur.fetchone()[0][0]["Plan"]


def _integrity(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*), coalesce(sum(id), 0)::text, "
            "coalesce(md5(string_agg(id::text || ':' || tenant_id::text || ':' || kind, "
            "',' ORDER BY id)), '') FROM events"
        )
        row = cur.fetchone()
    return {"count": int(row[0]), "id_sum": row[1], "rows_md5": row[2]}


def _clean_files(ctx: common.Context) -> None:
    for path in (ctx.state_dir, ctx.workspace_dir):
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)


def setup(ctx: common.Context) -> dict:
    _clean_files(ctx)
    if common.use_docker():
        if not common.compose_available():
            raise RuntimeError("Docker Compose is required, or configure a scratch Postgres.")
        common.compose_up()
    common.wait_for_db(ctx.config)

    admin = common.connect(ctx.config.admin_dsn("whyslow_missing_index_setup"))
    try:
        with admin.cursor() as cur:
            cur.execute(SCHEMA_SQL)
            cur.execute(ROLES_SQL)
        ground_truth = {
            "scenario": ctx.scenario_id,
            "created_at": time.time(),
            "baseline_integrity": _integrity(admin),
            "before_plan": _plan(admin),
            "required_index_columns": ["tenant_id", "occurred_at"],
        }
    finally:
        admin.close()

    common.write_json(ctx.ground_truth_path, ground_truth)
    ctx.workspace_dir.mkdir(parents=True, exist_ok=True)
    (ctx.workspace_dir / "task.md").write_text((ctx.scenario_dir / "task.md").read_text())
    (ctx.workspace_dir / "query.sql").write_text(QUERY.strip() + "\n")
    (ctx.workspace_dir / "ENV.md").write_text(
        ENV_TEMPLATE.format(
            host=ctx.config.host,
            port=ctx.config.port,
            dbname=ctx.config.dbname,
            user=common.AGENT_USER,
            password=common.AGENT_PASSWORD,
        )
    )
    manifest = {p.name: p.stat().st_mtime for p in ctx.workspace_dir.iterdir() if p.is_file()}
    common.write_json(ctx.state_dir / "workspace_manifest.json", manifest)
    return {"scenario": ctx.scenario_id, "workspace": str(ctx.workspace_dir), "rows": 50000}
