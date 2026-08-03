import time
import re

import psycopg2

from .storage import Store
from .validation import validate_interval

# Query-text labels are purely cosmetic -- they make evidence readable
# ("reindex on orders" instead of "pid 512"). Detection itself never
# depends on these matching; it depends only on backend_type / category /
# blocking relationships, which cover unnamed jobs too.
MAINTENANCE_PATTERNS = [
    (re.compile(r"^\s*REINDEX", re.I), "reindex"),
    (re.compile(r"^\s*VACUUM\s+FULL", re.I), "vacuum full"),
    (re.compile(r"^\s*COPY\b", re.I), "bulk load"),
    (re.compile(r"^\s*CREATE INDEX(?!\s+CONCURRENTLY)", re.I), "index build"),
]


# Rails/ActiveRecord commonly prepends a comment for query tagging --
# the marginalia gem, or Rails 7+'s built-in query log tags -- e.g.
# "/*application:MyApp,controller:orders*/ REINDEX TABLE orders".
# Matching from literal string-start would silently miss these, which
# matters a lot here: this stack is Puma, i.e. very likely Rails.
_LEADING_COMMENT = re.compile(r"^\s*(/\*.*?\*/\s*|--[^\n]*\n?\s*)")
_DOLLAR_QUOTE_START = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$")
_NUMERIC_LITERAL = re.compile(
    r"(?<![\w.])[-+]?(?:"
    r"0[xX][0-9A-Fa-f_]+|"
    r"0[oO][0-7_]+|"
    r"0[bB][01_]+|"
    r"(?:\d[\d_]*(?:\.[\d_]*)?|\.[\d_]+)(?:[eE][-+]?\d[\d_]*)?"
    r")(?![\w.])"
)
_WHITESPACE = re.compile(r"\s+")
MAX_STORED_QUERY_CHARS = 2048


def _strip_leading_comments(s):
    while True:
        stripped = _LEADING_COMMENT.sub("", s, count=1)
        if stripped == s:
            return s
        s = stripped


def label_query(query):
    if not query:
        return None
    query = _strip_leading_comments(query)
    for pattern, label in MAINTENANCE_PATTERNS:
        if pattern.match(query):
            return label
    return None


def sanitize_query(query):
    """Keep diagnostic SQL structure while removing likely sensitive data.

    Query text can contain emails, tokens, payloads, request-tag comments,
    and numeric identifiers. The incident engine only needs statement shape,
    relation names, and maintenance keywords, so literals and comments are
    replaced before anything reaches SQLite.
    """
    if query is None:
        return None
    sanitized = _redact_sql_text(query)
    sanitized = _NUMERIC_LITERAL.sub("?", sanitized)
    sanitized = _WHITESPACE.sub(" ", sanitized).strip()
    return sanitized[:MAX_STORED_QUERY_CHARS]


def _redact_sql_text(query):
    """Redact PostgreSQL strings/comments with a conservative scanner.

    Regex replacement cannot safely handle E'backslash-escaped quotes',
    tagged dollar quotes, nested block comments, or malformed input. This
    scanner preserves SQL structure and quoted identifiers while replacing
    every literal body with one fixed marker. Unterminated literals/comments
    consume the rest of the query rather than risk retaining a secret tail.
    """
    output = []
    i = 0
    length = len(query)

    while i < length:
        # Line comments.
        if query.startswith("--", i):
            newline = query.find("\n", i + 2)
            output.append(" ")
            i = length if newline < 0 else newline + 1
            continue

        # PostgreSQL block comments can nest.
        if query.startswith("/*", i):
            depth = 1
            i += 2
            while i < length and depth:
                if query.startswith("/*", i):
                    depth += 1
                    i += 2
                elif query.startswith("*/", i):
                    depth -= 1
                    i += 2
                else:
                    i += 1
            output.append(" ")
            continue

        # Dollar-quoted strings: $$...$$ and $tag$...$tag$. Positional
        # parameters such as $1 deliberately do not match this grammar.
        dollar = _DOLLAR_QUOTE_START.match(query, i)
        if dollar:
            delimiter = dollar.group(0)
            close = query.find(delimiter, dollar.end())
            output.append("'?'")
            i = length if close < 0 else close + len(delimiter)
            continue

        # Quoted identifiers are structure, not values. Copy them intact,
        # including doubled quotes and comment-like text inside them.
        if query[i] == '"':
            start = i
            i += 1
            closed = False
            while i < length:
                if query[i] == '"':
                    if i + 1 < length and query[i + 1] == '"':
                        i += 2
                        continue
                    i += 1
                    closed = True
                    break
                i += 1
            output.append(query[start:i] if closed else '"?"')
            continue

        quote_index = None
        if query[i] == "'":
            quote_index = i
        elif (
            query[i] in "eEbBxXnN"
            and i + 1 < length
            and query[i + 1] == "'"
            and _token_boundary_before(query, i)
        ):
            quote_index = i + 1
        elif (
            query[i : i + 2].lower() == "u&"
            and i + 2 < length
            and query[i + 2] == "'"
            and _token_boundary_before(query, i)
        ):
            quote_index = i + 2

        if quote_index is not None:
            i = _single_quote_end(query, quote_index)
            output.append("'?'")
            continue

        output.append(query[i])
        i += 1

    return "".join(output)


