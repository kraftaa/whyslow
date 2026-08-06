# Using whyslow

Running the collectors, querying an incident window, recording deploy/job
markers, and diffing a healthy baseline against an incident. For installation
see [INSTALL.md](../INSTALL.md); during a live incident see
[RUNBOOK.md](../RUNBOOK.md).

## Setup: tag connections by host

**Do this first, independent of installing anything** — tag DB
connections by host so activity is attributable:

```yaml
# config/database.yml
production:
  application_name: <%= "web-#{Socket.gethostname}" %>
```

Production installs use the tested wheel from a private GitHub release in a
versioned environment under `/opt/whyslow`; see [INSTALL.md](INSTALL.md) for
checksum verification, systemd setup, upgrades, and rollback. Every release
dependency tree is vulnerability-audited before publication and ships with a
checksummed CycloneDX JSON SBOM. Workflow actions are pinned to immutable
commits.

For development from a checkout:

```bash
# Python 3.10+
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[test,quality]"
.venv/bin/whyslow --version
.venv/bin/ruff check whyslow tests
.venv/bin/ruff format --check whyslow
```

CI records package coverage across the behavioral suite and its subprocesses,
fails below 80%, and uploads `coverage.xml` for both supported Python versions.

Run every collector on one collector host and point every process at the
same SQLite file. The Puma control endpoints must be reachable from that
host over a private TLS connection or tunnel; running collectors
independently on each web server creates isolated stores that cannot be
correlated.

Use long-lived processes (systemd unit, supervisor, whatever you already
use):

```bash
export WHYSLOW_PG_DSN="postgresql://user:pass@host/db"
export WHYSLOW_PUMA_TOKEN="replace-me"
export WHYSLOW_DB_CLUSTER_ID="my-aurora-cluster"

whyslow collect-pg    --db /var/lib/whyslow/store.sqlite3
whyslow collect-puma  --host-name web-3 --stats-url https://web-3.internal:9293/stats --db /var/lib/whyslow/store.sqlite3
whyslow collect-cw    --db-cluster-id "$WHYSLOW_DB_CLUSTER_ID" --db /var/lib/whyslow/store.sqlite3
```

`--dsn` and `--token` remain available for local testing, but environment
variables keep secrets out of process arguments in production.

Then, after (or during) an incident:

```bash
whyslow --from 11:42 --to 11:47
```

Times accept ISO-8601 (`2026-07-30T23:55:00Z`), `HH:MM`,
`HH:MM:SS`, or raw epoch seconds. Clock-only windows automatically roll
across UTC midnight when `--to` is earlier than `--from`.

## Recording deploys and job markers

```bash
# one line at the end of your deploy pipeline
whyslow event --source deploy --kind "v1.2.3 released" --payload "sha=$GIT_SHA"

# or from a dbt/Airflow wrapper
whyslow event --source dbt --kind "nightly_rollup started"
```

These appear on the timeline (`11:42:03  deploy event: v1.2.3 released`)
**and** count as a correlation signal when they land close in time to
the pressure -- within 60s, since a deploy or batch job can take a
minute to manifest as database load.

This closes a loop that was dangling: the `events` table was read and
rendered from the very first version, but had **no writer at all** and
never influenced any signal -- so the spec's own example output
("Deployment detected") and its "overlaps a dbt run window" signal were
both unreachable in practice. Found by grepping for callers of
`write_event` and finding none.

## Relative time windows

Computing exact UTC timestamps by hand during an incident is real
friction, so `--last` is supported everywhere a window is:

```bash
whyslow --last 15m
whyslow diff --last 15m --baseline-last 15m   # baseline = the 15m just before
```

`--from`/`--to` still work (always UTC — see the timezone fix in
[AUDIT_LOG.md](../AUDIT_LOG.md)).

## `whyslow diff` — a healthy baseline vs the incident window

```bash
whyslow diff --baseline-from 11:30 --baseline-to 11:35 --from 11:42 --to 11:47
```

Deliberately narrower than `explain`: no signals, no confidence, no
inferred cause — just counts and set differences (roles/apps/
maintenance-query-patterns that appeared or disappeared, blocking edge
count, max Puma backlog, max CloudWatch CPU). Safe to add without
touching the causality question, because it never claims one.

Verified in `tests/diff_smoke.py`: a quiet baseline vs. the REINDEX
scenario correctly shows `analytics_role` and `reindex` as newly
appeared and blocking edges going 0 -> 1.
