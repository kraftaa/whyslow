# Checkout connection exhaustion

New checkout requests cannot connect to PostgreSQL, while administrative access
and the health endpoint still work. Investigate live session and role evidence,
identify why application capacity is exhausted, and restore a safe amount of
headroom.

Use the least disruptive remediation. Preserve the unrelated healthcheck,
application data, and PostgreSQL process. Write `result.md` with root cause,
evidence, remediation, validation, and prevention recommendations. Connection
details are in `ENV.md`.
