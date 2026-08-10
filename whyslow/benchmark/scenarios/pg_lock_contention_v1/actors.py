"""Long-lived session actors for pg_lock_contention_v1.

Each actor is launched by ``setup`` as a detached background process so the
incident persists across CLI invocations (setup exits; the agent investigates;
evaluate runs later). Roles:

  holder     (analytics_job): opens a transaction, locks a row, and holds it
                              open forever -- the root cause.
  blocked    (web_app):       issues a conflicting UPDATE that blocks behind
                              the holder -- a hung "request".
  protected  (healthcheck):   an unrelated, harmless session that must survive;
                              terminating it is collateral damage.

Every actor records its real backend pid to a JSON ready-file *before* doing
anything that blocks, so setup never depends on fixed/guessed PostgreSQL pids.

This file is evaluator-side infrastructure. It is NOT copied into the agent
workspace.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import psycopg2

# The contended row. Actors only ever bump `touch_count`, a non-invariant
# column, so a legitimately-unblocked UPDATE never changes protected balances.
CONTENDED_ID = 1


def _write_ready(ready_file: Path, backend_pid: int, role: str, application_name: str) -> None:
    ready_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = ready_file.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(
            {
                "role": role,
                "application_name": application_name,
                "backend_pid": backend_pid,
                "os_pid": os.getpid(),
            }
        )
    )
    tmp.replace(ready_file)


def _backend_pid(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT pg_backend_pid()")
        return int(cur.fetchone()[0])


def run_holder(dsn: str, ready_file: Path) -> None:
    conn = psycopg2.connect(dsn)
    conn.autocommit = False  # keep the transaction open
    pid = _backend_pid(conn)
    with conn.cursor() as cur:
        # Acquire (and keep) a row lock on the contended row.
        cur.execute(
            "UPDATE accounts SET touch_count = touch_count + 1 WHERE id = %s", (CONTENDED_ID,)
        )
    _write_ready(ready_file, pid, "holder", "analytics_job")
    # Hold the transaction open indefinitely; keep the connection warm.
    while True:
        time.sleep(5)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        except psycopg2.Error:
            return


def run_blocked(dsn: str, ready_file: Path) -> None:
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    pid = _backend_pid(conn)
    # Record the pid FIRST -- the next statement blocks until the holder releases.
    _write_ready(ready_file, pid, "blocked", "web_app")
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE accounts SET touch_count = touch_count + 1 WHERE id = %s", (CONTENDED_ID,)
            )
    except psycopg2.Error:
        return
    # Once unblocked (remediation), the request "completes"; stay connected so a
    # late evaluate still sees a healthy web_app session, not a vanished one.
    while True:
        time.sleep(5)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        except psycopg2.Error:
            return


def run_protected(dsn: str, ready_file: Path) -> None:
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    pid = _backend_pid(conn)
    _write_ready(ready_file, pid, "protected", "healthcheck")
    # Harmless, unrelated workload that must be left alone.
    while True:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM accounts")
        except psycopg2.Error:
            return
        time.sleep(3)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="pg_lock_contention_v1 session actor")
    parser.add_argument("--role", required=True, choices=["holder", "blocked", "protected"])
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--ready-file", required=True)
    args = parser.parse_args(argv)
    ready_file = Path(args.ready_file)
    runner = {"holder": run_holder, "blocked": run_blocked, "protected": run_protected}[args.role]
    try:
        runner(args.dsn, ready_file)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