def _token_boundary_before(query, index):
    if index == 0:
        return True
    previous = query[index - 1]
    return not (previous.isalnum() or previous in "_$")


def _single_quote_end(query, quote_index):
    """Index immediately after a string, or EOF when it is unterminated."""
    i = quote_index + 1
    while i < len(query):
        if query[i] == "\\":
            # Treat backslash as an escape even for ordinary strings. That
            # is required for E'' and conservative if a database has
            # standard_conforming_strings disabled.
            i += 2
            continue
        if query[i] == "'":
            if i + 1 < len(query) and query[i + 1] == "'":
                i += 2
                continue
            return i + 1
        i += 1
    return len(query)


SESSIONS_SQL = """
SELECT
    pid,
    backend_type,
    usename,
    application_name,
    state,
    CASE
        WHEN wait_event_type = 'Lock' THEN 'lock'
        WHEN wait_event_type = 'IO'   THEN 'io'
        WHEN wait_event IS NULL AND state = 'active' THEN 'cpu'
        ELSE 'other'
    END AS category,
    query
FROM pg_stat_activity
WHERE state IS DISTINCT FROM 'idle'
  AND pid <> pg_backend_pid();
"""

BLOCKING_SQL = """
SELECT
    blocked.pid AS blocked_pid,
    blocked.application_name AS blocked_app,
    blocking.pid AS blocking_pid,
    blocking.application_name AS blocking_app,
    blocking.usename AS blocking_usename,
    blocking.query AS blocking_query
FROM pg_stat_activity AS blocked
CROSS JOIN LATERAL unnest(pg_blocking_pids(blocked.pid)) AS blocking_pid(pid)
JOIN pg_stat_activity AS blocking ON blocking.pid = blocking_pid.pid
WHERE cardinality(pg_blocking_pids(blocked.pid)) > 0
  -- Only the true root blocker: a session blocking others but not itself
  -- waiting on anyone. Without this, Postgres's row-lock wait queue chains
  -- waiters behind each other, and pg_blocking_pids() returns every link
  -- in that chain -- an O(N^2) explosion of edges for N blocked sessions,
  -- and a misleading "many different blockers" report during exactly the
  -- incidents where there's really only one root cause.
  AND cardinality(pg_blocking_pids(blocking.pid)) = 0;
"""


