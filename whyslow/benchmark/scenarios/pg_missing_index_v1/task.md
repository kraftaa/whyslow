# Slow tenant-history query

The customer event-history endpoint has regressed and now scans far more data
than expected. The representative SQL is in `query.sql`.

Investigate the live database, explain the query plan, and restore efficient
query execution without deleting or rewriting event data and without restarting
PostgreSQL. Prefer a durable, narrowly targeted database change.

Write `result.md` with the root cause, evidence, remediation, validation, and
prevention recommendations. Connection details are in `ENV.md`.
