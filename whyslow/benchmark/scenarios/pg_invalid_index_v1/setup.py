"""Create a failed concurrent-index deployment artifact."""

from __future__ import annotations

import shutil
import time

import psycopg2

from ... import common

QUERY = """
SELECT id, customer_id, created_at, total
FROM deployment_orders
WHERE customer_id = 42
  AND created_at >= now() - interval '30 days'
ORDER BY created_at DESC
LIMIT 100
"""

SCHEMA_SQL = """
DROP TABLE IF EXISTS deployment_orders CASCADE;

CREATE TABLE deployment_orders (
    id bigserial PRIMARY KEY,
    customer_id integer NOT NULL,
    created_at timestamptz NOT NULL,
    total numeric(12, 2) NOT NULL,
    status text NOT NULL
);
INSERT INTO deployment_orders (customer_id, created_at, total, status)
SELECT
    (g % 250) + 1,
    date_trunc('minute', now() - ((g % 120) || ' days')::interval),
    (g % 5000) + 0.99,
    CASE WHEN g % 4 = 0 THEN 'paid' ELSE 'open' END
FROM generate_series(1, 60000) AS g;
ANALYZE deployment_orders;
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
ALTER TABLE deployment_orders OWNER TO {common.AGENT_USER};
"""

ENV_TEMPLATE = """# Benchmark environment: pg_invalid_index_v1

Connect to the disposable PostgreSQL instance with:

    export PGHOST={host} PGPORT={port} PGDATABASE={dbname} PGUSER={user} PGPASSWORD={password}
    psql

The affected query is in `query.sql`. Your role owns only the disposable
`deployment_orders` table and its indexes. Preserve order data and do not
restart PostgreSQL. Write the incident report to `result.md`.
"""


def plan(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute("EXPLAIN (FORMAT JSON) " + QUERY)
        return cur.fetchone()[0][0]["Plan"]


def integrity_snapshot(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*), coalesce(sum(id), 0)::text, "
            "coalesce(md5(string_agg(id::text || ':' || customer_id::text || ':' || "
            "total::text || ':' || status, ',' ORDER BY id)), '') FROM deployment_orders"
        )
        row = cur.fetchone()
    return {"count": int(row[0]), "id_sum": row[1], "rows_md5": row[2]}


def setup(ctx: common.Context) -> dict:
    for path in (ctx.state_dir, ctx.workspace_dir):
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
    if common.use_docker():
        if not common.compose_available():
            raise RuntimeError("Docker Compose is required, or configure a scratch Postgres.")
        common.compose_up()
    common.wait_for_db(ctx.config)

    admin = common.connect(ctx.config.admin_dsn("whyslow_invalid_index_setup"))
    try:
        with admin.cursor() as cur:
            cur.execute(SCHEMA_SQL)
            try:
                cur.execute(
                    "CREATE UNIQUE INDEX CONCURRENTLY deployment_orders_customer_created_idx "
                    "ON deployment_orders (customer_id, created_at DESC)"
                )
            except psycopg2.errors.UniqueViolation:
                pass
            cur.execute(ROLES_SQL)
        with admin.cursor() as cur:
            cur.execute(
                "SELECT indisvalid, indisready FROM pg_index "
                "WHERE indexrelid = to_regclass('deployment_orders_customer_created_idx')"
            )
            invalid_state = cur.fetchone()
        if invalid_state is None or invalid_state[0]:
            raise RuntimeError("failed concurrent index build did not leave an invalid artifact")
        truth = {
            "scenario": ctx.scenario_id,
            "created_at": time.time(),
            "baseline_integrity": integrity_snapshot(admin),
            "before_plan": plan(admin),
            "invalid_index": "deployment_orders_customer_created_idx",
        }
    finally:
        admin.close()

    common.write_json(ctx.ground_truth_path, truth)
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
    return {
        "scenario": ctx.scenario_id,
        "workspace": str(ctx.workspace_dir),
        "rows": 60000,
        "invalid_indexes": 1,
    }
