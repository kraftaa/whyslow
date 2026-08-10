"""Tear down pg_lock_contention_v1. Idempotent and safe to call repeatedly."""

from __future__ import annotations

import glob
import os
import shutil
import signal

import psycopg2

from ... import common

# Must appear in a live actor's command line; used to confirm a pid is still
# ours before signalling it, so a reused pid can never hit an unrelated process.
ACTOR_MARKER = "benchmark.scenarios.pg_lock_contention_v1.actors"


def kill_actor_processes(ctx: common.Context) -> int:
    """Terminate the detached OS actor processes recorded during setup.

    Every pid is verified to still be one of our actors (by command line)
    before ``os.kill`` -- if the actor already exited and the OS reused its pid,
    the check fails and we skip it rather than signalling a stranger.
    """
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
        cmdline = common.process_cmdline(pid)
        if not cmdline or ACTOR_MARKER not in cmdline:
            continue  # gone, or the pid was reused by an unrelated process
        try:
            os.kill(pid, signal.SIGTERM)
            killed += 1
        except (ProcessLookupError, PermissionError):
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

    docker_down_ok = True
    docker_error = ""
    if common.use_docker():
        docker_down_ok, docker_error = common.compose_down()

    # Remove private state and the agent workspace; keep results history.
    for path in (ctx.state_dir, ctx.workspace_dir):
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)

    return {
        "scenario": ctx.scenario_id,
        "actor_processes_killed": killed,
        "backends_terminated": terminated,
        "docker_attempted": common.use_docker(),
        "docker_down_ok": docker_down_ok,
        "docker_error": docker_error,
    }
