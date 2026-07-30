import os
import shutil
import stat
import time

from whyslow.collector_postgres import (
    MAX_STORED_QUERY_CHARS,
    PostgresCollector,
    label_query,
    sanitize_query,
)
from whyslow.storage import Store


DB_PATH = "/tmp/query_privacy_smoke/store.sqlite3"
shutil.rmtree("/tmp/query_privacy_smoke", ignore_errors=True)

session_query = """
/* request_id=secret-request-token */
SELECT * FROM users
WHERE email = 'alice@example.com'
  AND account_id = 987654
  AND api_token = $$top-secret-token$$
"""
blocking_query = """
-- customer=private-customer
REINDEX TABLE orders_2026 /* bearer=secret-bearer */
"""


class FakeCursor:
    def __init__(self):
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def execute(self, sql):
        if "blocked.pid AS blocked_pid" in sql:
            self.rows = [
                (
                    512,
                    "web-3",
                    210,
                    "batch",
                    "analytics_role",
                    blocking_query,
                )
            ]
        else:
            self.rows = [
                (
                    301,
                    "client backend",
                    "app",
                    "web-1",
                    "active",
                    "cpu",
                    session_query,
                )
            ]

    def fetchall(self):
        return self.rows


class FakeConnection:
    def cursor(self):
        return FakeCursor()


store = Store(DB_PATH)
collector = PostgresCollector("unused", store)
collector.poll_once(FakeConnection())

stored_session_query = store.sessions_in(0, time.time() + 1)[0][7]
stored_blocking_query = store.blocking_edges_in(0, time.time() + 1)[0][6]
combined = stored_session_query + stored_blocking_query

for secret in (
    "alice@example.com",
    "987654",
    "top-secret-token",
    "secret-request-token",
    "private-customer",
    "secret-bearer",
):
    assert secret not in combined, f"sensitive query value reached SQLite: {secret}"

assert "SELECT * FROM users" in stored_session_query
assert "account_id = ?" in stored_session_query
assert label_query(stored_blocking_query) == "reindex"
assert len(sanitize_query("SELECT '" + ("x" * 5000) + "'")) <= MAX_STORED_QUERY_CHARS

# PostgreSQL-specific literal forms that regex-only redaction commonly gets
# wrong, especially an escaped quote followed by a sensitive suffix.
adversarial_query = r"""
SELECT
  E'api\'key-secret' AS escaped,
  U&'unicode-secret-\0061' AS unicode_value,
  B'101010-secret' AS bits,
  X'deadbeef-secret' AS hexes,
  $$simple-dollar-secret$$ AS simple_dollar,
  $payload$tagged-dollar-secret$payload$ AS tagged_dollar,
  'doubled-quote-''secret' AS ordinary
FROM "table--name"
/* outer-secret /* nested-secret */ still-secret */
WHERE id = 123456
  AND hex_id = 0xDEAD_BEEF
  AND binary_id = 0b1010_0101
  AND grouped_id = 987_654_321
"""
adversarial_sanitized = sanitize_query(adversarial_query)
for secret in (
    "key-secret",
    "unicode-secret",
    "101010-secret",
    "deadbeef-secret",
    "simple-dollar-secret",
    "tagged-dollar-secret",
    "doubled-quote",
    "outer-secret",
    "nested-secret",
    "still-secret",
    "123456",
    "DEAD_BEEF",
    "1010_0101",
    "987_654_321",
):
    assert secret not in adversarial_sanitized, (
        f"PostgreSQL literal/comment leaked through sanitization: {secret}"
    )
assert '"table--name"' in adversarial_sanitized, (
    "comment markers inside a quoted identifier must remain SQL structure"
)
assert adversarial_sanitized.count("'?'") == 7, adversarial_sanitized

for malformed in (
    "SELECT E'unterminated-secret",
    "SELECT $tag$unterminated-dollar-secret",
    "SELECT 1 /* unterminated-comment-secret",
    'SELECT "unterminated-identifier-secret',
):
    safe = sanitize_query(malformed)
    assert "secret" not in safe, f"malformed SQL leaked its sensitive tail: {safe}"

mode = stat.S_IMODE(os.stat(DB_PATH).st_mode)
assert mode == 0o600, f"SQLite evidence file must be private, got mode {oct(mode)}"

store.close()
print("PASS: query literals/comments are sanitized and the evidence store is mode 0600")
