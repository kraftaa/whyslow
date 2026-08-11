"""Detached connection actors for the connection-exhaustion scenarios."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import psycopg2


def _write_ready(path: Path, conn, role: str) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT pg_backend_pid(), current_setting('application_name')")
        backend_pid, application_name = cur.fetchone()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(
            {
                "role": role,
                "application_name": application_name,
                "backend_pid": int(backend_pid),
                "os_pid": os.getpid(),
            }
        )
    )
    temporary.replace(path)


def run(dsn: str, ready_file: Path, role: str) -> None:
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    _write_ready(ready_file, conn, role)
    while True:
        time.sleep(3)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        except psycopg2.Error:
            return


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", required=True, choices=["leaked", "protected"])
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--ready-file", required=True)
    args = parser.parse_args(argv)
    try:
        run(args.dsn, Path(args.ready_file), args.role)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
