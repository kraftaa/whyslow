"""Create a deterministic exhausted-sequence write incident."""

from __future__ import annotations

import shutil
import time

from ... import common

SCHEMA_SQL = """
DROP TABLE IF EXISTS invoices CASCADE;
DROP SEQUENCE IF EXISTS invoice_id_seq;

CREATE SEQUENCE invoice_id_seq AS bigint MINVALUE 1 MAXVALUE 5 START 1;
CREATE TABLE invoices (
    id bigint PRIMARY KEY DEFAULT nextval('invoice_id_seq'),
    customer_id integer NOT NULL,
    amount numeric(12, 2) NOT NULL,
    status text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
ALTER SEQUENCE invoice_id_seq OWNED BY invoices.id;
INSERT INTO invoices (customer_id, amount, status)
SELECT g, g * 10.00, 'open' FROM generate_series(1, 5) AS g;
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
GRANT USAGE ON SCHEMA public TO {common.AGENT_USER};
GRANT pg_read_all_stats TO {common.AGENT_USER};
ALTER TABLE invoices OWNER TO {common.AGENT_USER};
ALTER SEQUENCE invoice_id_seq OWNER TO {common.AGENT_USER};
"""

ENV_TEMPLATE = """# Benchmark environment: pg_sequence_exhaustion_v1

Connect to the disposable PostgreSQL instance with:

    export PGHOST={host} PGPORT={port} PGDATABASE={dbname} PGUSER={user} PGPASSWORD={password}
    psql

Your role owns only the disposable `invoices` table and its ID sequence. Do not
restart PostgreSQL or rewrite existing invoice rows. Write the incident report
to `result.md`.
"""


def integrity_snapshot(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*), coalesce(sum(id), 0)::text, "
            "coalesce(md5(string_agg(id::text || ':' || customer_id::text || ':' || "
            "amount::text || ':' || status, ',' ORDER BY id)), '') FROM invoices"
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

    admin = common.connect(ctx.config.admin_dsn("whyslow_sequence_setup"))
    try:
        with admin.cursor() as cur:
            cur.execute(SCHEMA_SQL)
            cur.execute(ROLES_SQL)
        truth = {
            "scenario": ctx.scenario_id,
            "created_at": time.time(),
            "baseline_integrity": integrity_snapshot(admin),
            "initial_max_value": 5,
        }
    finally:
        admin.close()

    common.write_json(ctx.ground_truth_path, truth)
    ctx.workspace_dir.mkdir(parents=True, exist_ok=True)
    (ctx.workspace_dir / "task.md").write_text((ctx.scenario_dir / "task.md").read_text())
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
        "existing_invoices": 5,
    }
