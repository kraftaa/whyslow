"""Deterministic responder used to integration-test the trajectory runner."""

from __future__ import annotations

import os
from pathlib import Path

import psycopg2


def main() -> int:
    conn = psycopg2.connect(
        host=os.environ["PGHOST"],
        port=os.environ["PGPORT"],
        dbname=os.environ["PGDATABASE"],
        user=os.environ["PGUSER"],
        password=os.environ["PGPASSWORD"],
        application_name="trajectory_test_agent",
    )
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE INDEX events_tenant_occurred_idx ON events (tenant_id, occurred_at DESC)"
            )
            cur.execute("ANALYZE events")
    finally:
        conn.close()
    Path("result.md").write_text(
        "# Incident report\n\n"
        "Root cause: the tenant-history query lacked a composite index.\n\n"
        "Evidence: EXPLAIN showed a sequential scan and explicit sort.\n\n"
        "Remediation: added an index on tenant_id and occurred_at.\n\n"
        "Validation: the query now uses the index and event data is intact.\n"
    )
    print("agent: created targeted index and wrote result.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
