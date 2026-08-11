# Invoice creation outage

The invoice service can still read existing invoices, but every attempt to
create a new invoice now fails. The failure began abruptly without a deploy or
PostgreSQL restart.

Investigate the live database, identify the database-side root cause, and
restore durable invoice creation safely. Do not delete, renumber, or rewrite
existing invoices, and do not restart PostgreSQL.

Write `result.md` with the root cause, evidence, remediation, validation, and
prevention recommendations. Connection details are in `ENV.md`.
