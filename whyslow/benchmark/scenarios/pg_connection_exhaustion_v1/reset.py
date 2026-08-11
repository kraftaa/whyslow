"""Reset connection-exhaustion scenarios and their detached actors."""

from __future__ import annotations

import glob
import os
import shutil
import signal

import psycopg2

from ... import common

ACTOR_MARKER = "whyslow.benchmark.scenarios.pg_connection_exhaustion_v1.actors"


def kill_actor_processes(ctx: common.Context) -> int:
    pids: list[int] = []
    if ctx.ground_truth_path.exists():
        try:
            pids.extend(common.read_json(ctx.ground_truth_path).get("actor_os_pids", []))
        except Exception:
            pass
    for ready in glob.glob(str(ctx.state_dir / "actor_*.json")):
        try:
            pids.append(int(common.read_json(ready)["os_pid"]))
        except Exception:
            pass
    killed = 0
    for pid in set(pids):
        cmdline = common.process_cmdline(pid)
        if not cmdline or ACTOR_MARKER not in cmdline:
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            killed += 1
        except (ProcessLookupError, PermissionError):
            pass
    return killed


def reset(ctx: common.Context) -> dict:
    killed = kill_actor_processes(ctx)
    terminated = 0
    if not common.use_docker():
        try:
            conn = common.connect(ctx.config.admin_dsn("whyslow_connections_reset"))
            try:
                terminated = common.terminate_scenario_backends(conn)
                with conn.cursor() as cur:
                    cur.execute(f"ALTER ROLE {common.APP_USER} CONNECTION LIMIT -1")
            finally:
                conn.close()
        except psycopg2.Error:
            pass
    docker_ok, docker_error = True, ""
    if common.use_docker():
        docker_ok, docker_error = common.compose_down()
    for path in (ctx.state_dir, ctx.workspace_dir):
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
    return {
        "scenario": ctx.scenario_id,
        "actor_processes_killed": killed,
        "backends_terminated": terminated,
        "docker_attempted": common.use_docker(),
        "docker_down_ok": docker_ok,
        "docker_error": docker_error,
    }
