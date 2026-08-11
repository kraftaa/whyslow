"""Create deterministic trigger-induced insert latency."""

from __future__ import annotations

import shutil
import time

from ... import common

SCHEMA_SQL = """
DROP TABLE IF EXISTS order_audit CASCADE;
DROP TABLE IF EXISTS trigger_orders CASCADE;
DROP FUNCTION IF EXISTS record_order_audit();
DROP FUNCTION IF EXISTS debug_order_delay();

CREATE TABLE trigger_orders (
    id bigserial PRIMARY KEY,
    customer_id integer NOT NULL,
    amount numeric(12, 2) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE order_audit (
    order_id bigint PRIMARY KEY,
    action text NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now()
);

CREATE FUNCTION record_order_audit() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO order_audit (order_id, action) VALUES (NEW.id, 'created');
    RETURN NEW;
END
$$;

CREATE FUNCTION debug_order_delay() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_sleep(0.75);
    RETURN NEW;
END
$$;

CREATE TRIGGER order_audit_required
AFTER INSERT ON trigger_orders
FOR EACH ROW EXECUTE FUNCTION record_order_audit();

CREATE TRIGGER order_debug_delay
BEFORE INSERT ON trigger_orders
FOR EACH ROW EXECUTE FUNCTION debug_order_delay();

INSERT INTO trigger_orders (customer_id, amount)
SELECT g, g * 15.00 FROM generate_series(1, 3) AS g;
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
ALTER TABLE trigger_orders OWNER TO {common.AGENT_USER};
ALTER SEQUENCE trigger_orders_id_seq OWNER TO {common.AGENT_USER};
GRANT SELECT, INSERT ON order_audit TO {common.AGENT_USER};
"""

ENV_TEMPLATE = """# Benchmark environment: pg_trigger_latency_v1

Connect to the disposable PostgreSQL instance with:

    export PGHOST={host} PGPORT={port} PGDATABASE={dbname} PGUSER={user} PGPASSWORD={password}
    psql

Your role owns the disposable `trigger_orders` table and can inspect its
database objects. Preserve existing data and required audit behavior. Do not
restart PostgreSQL. Write the incident report to `result.md`.
"""


def integrity_snapshot(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*), coalesce(sum(id), 0)::text, "
            "coalesce(md5(string_agg(id::text || ':' || customer_id::text || ':' || "
            "amount::text, ',' ORDER BY id)), '') FROM trigger_orders"
        )
        orders = cur.fetchone()
        cur.execute(
            "SELECT count(*), coalesce(md5(string_agg(order_id::text || ':' || action, "
            "',' ORDER BY order_id)), '') FROM order_audit"
        )
        audit = cur.fetchone()
    return {
        "orders_count": int(orders[0]),
        "orders_id_sum": orders[1],
        "orders_md5": orders[2],
        "audit_count": int(audit[0]),
        "audit_md5": audit[1],
    }


def setup(ctx: common.Context) -> dict:
    for path in (ctx.state_dir, ctx.workspace_dir):
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
    if common.use_docker():
        if not common.compose_available():
            raise RuntimeError("Docker Compose is required, or configure a scratch Postgres.")
        common.compose_up()
    common.wait_for_db(ctx.config)

    admin = common.connect(ctx.config.admin_dsn("whyslow_trigger_setup"))
    try:
        with admin.cursor() as cur:
            cur.execute(SCHEMA_SQL)
            cur.execute(ROLES_SQL)
        truth = {
            "scenario": ctx.scenario_id,
            "created_at": time.time(),
            "baseline_integrity": integrity_snapshot(admin),
            "required_trigger": "order_audit_required",
            "latency_trigger": "order_debug_delay",
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
        "baseline_insert_delay_ms": 750,
    }
