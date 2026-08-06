# Operating whyslow in production

Deploying the collectors, confirming they are actually collecting, and the
reliability/retention behavior. See [INSTALL.md](../INSTALL.md) for the
versioned install, checksum verification, and rollback.

## `whyslow status` / `whyslow doctor` — is this thing actually collecting?

The single most important command, because this tool can only explain
incidents **from the moment collectors started running**. If a collector
died three weeks ago, you do not want to discover that at 2am.

```bash
whyslow status
whyslow doctor
```

`whyslow doctor` checks SQLite integrity, WAL mode, schema shape, private
file permissions, free disk space, required collector health, and the
configured Postgres, Puma, and CloudWatch dependencies. Live credentials
come from `WHYSLOW_PG_DSN`, `WHYSLOW_PUMA_STATS_URL` /
`WHYSLOW_PUMA_TOKEN`, and `WHYSLOW_DB_CLUSTER_ID`, so they stay out of
process arguments and diagnostic output. Use `whyslow doctor --json` for
automation.

`explain`, `diff`, and `status` also support a stable, versioned JSON contract:

```bash
whyslow --last 15m --json > incident.json
whyslow diff --last 15m --baseline-last 15m --json
whyslow status --json
```

The JSON form preserves exact windows, coverage gaps, contributors, named
signals, blocking remediation fields, and CloudWatch source-instance
provenance without requiring automation to parse terminal prose. Set-like
values are sorted and explicit-window reports are deterministic. Compatibility
rules and field descriptions are documented in [JSON_OUTPUT.md](JSON_OUTPUT.md).

Aurora mode requires IAM permissions for `rds:DescribeDBClusters` and
`cloudwatch:GetMetricStatistics`. `whyslow doctor` resolves the current
writer and fails its provenance check if the latest stored metrics still
refer to a different instance. `--db-instance-id` remains available for
standalone RDS, but deliberately warns that fixed-instance mode cannot follow
Aurora failovers.

CLI inputs fail closed: report windows are capped at 31 days, collector
intervals must be finite and between 0 and 3600 seconds, Puma URLs cannot
embed credentials or query-string tokens, identifiers are restricted to
safe operational characters, and event fields have bounded sizes.

The SQLite format has an explicit version with numbered transactional
migrations. Older and unversioned stores upgrade in place; a store from a
newer unsupported whyslow release is refused before journal, schema, or
permission mutation. `whyslow doctor` reports current and expected versions.

```
Collectors
  ✓ postgres                 alive  last heartbeat 1s ago  (interval=1.0s)
  ✗ puma:web-3               STALE  last heartbeat 4.2d ago

  WARNING: a stale collector means incidents during that gap
  cannot be explained. Check the process is still running.

Data coverage
  session_changes            2841 rows   2026-07-26 09:00:00Z -> 2026-07-28 11:47:00Z  (50.8h)
  blocking_edges               17 rows   2026-07-26 14:22:00Z -> 2026-07-28 11:44:00Z  (45.4h)
```

**Why heartbeats exist, rather than just counting rows:** collectors
write *diffs*, so a healthy collector watching a quiet database writes
**zero rows** — identical to a collector that died weeks ago. Row counts
cannot distinguish those two states; heartbeats can. This was a real
blind spot until it was found and fixed.

Exits non-zero if any collector is stale, so it works as a monitoring
check (cron, Nagios, a readiness probe), not just something read by eye.

## Deployment

`deploy/` contains systemd units for Postgres and CloudWatch plus a
templated Puma unit (`whyslow-collect-puma@web-3`). Run all of them on
the same collector host. Every unit invokes the explicit production
environment at `/opt/whyslow/venv/bin/whyslow`. Each Puma instance reads its target URL and
token from `/etc/whyslow/puma/<host>.env`.

Enable the independent retention and validated backup timers as well:

```bash
systemctl enable --now whyslow-prune.timer
systemctl enable --now whyslow-backup.timer
```

This runs `whyslow prune` hourly, so expired data is removed even if the
Postgres collector is unavailable. The backup timer creates a private,
integrity-checked online snapshot daily and retains the seven newest copies;
collectors do not need to stop. The collector units use
`Restart=always` with `StartLimitIntervalSec=0` — a collector that gives
up retrying is a collector that silently isn't there when it matters —
and read credentials from an `EnvironmentFile` rather than command-line
arguments, since a DSN passed as an argument is visible in `ps` output
to every user on the box. SQL string/numeric literals and comments are
removed before query evidence is stored, query text is capped at 2 KiB,
and the SQLite file is created with mode `0600`.

## Reliability, retention, and Puma coverage

Operational gaps closed after repeated audits:

- **Retention/pruning is implemented and tested** (`Store.prune()`,
  `whyslow prune`, and `tests/prune_smoke.py`) — session/Puma data ages
  out after 48h, blocking edges and CloudWatch metrics after 30 days.
  The systemd timer runs independently of collector health.
- **Heartbeat staleness uses each collector's configured interval.**
  A collector intentionally running every 30 seconds no longer gets
  judged against the one-second default; older stores retain safe
  role-based fallbacks.
- **The Postgres collector now reconnects with exponential backoff**
  instead of dying if the connection drops mid-poll — plausible
  exactly during a severe incident, which is the one moment this tool
  cannot afford to go silent.
- **A priming poll on every (re)start** establishes the "already seen"
  baseline without writing it, so a restart no longer reports every
  currently-active session as newly appeared.
- **The Puma collector had zero test coverage before this pass.**
  `tests/fake_puma.py` + `tests/puma_smoke.py` exercise both
  single-mode and clustered-mode parsing against a real HTTP server
  (no Ruby stack required). This also caught a real bug: clustered
  mode was *summing* stats across workers, which hides a single
  saturated worker among idle ones. Fixed to report the worst worker
  (max backlog, min pool_capacity) instead — verified with a
  synthetic case where summing and worst-worker aggregation disagree
  (sum: backlog=25, pool_capacity=27 -- both wrong; worst-worker:
  20 and 0 -- correct).
- **Machine-readable output is a versioned interface, not a dump of internal
  Python objects.** `tests/json_output_smoke.py` verifies the schema envelope,
  deterministic ordering, exact windows, health verdict, default command
  routing, and CloudWatch writer provenance.
- **Dependency maintenance is automated.** Dependabot checks both Python and
  GitHub Actions dependencies weekly, and CI actions use Node 24-compatible
  releases.
