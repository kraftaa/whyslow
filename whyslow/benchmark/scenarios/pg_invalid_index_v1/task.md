# Query regression after an interrupted index deployment

The recent-orders endpoint remains available but scans far more rows than
expected. The slowdown appeared after an index deployment was interrupted.
The representative SQL is in `query.sql`.

Investigate the live database and restore an efficient, durable access path.
Clean up unusable deployment artifacts without deleting or rewriting order
data, and do not restart PostgreSQL.

Write `result.md` with the root cause, evidence, remediation, validation, and
prevention recommendations. Connection details are in `ENV.md`.
