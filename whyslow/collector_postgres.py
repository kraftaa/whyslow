import time
import re

import psycopg2

from .storage import Store

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
        self.interval = interval
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
            key = (pid, state, category, query)
            current_session_keys.add(key)
            if key not in self._last_session_keys:
                session_rows.append((pid, backend_type, usename, app, state, category, query))
        self._last_session_keys = current_session_keys

        edge_rows = []
        current_edge_keys = set()
        last_blocking_keys_snapshot = self._last_blocking_keys
        for blocked_pid, blocked_app, blocking_pid, blocking_app, blocking_user, blocking_query in edges:
            key = (blocked_pid, blocking_pid)
            current_edge_keys.add(key)
            if key not in self._last_blocking_keys:
                edge_rows.append(
                    (blocked_pid, blocked_app, blocking_pid, blocking_app, blocking_user, blocking_query)
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
                print(f"[whyslow] postgres collector connection lost ({e}); "
                      f"reconnecting in {backoff:.0f}s")
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