class PostgresCollector:
    """Polls pg_stat_activity + blocking chains every `interval` seconds
    and writes diffs to the shared SQLite store. This is the core
    collector: because Aurora is fully managed, anything consuming
    DB CPU/IO or holding a lock -- dbt, a cronjob, REINDEX, autovacuum,
    something never seen before -- shows up here without a named
    integration."""

    def __init__(self, dsn, store: Store, interval=1.0):
        self.dsn = dsn
        self.store = store
        self.interval = validate_interval(interval)
        self._last_session_keys = set()
        self._last_blocking_keys = set()

    def _connect(self):
        return psycopg2.connect(self.dsn)

    def _detect_instance_role(self, conn):
        """'primary' or 'replica'. On Aurora, the cluster reader endpoint
        looks like the safe choice (read-only!) but write-lock contention
        happens on the writer -- a reader-connected collector would report
        'no blocking found' forever, with coverage looking perfect."""
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_is_in_recovery();")
                in_recovery = cur.fetchone()[0]
            return "replica" if in_recovery else "primary"
        except Exception:
            return None

    def poll_once(self, conn, prime_only=False):
        ts = time.time()
        with conn.cursor() as cur:
            cur.execute(SESSIONS_SQL)
            sessions = cur.fetchall()

            cur.execute(BLOCKING_SQL)
            edges = cur.fetchall()

        session_rows = []
        current_session_keys = set()
        for pid, backend_type, usename, app, state, category, query in sessions:
            safe_query = sanitize_query(query)
            key = (pid, state, category, safe_query)
            current_session_keys.add(key)
            if key not in self._last_session_keys:
                session_rows.append((pid, backend_type, usename, app, state, category, safe_query))
        self._last_session_keys = current_session_keys

        edge_rows = []
        current_edge_keys = set()
        last_blocking_keys_snapshot = self._last_blocking_keys
        for (
            blocked_pid,
            blocked_app,
            blocking_pid,
            blocking_app,
            blocking_user,
            blocking_query,
        ) in edges:
            safe_blocking_query = sanitize_query(blocking_query)
            key = (blocked_pid, blocking_pid)
            current_edge_keys.add(key)
            if key not in self._last_blocking_keys:
                edge_rows.append(
                    (
                        blocked_pid,
                        blocked_app,
                        blocking_pid,
                        blocking_app,
                        blocking_user,
                        safe_blocking_query,
                    )
                )
        self._last_blocking_keys = current_edge_keys

        # Edges that were present last poll and are gone now have resolved.
        # Recording this is what makes duration computable at all -- the
        # collector previously only ever recorded edge *appearance*.
        resolved_keys = last_blocking_keys_snapshot - current_edge_keys

        # Priming establishes the "already seen" baseline for SESSIONS on
        # startup/reconnect, so a restart doesn't report every already-active
        # session as newly appeared. It deliberately does NOT suppress
        # blocking edges: those are rare and always significant, and there
        # is no noise argument for hiding them. Suppressing them meant a
        # collector restarting *during* an incident recorded zero blocking
        # edges -- silently blind in exactly the situation it exists for.
        if not prime_only and session_rows:
            self.store.write_sessions(session_rows, ts=ts)
        if edge_rows:
            self.store.write_blocking_edges(edge_rows, ts=ts)
        if resolved_keys:
            self.store.mark_blocking_edges_ended(resolved_keys, ts=ts)

        if prime_only:
            return 0, len(edge_rows)
        return len(session_rows), len(edge_rows)

    def _prepare_for_connection(self, now=None):
        """Reset edge identity only after a real historical coverage gap.

        Keeping `_last_blocking_keys` through a brief reconnect lets the
        priming poll mark edges that disappeared while reconnecting. After a
        complete uncovered minute, however, continuity is unknowable and an
        edge still present must be recorded as a new episode.
        """
        now = time.time() if now is None else now
        postgres_heartbeat = next(
            (heartbeat for heartbeat in self.store.get_heartbeats() if heartbeat[0] == "postgres"),
            None,
        )
        if (
            postgres_heartbeat
            and self.store.collector_coverage_gap_after("postgres", postgres_heartbeat[1], now)
            is not None
        ):
            self._last_blocking_keys.clear()

    def run_forever(self, prune_every=3600):
        backoff = 1.0
        last_prune = time.time()
        conn = None
        while True:
            try:
                conn = self._connect()
                conn.autocommit = True
                instance_role = self._detect_instance_role(conn)
                print(f"[whyslow] postgres collector connected, polling every {self.interval}s")
                if instance_role == "replica":
                    print("[whyslow] " + "!" * 60)
                    print("[whyslow] WARNING: connected to a REPLICA / Aurora reader endpoint.")
                    print("[whyslow] Write-lock contention happens on the WRITER and will be")
                    print("[whyslow] INVISIBLE from here. This collector will report 'no")
                    print("[whyslow] blocking found' no matter what the primary is doing.")
                    print("[whyslow] Point the DSN at the cluster WRITER endpoint instead.")
                    print("[whyslow] " + "!" * 60)
                backoff = 1.0  # reset after a successful connect

                # Prime once so a (re)start doesn't report every already-active
                # session as newly appeared.
                self._prepare_for_connection()
                self.poll_once(conn, prime_only=True)

                while True:
                    n_sessions, n_edges = self.poll_once(conn)
                    # Always heartbeat, even when zero diff rows were written --
                    # that is the entire point: a quiet database must be
                    # distinguishable from a dead collector.
                    self.store.write_heartbeat(
                        "postgres",
                        detail=f"interval={self.interval}s",
                        instance_role=instance_role,
                        expected_interval=self.interval,
                    )
                    if n_sessions or n_edges:
                        print(f"[whyslow] +{n_sessions} session rows, +{n_edges} blocking edges")
                    if time.time() - last_prune > prune_every:
                        deleted = self.store.prune()
                        if any(deleted.values()):
                            print(f"[whyslow] pruned old rows: {deleted}")
                        last_prune = time.time()
                    time.sleep(self.interval)

            except KeyboardInterrupt:
                break
            except Exception as e:
                print(
                    f"[whyslow] postgres collector connection lost ({e}); "
                    f"reconnecting in {backoff:.0f}s"
                )
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
