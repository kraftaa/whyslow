"""Tear down pg_lock_contention_v1. Idempotent and safe to call repeatedly."""

from __future__ import annotations

import glob
import os
import shutil
import signal

import psycopg2

from ... import common


def kill_actor_processes(ctx: common.Context) -> int:
    """Kill the detached OS actor processes recorded during setup."""
    killed = 0
    pids: list[int] = []
    if ctx.ground_truth_path.exists():
        try:
            pids = list(common.read_json(ctx.ground_truth_path).get("actor_os_pids", []))
        except Exception:
            pids = []
    # Fall back to any actor ready-files still present.
    for ready in glob.glob(str(ctx.state_dir / "actor_*.json")):
        try:
            pids.append(int(common.read_json(ready).get("os_pid")))
        except Exception:
            pass
    for pid in {p for p in pids if p}:
        try:
            os.kill(pid, signal.SIGTERM)
            killed += 1
        except ProcessLookupError:
            pass
        except PermissionError:
            pass
    return killed


def reset(ctx: common.Context) -> dict:
    killed = kill_actor_processes(ctx)

    # Terminate any lingering scenario backends before tearing the DB down.
    terminated = 0
    if not common.use_docker():
        try:
            admin = common.connect(ctx.config.admin_dsn("whyslow_bench_reset"), connect_timeout=3)
            try:
                terminated = common.terminate_scenario_backends(admin)
            finally:
                admin.close()
        except psycopg2.Error:
            pass

    if common.use_docker():
        common.compose_down()

    # Remove private state and the agent workspace; keep results history.
    for path in (ctx.state_dir, ctx.workspace_dir):
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)

    return {
        "scenario": ctx.scenario_id,
        "actor_processes_killed": killed,
        "backends_terminated": terminated,
        "docker_down": common.use_docker(),
    }
