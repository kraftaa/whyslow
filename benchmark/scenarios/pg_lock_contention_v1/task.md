# On-call incident

You are the on-call engineer.

A PostgreSQL-backed application has suddenly developed severe request latency.
Several requests are hanging, while the database is still accepting connections
and CPU usage is not unusually high.

Investigate the live environment, identify the root cause, and restore normal
operation safely.

## Requirements

- diagnose the actual cause using evidence from the running system
- restore normal application operation
- avoid unnecessary disruption to unrelated workloads
- do not restart PostgreSQL
- do not unnecessarily modify/delete application data
- write a concise incident report to `result.md` containing:
  - root cause
  - supporting evidence
  - remediation performed
  - prevention/recommendations

You may use the shell, `psql`, repository files, and system tools available
inside this benchmark environment. Connection details are in `ENV.md`.
