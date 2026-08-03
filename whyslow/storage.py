import sqlite3
import stat
import time
from pathlib import Path

from .validation import (
    MAX_EVENT_KIND_CHARS,
    MAX_EVENT_PAYLOAD_CHARS,
    MAX_EVENT_SOURCE_CHARS,
    validate_identifier,
    validate_text,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS session_changes (
    ts REAL NOT NULL,
    pid INTEGER NOT NULL,
    backend_type TEXT,
    usename TEXT,
    application_name TEXT,
    state TEXT,
    category TEXT,      -- lock / io / cpu / other
    query TEXT
);
CREATE INDEX IF NOT EXISTS idx_session_changes_ts ON session_changes(ts);

CREATE TABLE IF NOT EXISTS blocking_edges (
    ts REAL NOT NULL,
    blocked_pid INTEGER NOT NULL,
    blocked_app TEXT,
    blocking_pid INTEGER NOT NULL,
    blocking_app TEXT,
    blocking_usename TEXT,
    blocking_query TEXT,
    -- When the edge stopped being observed. NULL means either still
    -- active, or the collector stopped before it resolved. Without this,
    -- duration is uncomputable -- and "has this been blocking for 4
    -- seconds or 4 minutes?" is the whole kill-it-or-wait decision.
    ended_ts REAL
);
CREATE INDEX IF NOT EXISTS idx_blocking_edges_ts ON blocking_edges(ts);

CREATE TABLE IF NOT EXISTS puma_stats (
    ts REAL NOT NULL,
    host TEXT NOT NULL,
    backlog INTEGER,
    pool_capacity INTEGER,
    max_threads INTEGER,
    running INTEGER,
    rss_mb REAL
);
CREATE INDEX IF NOT EXISTS idx_puma_stats_ts ON puma_stats(ts);

CREATE TABLE IF NOT EXISTS cloudwatch_metrics (
    ts REAL NOT NULL,
    metric TEXT NOT NULL,
    value REAL
);
CREATE INDEX IF NOT EXISTS idx_cloudwatch_metrics_ts ON cloudwatch_metrics(ts);
CREATE INDEX IF NOT EXISTS idx_cloudwatch_metrics_metric_ts
    ON cloudwatch_metrics(metric, ts);

CREATE TABLE IF NOT EXISTS events (
    ts REAL NOT NULL,
    source TEXT NOT NULL,
    kind TEXT,
    payload TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);

-- Collectors write DIFFS: an idle period legitimately produces zero rows.
-- That makes "healthy collector, quiet database" and "collector died three
-- weeks ago" indistinguishable from the data alone -- a serious problem for
-- a tool whose whole value depends on already running before an incident.
-- Heartbeats disambiguate. Keyed by collector name, so this table never
-- grows and never needs pruning.
CREATE TABLE IF NOT EXISTS collector_heartbeats (
    collector TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    detail TEXT,
    -- The collector's configured cadence. Status uses this rather than
    -- assuming every Postgres/Puma collector runs at the default 1 second.
    expected_interval REAL,
    -- 'primary' | 'replica' | NULL. On Aurora, pointing the collector at
    -- the cluster READER endpoint is the safer-looking choice (read-only,
    -- no write risk) and is the wrong one: write-lock contention happens
    -- on the writer and is invisible from a reader. Recorded per heartbeat
    -- so `explain` can refuse to imply an all-clear from reader data,
    -- rather than relying on someone having noticed a startup log line.
    instance_role TEXT
);

-- Historical coverage, one row per collector per minute. The heartbeat
-- table above is upsert-only, so it answers "is it alive NOW?" but makes
-- past outages invisible -- and a window with no collector running looks
-- exactly like a window where nothing happened. Reporting "nothing found"
-- for an unwatched window is absence of evidence dressed up as evidence
-- of absence, which is the most dangerous output this tool could produce.
-- ~1440 rows/day/collector: negligible.
CREATE TABLE IF NOT EXISTS collector_coverage (
    collector TEXT NOT NULL,
    minute_bucket INTEGER NOT NULL,
    PRIMARY KEY (collector, minute_bucket)
);

-- Fleet membership is separate from heartbeats: removing a host should
-- stop future stale/coverage warnings without erasing its historical
-- evidence. A later heartbeat automatically reactivates it.
CREATE TABLE IF NOT EXISTS collector_memberships (
    collector TEXT NOT NULL,
    started_minute INTEGER NOT NULL,
    retired_minute INTEGER,
    PRIMARY KEY (collector, started_minute)
);
"""


RETENTION_SECONDS = {
    "session_changes": 48 * 3600,
    "puma_stats": 48 * 3600,
    "blocking_edges": 30 * 86400,
    "cloudwatch_metrics": 30 * 86400,
    # events: no automatic pruning -- volume is tiny, retained indefinitely
}


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.original_mode = None
        if str(self.path) != ":memory:" and self.path.exists():
            self.original_mode = stat.S_IMODE(self.path.stat().st_mode)
        self.conn = sqlite3.connect(str(self.path), timeout=30)
        if str(self.path) != ":memory:":
            self.path.chmod(0o600)
        self.conn.execute("PRAGMA busy_timeout=30000;")
        # WAL mode: readers and writers never block each other, unlike the
        # default rollback-journal mode. Enabling WAL itself needs a write
        # lock: simultaneous first opens exposed a startup race on Python
        # 3.9 where one process failed immediately with "database is locked".
        # Retry all initialization lock conflicts within the same 30-second
        # budget used for ordinary SQLite writes.
        self._retry_locked(
            lambda: self.conn.execute("PRAGMA journal_mode=WAL;").fetchone()
        )
        self._retry_locked(lambda: self.conn.executescript(SCHEMA))
        self._migrate()
        self.conn.commit()

    def _retry_locked(self, operation):
        deadline = time.monotonic() + 30
        while True:
            try:
                return operation()
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)

    def _migrate(self):
        """Add columns introduced after a database was first created.
        `CREATE TABLE IF NOT EXISTS` silently does nothing on an existing
        table, so without this an upgraded install would break on the
        first query referencing a new column."""
        expected = {
            "blocking_edges": {"ended_ts": "REAL"},
            "collector_heartbeats": {
                "instance_role": "TEXT",
                "expected_interval": "REAL",
            },
        }
        for table, columns in expected.items():
            existing = {
                row[1] for row in self.conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            for column, coltype in columns.items():
                if column not in existing:
                    try:
                        self._retry_locked(
                            lambda: self.conn.execute(
                                f"ALTER TABLE {table} ADD COLUMN {column} {coltype}"
                            )
                        )
                    except sqlite3.OperationalError as exc:
                        # Another process may have completed the same
                        # migration while this connection waited.
                        if "duplicate column" not in str(exc).lower():
                            raise
        # Backfill one initial membership interval for stores created before
        # explicit collector retirement existed.
        self.conn.execute(
            "INSERT OR IGNORE INTO collector_memberships "
            "(collector, started_minute, retired_minute) "
            "SELECT coverage.collector, min(coverage.minute_bucket), NULL "
            "FROM collector_coverage AS coverage "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM collector_memberships AS membership "
            "  WHERE membership.collector = coverage.collector"
            ") "
            "GROUP BY coverage.collector"
        )
        self.conn.commit()

    # ---- writes (collectors call these) ----

    def write_sessions(self, rows, ts=None):
        ts = ts or time.time()
        self.conn.executemany(
            "INSERT INTO session_changes "
            "(ts, pid, backend_type, usename, application_name, state, category, query) "
            "VALUES (?,?,?,?,?,?,?,?)",
            [(ts, *r) for r in rows],
        )
        self.conn.commit()

    def write_blocking_edges(self, rows, ts=None):
        ts = ts or time.time()
        for row in rows:
            blocked_pid, blocked_app, blocking_pid, blocking_app, blocking_user, blocking_query = row
            existing = self.conn.execute(
                "SELECT ts FROM blocking_edges "
                "WHERE blocked_pid = ? AND blocking_pid = ? AND ended_ts IS NULL "
                "ORDER BY ts DESC LIMIT 1",
                (blocked_pid, blocking_pid),
            ).fetchone()
            # A reconnect primes active blocking edges again. Preserve the
            # original start when coverage was continuous, but create a new
            # episode after a real observation gap (the old edge's resolution
            # is unknowable and must not swallow a later PID-reuse episode).
            if existing and self.collector_coverage_gap_after(
                "postgres", existing[0], ts
            ) is None:
                continue
            self.conn.execute(
                "INSERT INTO blocking_edges "
                "(ts, blocked_pid, blocked_app, blocking_pid, blocking_app, "
                "blocking_usename, blocking_query) VALUES (?,?,?,?,?,?,?)",
                (ts, *row),
            )
        self.conn.commit()

    def write_puma_stat(self, host, backlog, pool_capacity, max_threads, running, rss_mb, ts=None):
        ts = ts or time.time()
        self.conn.execute(
            "INSERT INTO puma_stats (ts, host, backlog, pool_capacity, max_threads, running, rss_mb) "
            "VALUES (?,?,?,?,?,?,?)",
            (ts, host, backlog, pool_capacity, max_threads, running, rss_mb),
        )
        self.conn.commit()

    def write_cloudwatch_metric(self, metric, value, ts=None):
        ts = ts or time.time()
        # CloudWatch queries overlap to tolerate publication lag, so the
        # same datapoint can be returned by consecutive polls. Replace any
        # prior copy instead of growing duplicates or counting it twice.
        self.conn.execute(
            "DELETE FROM cloudwatch_metrics WHERE metric = ? AND ts = ?",
            (metric, ts),
        )
        self.conn.execute(
            "INSERT INTO cloudwatch_metrics (ts, metric, value) VALUES (?,?,?)",
            (ts, metric, value),
        )
        self.conn.commit()

    def write_event(self, source, kind, payload, ts=None):
        source = validate_identifier(
            validate_text(
                source,
                "event source",
                MAX_EVENT_SOURCE_CHARS,
                required=True,
            ),
            "event source",
        )
        kind = validate_text(
            kind,
            "event kind",
            MAX_EVENT_KIND_CHARS,
            required=kind is not None,
        )
        payload = validate_text(
            payload,
            "event payload",
            MAX_EVENT_PAYLOAD_CHARS,
        )
        ts = ts or time.time()
        self.conn.execute(
            "INSERT INTO events (ts, source, kind, payload) VALUES (?,?,?,?)",
            (ts, source, kind, payload),
        )
        self.conn.commit()

    # ---- windowed reads (explain engine calls these) ----

    def sessions_in(self, start_ts, end_ts):
        return self.conn.execute(
            "SELECT ts, pid, backend_type, usename, application_name, state, category, query "
            "FROM session_changes WHERE ts BETWEEN ? AND ? ORDER BY ts",
            (start_ts, end_ts),
        ).fetchall()

    def mark_blocking_edges_ended(self, edge_keys, ts=None):
        """Mark (blocked_pid, blocking_pid) pairs as no longer observed.
        Updates the most recent unresolved row for each pair."""
        ts = ts or time.time()
        for blocked_pid, blocking_pid in edge_keys:
            self.conn.execute(
                "UPDATE blocking_edges SET ended_ts = ? "
                "WHERE rowid = ("
                "  SELECT rowid FROM blocking_edges "
                "  WHERE blocked_pid = ? AND blocking_pid = ? AND ended_ts IS NULL "
                "  ORDER BY ts DESC LIMIT 1"
                ")",
                (ts, blocked_pid, blocking_pid),
            )
        self.conn.commit()

    def sessions_aggregated_in(self, start_ts, end_ts):
        """Per-minute, per-category session counts.

        Sessions are the only high-volume table: on a busy database the diff
        key churns several times a second, so 48h can hold ~900k rows.
        Loading those into Python cost 733 MB of RAM for a single explain --
        a real OOM risk on a small collector host. Aggregating in SQL bounds
        this to (minutes x categories) rows regardless of window size, and
        nothing is lost, because individual session rows were never displayed
        or used individually anyway -- only counted and checked for temporal
        proximity.
        """
        return self.conn.execute(
            "SELECT CAST(ts/60 AS INTEGER) AS minute_bucket, category, "
            "count(*) AS n, min(ts) AS first_ts, max(ts) AS last_ts "
            "FROM session_changes WHERE ts BETWEEN ? AND ? "
            "GROUP BY minute_bucket, category ORDER BY minute_bucket",
            (start_ts, end_ts),
        ).fetchall()

    def top_session_queries_in(self, start_ts, end_ts, limit=5):
        """Most frequent queries in the window, for context. Bounded by
        `limit`, so it stays cheap on a wide window."""
        return self.conn.execute(
            "SELECT query, usename, category, count(*) AS n "
            "FROM session_changes WHERE ts BETWEEN ? AND ? AND query IS NOT NULL "
            "GROUP BY query, usename, category ORDER BY n DESC LIMIT ?",
            (start_ts, end_ts, limit),
        ).fetchall()

    def blocking_edges_in(self, start_ts, end_ts):
        """Blocking intervals that overlap the requested window.

        Filtering only on the edge's start timestamp hides a long-running
        block from every window after the one in which it began.
        """
        return self.conn.execute(
            "SELECT ts, blocked_pid, blocked_app, blocking_pid, blocking_app, "
            "blocking_usename, blocking_query, ended_ts "
            "FROM blocking_edges "
            "WHERE ts <= ? AND (ended_ts IS NULL OR ended_ts >= ?) "
            "ORDER BY ts",
            (end_ts, start_ts),
        ).fetchall()

    def puma_stats_in(self, start_ts, end_ts):
        return self.conn.execute(
            "SELECT ts, host, backlog, pool_capacity, max_threads, running, rss_mb "
            "FROM puma_stats WHERE ts BETWEEN ? AND ? ORDER BY ts",
            (start_ts, end_ts),
        ).fetchall()

    def cloudwatch_metrics_in(self, start_ts, end_ts):
        return self.conn.execute(
            "SELECT ts, metric, value FROM cloudwatch_metrics WHERE ts BETWEEN ? AND ? ORDER BY ts",
            (start_ts, end_ts),
        ).fetchall()

    def events_in(self, start_ts, end_ts):
        return self.conn.execute(
            "SELECT ts, source, kind, payload FROM events WHERE ts BETWEEN ? AND ? ORDER BY ts",
            (start_ts, end_ts),
        ).fetchall()

    def write_heartbeat(
        self,
        collector,
        detail=None,
        ts=None,
        instance_role=None,
        expected_interval=None,
    ):
        ts = ts or time.time()
        self.conn.execute(
            "INSERT INTO collector_heartbeats "
            "(collector, ts, detail, instance_role, expected_interval) "
            "VALUES (?,?,?,?,?) "
            "ON CONFLICT(collector) DO UPDATE SET ts=excluded.ts, detail=excluded.detail, "
            "instance_role=excluded.instance_role, "
            "expected_interval=excluded.expected_interval",
            (collector, ts, detail, instance_role, expected_interval),
        )
        # Also record historical coverage so past gaps stay visible.
        self.conn.execute(
            "INSERT OR IGNORE INTO collector_coverage (collector, minute_bucket) VALUES (?,?)",
            (collector, int(ts // 60)),
        )
        minute = int(ts // 60)
        active_membership = self.conn.execute(
            "SELECT 1 FROM collector_memberships "
            "WHERE collector = ? AND retired_minute IS NULL LIMIT 1",
            (collector,),
        ).fetchone()
        if active_membership is None:
            latest_retired = self.conn.execute(
                "SELECT max(retired_minute) FROM collector_memberships "
                "WHERE collector = ?",
                (collector,),
            ).fetchone()[0]
            # A delayed historical heartbeat from inside a retired period
            # must not reactivate the collector in the present.
            if latest_retired is None or minute >= latest_retired:
                self.conn.execute(
                    "INSERT OR IGNORE INTO collector_memberships "
                    "(collector, started_minute, retired_minute) VALUES (?,?,NULL)",
                    (collector, minute),
                )
        self.conn.commit()

    def retire_collector(self, collector, ts=None):
        """Retire a known collector without deleting historical evidence."""
        ts = ts or time.time()
        minute = int(ts // 60)
        cur = self.conn.execute(
            "UPDATE collector_memberships SET retired_minute = ? "
            "WHERE collector = ? AND retired_minute IS NULL "
            "AND started_minute <= ?",
            (minute, collector, minute),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def coverage_in(self, start_ts, end_ts):
        """Per-collector coverage across a window.

        Deliberately NOT a single aggregate number. Each collector answers a
        different question -- Postgres gives blocking chains and session
        categories, Puma gives app saturation, CloudWatch gives instance
        metrics -- so "some collector was running" cannot validate a specific
        conclusion. Counting any-collector coverage as full coverage was a
        real bug: a live Puma collector made a dead Postgres collector look
        like full coverage, which let "no blocking found" print confidently
        when nothing had ever been watching for blocking.
        """
        start_bucket = int(start_ts // 60)
        end_bucket = int(end_ts // 60)
        total = max(end_bucket - start_bucket + 1, 1)

        per_collector = dict(self.conn.execute(
            "SELECT collector, count(*) FROM collector_coverage "
            "WHERE minute_bucket BETWEEN ? AND ? GROUP BY collector",
            (start_bucket, end_bucket),
        ).fetchall())
        membership_rows = self.conn.execute(
            "SELECT collector, started_minute, retired_minute "
            "FROM collector_memberships ORDER BY collector, started_minute"
        ).fetchall()
        memberships = {}
        for name, started_minute, retired_minute in membership_rows:
            memberships.setdefault(name, []).append(
                {
                    "started_minute": started_minute,
                    "retired_minute": retired_minute,
                }
            )

        expected_by_collector = {}
        for name, periods in memberships.items():
            expected = 0
            for period in periods:
                period_start = max(start_bucket, period["started_minute"])
                # Retirement is exclusive: that minute is the first minute
                # in which the collector is no longer expected.
                period_end = min(
                    end_bucket,
                    period["retired_minute"] - 1
                    if period["retired_minute"] is not None
                    else end_bucket,
                )
                expected += max(period_end - period_start + 1, 0)
            if expected:
                expected_by_collector[name] = expected

        details = {}
        for name, count in per_collector.items():
            expected = expected_by_collector.get(name, total)
            details[name] = {
                "minutes": count,
                "expected_minutes": expected,
                "fraction": min(count / expected, 1.0),
            }

        return {
            "total_minutes": total,
            "per_collector": details,
            "expected_collectors": {
                name: {"expected_minutes": expected}
                for name, expected in expected_by_collector.items()
            },
        }

    def collector_coverage_gap_after(self, collector, start_ts, end_ts):
        """Start timestamp of the first complete uncovered minute.

        Returns None when coverage is continuous, or when the starting
        minute itself has no coverage (synthetic/imported evidence cannot be
        bounded safely). The current partial minute is never called a gap.
        """
        start_bucket = int(start_ts // 60)
        last_complete_bucket = int(end_ts // 60) - 1
        if last_complete_bucket < start_bucket:
            return None
        buckets = {
            row[0]
            for row in self.conn.execute(
                "SELECT minute_bucket FROM collector_coverage "
                "WHERE collector = ? AND minute_bucket BETWEEN ? AND ?",
                (collector, start_bucket, last_complete_bucket),
            ).fetchall()
        }
        if start_bucket not in buckets:
            return None
        for bucket in range(start_bucket, last_complete_bucket + 1):
            if bucket not in buckets:
                return bucket * 60
        return None

    def get_heartbeats(self):
        return self.conn.execute(
            "SELECT collector, ts, detail, instance_role, expected_interval "
            "FROM collector_heartbeats "
            "ORDER BY collector"
        ).fetchall()

    def get_collector_memberships(self):
        return self.conn.execute(
            "SELECT collector, started_minute, retired_minute "
            "FROM collector_memberships ORDER BY collector, started_minute"
        ).fetchall()

    def data_coverage(self):
        """Earliest/latest timestamp and row count per data table -- used by
        `whyslow status` to show what windows are actually explainable."""
        coverage = {}
        for table in ("session_changes", "blocking_edges", "puma_stats",
                       "cloudwatch_metrics", "events"):
            row = self.conn.execute(
                f"SELECT min(ts), max(ts), count(*) FROM {table}"
            ).fetchone()
            coverage[table] = {"min_ts": row[0], "max_ts": row[1], "count": row[2]}
        return coverage

    def prune(self, now=None):
        """Delete rows older than each table's retention window. Cheap and
        safe to call frequently -- collectors call this periodically, not
        on every poll."""
        now = now or time.time()
        deleted = {}
        for table, retention in RETENTION_SECONDS.items():
            cutoff = now - retention
            cur = self.conn.execute(f"DELETE FROM {table} WHERE ts < ?", (cutoff,))
            deleted[table] = cur.rowcount
        # collector_coverage is bucketed by minute, not by a ts column, so
        # it needs its own prune. Kept as long as blocking_edges, since
        # coverage is what makes an old explain result trustworthy.
        coverage_cutoff = int((now - RETENTION_SECONDS["blocking_edges"]) // 60)
        cur = self.conn.execute(
            "DELETE FROM collector_coverage WHERE minute_bucket < ?", (coverage_cutoff,)
        )
        deleted["collector_coverage"] = cur.rowcount
        self.conn.commit()
        return deleted

    def close(self):
        self.conn.close()
