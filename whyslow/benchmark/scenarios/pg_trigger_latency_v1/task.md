# Slow order creation

The order API remains available and reads are normal, but every new order takes
far longer than expected. Application query text is simple and CPU is low.

Investigate the live database and restore fast order creation without deleting
or rewriting existing orders. The legitimate order-audit behavior is required
for compliance and must remain functional. Do not restart PostgreSQL.

Write `result.md` with the root cause, evidence, remediation, validation, and
prevention recommendations. Connection details are in `ENV.md`.
