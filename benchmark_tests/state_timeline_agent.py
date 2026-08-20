"""Deterministic external responder used by live state-timeline tests."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import time

import psycopg2


def _connect():
    return psycopg2.connect(
        host=os.environ["PGHOST"],
        port=os.environ["PGPORT"],
        dbname=os.environ["PGDATABASE"],
        user=os.environ["PGUSER"],
        password=os.environ["PGPASSWORD"],
        application_name="state_timeline_test_agent",
    )


def main() -> int:
    mode = sys.argv[1]
    result_path = Path(os.environ["WHYSLOW_BENCH_RESULT_PATH"])
    conn = _connect()
    try:
        with conn.cursor() as cur:
            if mode == "commit":
                cur.execute("GRANT SELECT ON account_reports TO whyslow_app")
                conn.commit()
            elif mode == "rollback":
                cur.execute("GRANT SELECT ON account_reports TO whyslow_app")
                conn.rollback()
            elif mode == "regress-recover":
                conn.autocommit = True
                cur.execute("GRANT SELECT ON account_reports TO whyslow_app")
                time.sleep(0.3)
                cur.execute("GRANT UPDATE ON account_reports TO whyslow_app")
                time.sleep(0.3)
                cur.execute("REVOKE UPDATE ON account_reports FROM whyslow_app")
                time.sleep(0.3)
            elif mode == "over-broad":
                conn.autocommit = True
                cur.execute("GRANT ALL ON account_reports TO whyslow_app")
            else:
                raise ValueError(f"unknown mode: {mode}")
    finally:
        conn.close()
    result_path.write_text("# Result\n\nDeterministic temporal instrumentation test.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
