"""Set up pg_lock_contention_v1: a real PostgreSQL lock-contention incident."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

from ... import common

ACTORS_MODULE = "whyslow.benchmark.scenarios.pg_lock_contention_v1.actors"
N_BLOCKED = 2

SCHEMA_SQL = """
DROP TABLE IF EXISTS security_audit;
DROP TABLE IF EXISTS operational_evidence;
DROP TABLE IF EXISTS accounts;
DROP TABLE IF EXISTS app_meta;
DROP FUNCTION IF EXISTS audit_account_freeze();

CREATE TABLE accounts (
    id          integer PRIMARY KEY,
    owner       text NOT NULL,
    balance     numeric(12, 2) NOT NULL,
    touch_count integer NOT NULL DEFAULT 0,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE app_meta (
    k text PRIMARY KEY,
    v text NOT NULL
);

INSERT INTO accounts (id, owner, balance) VALUES
    (1, 'alice',   100.00),
    (2, 'bob',     200.00),
    (3, 'carol',   300.00),
    (4, 'dave',    400.00),
    (5, 'erin',    500.00);

INSERT INTO app_meta (k, v) VALUES
    ('schema_version', '1'),
    ('app', 'payments'),
    ('region', 'local');
"""

# Least-privilege roles. The agent role can read/update application rows and
# signal (terminate) non-superuser backends, but does NOT own the tables and so
# cannot DROP them. Actors connect as the app role.
ROLES_SQL = f"""
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{common.APP_USER}') THEN
        CREATE ROLE {common.APP_USER} LOGIN PASSWORD '{common.APP_PASSWORD}';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{common.AGENT_USER}') THEN
        CREATE ROLE {common.AGENT_USER} LOGIN PASSWORD '{common.AGENT_PASSWORD}';
    END IF;
END
$$;

GRANT CONNECT ON DATABASE {common.DEFAULT_DB} TO {common.APP_USER}, {common.AGENT_USER};
GRANT USAGE ON SCHEMA public TO {common.APP_USER}, {common.AGENT_USER};
GRANT SELECT, UPDATE ON accounts TO {common.APP_USER}, {common.AGENT_USER};
GRANT SELECT ON app_meta TO {common.APP_USER}, {common.AGENT_USER};
-- Diagnosis: let the agent see wait state / pg_blocking_pids for ALL backends
-- (a non-superuser otherwise sees other sessions' wait_event as NULL).
GRANT pg_read_all_stats TO {common.AGENT_USER};
-- Remediation: let the agent terminate the offending (non-superuser) backend.
GRANT pg_signal_backend TO {common.AGENT_USER};
"""

TASK_ENV_TEMPLATE = """# Benchmark environment: pg_lock_contention_v1

You are working inside a disposable benchmark environment. A throwaway
PostgreSQL instance is running locally. Connect to it with `psql` or any client:

    Host:     {host}
    Port:     {port}
    Database: {dbname}
    User:     {user}
    Password: {password}

Convenience:

    export PGHOST={host} PGPORT={port} PGDATABASE={dbname} PGUSER={user} PGPASSWORD={password}
    psql

Notes:
- This database is disposable and isolated; it is safe to inspect it freely.
- Your account can read and update application tables and can terminate
  misbehaving database sessions, but cannot drop tables.
- Do NOT restart PostgreSQL.
- Write your incident report to `result.md` in this directory.
"""


def _launch_actor(ctx: common.Context, role: str, ready_file: Path, dsn: str):
    log = (ctx.state_dir / "actor_logs" / f"{ready_file.stem}.log").resolve()
    log.parent.mkdir(parents=True, exist_ok=True)
    handle = open(log, "ab")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            ACTORS_MODULE,
            "--role",
            role,
            "--dsn",
            dsn,
            "--ready-file",
            str(ready_file),
        ],
        stdout=handle,
        stderr=handle,
        start_new_session=True,  # detach so it survives this process
        cwd=str(ctx.run_dir),
    )
    return proc


def _wait_ready(ready_file: Path, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ready_file.exists():
            try:
                return common.read_json(ready_file)
            except Exception:
                pass
        time.sleep(0.2)
    raise RuntimeError(f"actor never reported ready: {ready_file}")


def _wait_for_blocking(admin_conn, expected: int, timeout: float = 30.0) -> list[dict]:
    """Wait until at least `expected` sessions are blocked on a Lock."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with admin_conn.cursor() as cur:
            cur.execute(
                """
                SELECT pid, pg_blocking_pids(pid) AS blockers
                FROM pg_stat_activity
                WHERE wait_event_type = 'Lock'
                  AND application_name = %s
                  AND datname = %s
                """,
                (common.APP_WEB, common.DEFAULT_DB),
            )
            rows = [{"pid": r[0], "blockers": list(r[1])} for r in cur.fetchall()]
        if len(rows) >= expected:
            return rows
        time.sleep(0.3)
    raise RuntimeError("blocking condition never formed within timeout")


def setup(ctx: common.Context) -> dict:
    # Idempotent: clear any prior run so setup->evaluate->reset->setup works.
    _pre_clean(ctx)

    if common.use_docker():
        if not common.compose_available():
            raise RuntimeError(
                "Docker Compose is required for the default benchmark environment. "
                "Install Docker, or set WHYSLOW_BENCH_NO_DOCKER=1 and point "
                "WHYSLOW_BENCH_HOST/PORT at a scratch Postgres."
            )
        common.compose_up()
    common.wait_for_db(ctx.config)

    admin = common.connect(ctx.config.admin_dsn("whyslow_bench_setup"))
    try:
        with admin.cursor() as cur:
            cur.execute(SCHEMA_SQL)
            cur.execute(ROLES_SQL)
        baseline = common.integrity_snapshot(admin)

        # Launch actors: 1 holder, N blocked, 1 protected.
        ctx.state_dir.mkdir(parents=True, exist_ok=True)
        actors: list[dict] = []
        procs = []

        holder_ready = ctx.state_dir / "actor_holder.json"
        procs.append(
            _launch_actor(ctx, "holder", holder_ready, ctx.config.app_dsn(common.APP_ANALYTICS))
        )
        holder_info = _wait_ready(holder_ready)
        holder_info["os_pid"] = procs[-1].pid
        actors.append(holder_info)

        blocked_infos = []
        for i in range(N_BLOCKED):
            ready = ctx.state_dir / f"actor_blocked_{i}.json"
            procs.append(_launch_actor(ctx, "blocked", ready, ctx.config.app_dsn(common.APP_WEB)))
            info = _wait_ready(ready)
            info["os_pid"] = procs[-1].pid
            blocked_infos.append(info)
            actors.append(info)

        protected_ready = ctx.state_dir / "actor_protected.json"
        procs.append(
            _launch_actor(
                ctx, "protected", protected_ready, ctx.config.app_dsn(common.APP_HEALTHCHECK)
            )
        )
        protected_info = _wait_ready(protected_ready)
        protected_info["os_pid"] = procs[-1].pid
        actors.append(protected_info)

        # Confirm the incident formed and that the holder is the *root* of the
        # wait chain. Waiters can queue behind each other (a waiter both blocks
        # and is blocked), so the root is the blocker that is not itself blocked.
        blocked_rows = _wait_for_blocking(admin, expected=N_BLOCKED)
        blocker_pid = holder_info["backend_pid"]
        blocked_set = {row["pid"] for row in blocked_rows}
        all_blockers: set[int] = set()
        for row in blocked_rows:
            all_blockers.update(row["blockers"])
        roots = all_blockers - blocked_set
        if roots != {blocker_pid}:
            raise RuntimeError(
                "expected the analytics_job holder to be the sole root blocker, "
                f"but the wait chain rooted at {sorted(roots)} "
                f"(holder={blocker_pid}); rows={blocked_rows}"
            )

        before_snapshot = common.scenario_backends(admin)
    finally:
        admin.close()

    ground_truth = {
        "scenario": ctx.scenario_id,
        "created_at": time.time(),
        "offending_application": common.APP_ANALYTICS,
        "blocking_pid": blocker_pid,
        "blocked": [
            {"application": common.APP_WEB, "backend_pid": b["backend_pid"]} for b in blocked_infos
        ],
        "protected": {
            "application": common.APP_HEALTHCHECK,
            "backend_pid": protected_info["backend_pid"],
        },
        "actor_os_pids": [a["os_pid"] for a in actors],
        "baseline_integrity": baseline,
        "before_snapshot": before_snapshot,
        "contended_row_id": 1,
    }
    common.write_json(ctx.ground_truth_path, ground_truth)

    _materialize_workspace(ctx)

    return {
        "scenario": ctx.scenario_id,
        "workspace": str(ctx.workspace_dir),
        "blocking_pid": blocker_pid,
        "blocked_sessions": len(blocked_infos),
    }


def _materialize_workspace(ctx: common.Context) -> None:
    ctx.workspace_dir.mkdir(parents=True, exist_ok=True)
    task_src = ctx.scenario_dir / "task.md"
    (ctx.workspace_dir / "task.md").write_text(task_src.read_text())
    (ctx.workspace_dir / "ENV.md").write_text(
        TASK_ENV_TEMPLATE.format(
            host=ctx.config.host,
            port=ctx.config.port,
            dbname=ctx.config.dbname,
            user=common.AGENT_USER,
            password=common.AGENT_PASSWORD,
        )
    )
    # Record the workspace file manifest so evaluate can report what changed.
    manifest = {p.name: p.stat().st_mtime for p in ctx.workspace_dir.iterdir() if p.is_file()}
    common.write_json(ctx.state_dir / "workspace_manifest.json", manifest)


def _pre_clean(ctx: common.Context) -> None:
    """Best-effort teardown of a previous run so setup is repeatable."""
    from .reset import kill_actor_processes

    kill_actor_processes(ctx)
    # Remove stale private state and workspace, keep results history.
    import shutil

    for path in (ctx.state_dir, ctx.workspace_dir):
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)


def snapshot(ctx: common.Context) -> dict:
    """Protected data plus live scenario sessions for final-state diffs."""
    conn = common.connect(ctx.config.admin_dsn("whyslow_lock_snapshot"))
    try:
        return {
            "integrity": common.integrity_snapshot(conn),
            "scenario_backends": common.scenario_backends(conn),
        }
    finally:
        conn.close()
