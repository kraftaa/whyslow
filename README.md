# whyslow

**Why is it slow?** Deterministic, evidence-first incident reconstruction for
Aurora Postgres + Puma — not another monitoring dashboard.

A CLI that reconstructs *why* web servers slowed down, from data already being
collected — instead of a team hand-correlating Puma stats, CloudWatch, and
`pg_stat_activity` by eye at 2am.

```
whyslow --from 11:42 --to 11:47      # or: whyslow --last 15m
```

Install the `whyslow-db` distribution; the command remains `whyslow`:

```bash
pipx install whyslow-db
# or: python -m pip install whyslow-db
```

The package also includes reproducible PostgreSQL incidents for evaluating AI
agents and humans:

```bash
whyslow benchmark list
whyslow benchmark setup pg_lock_contention_v1
```

See **[whyslow/benchmark/README.md](whyslow/benchmark/README.md)** for the
setup → act → evaluate → reset workflow and security scenarios.

**During an incident, go straight to [RUNBOOK.md](RUNBOOK.md)** — what to type,
and what each answer means.

## Why this exists

"Production is slow" usually collapses into one of a few root causes — a
Postgres lock chain, an app-server thread pool pinned on slow queries, or a
resource-contention event (a reindex, a bulk load, autovacuum, a cronjob —
anything sharing the DB instance's CPU/IO). This correlates the three places
you'd otherwise check by hand and reconstructs the incident window into one
plain-English timeline.

## How it works

No model, no statistics, no scoring formula — every conclusion is a lookup
against rows the collectors already wrote. Three collectors (Postgres, Puma,
CloudWatch) write to one SQLite file on a timer; `explain` reconstructs any
past window from that stored data, names the contributors, and shows the exact
evidence lines behind each one. Every tunable lives in plain sight at the top
of `explain.py`.

See **[docs/design.md](docs/design.md)** for the six-step mechanism, why it
needs no named integrations, and the evidence that it works.

## Quickstart

Tag DB connections by host so activity is attributable:

```yaml
# config/database.yml
production:
  application_name: <%= "web-#{Socket.gethostname}" %>
```

Run the collectors as long-lived processes on one collector host, all pointed
at the same SQLite file:

```bash
export WHYSLOW_PG_DSN="postgresql://user:pass@host/db"
export WHYSLOW_DB_CLUSTER_ID="my-aurora-cluster"

whyslow collect-pg   --db /var/lib/whyslow/store.sqlite3
whyslow collect-puma --host-name web-3 --stats-url https://web-3.internal:9293/stats --db /var/lib/whyslow/store.sqlite3
whyslow collect-cw   --db-cluster-id "$WHYSLOW_DB_CLUSTER_ID" --db /var/lib/whyslow/store.sqlite3
```

Then, after (or during) an incident:

```bash
whyslow --last 15m
whyslow status          # is everything actually collecting?
```

Production install (versioned wheel, checksum verification, systemd, rollback)
is in **[INSTALL.md](INSTALL.md)**. Full setup, querying, events, and `diff`
are in **[docs/usage.md](docs/usage.md)**.

## Honest limits

- **Only reconstructs incidents from the moment collectors were running.** It
  cannot retroactively explain anything from before install — that is the cost
  of a self-hosted collector with no vendor lock-in, not a bug to engineer away.
- **1-second polling can miss sub-second blocking events.**
- **Confidence is a named-signal count, not a statistical or causal
  guarantee.** Two unrelated things co-occurring can still produce a
  Medium/High label — read the Evidence section, not just the label.
- **Sanitized query structure is still operational data.** Literal values and
  comments are stripped, but statement types and relation names remain. Treat
  the mode-`0600` SQLite store as sensitive.
- **The CloudWatch collector is real code but not yet tested against a live AWS
  account.** Everything else is demonstrated against a real running Postgres.

## Documentation

- **[RUNBOOK.md](RUNBOOK.md)** — what to type during an incident, and what each answer means
- **[INSTALL.md](INSTALL.md)** — versioned production install, checksums, systemd, rollback
- **[docs/usage.md](docs/usage.md)** — running collectors, querying, events, `diff`
- **[docs/operations.md](docs/operations.md)** — `status`/`doctor`, deployment, reliability & retention
- **[docs/design.md](docs/design.md)** — how it works, why no integrations, evidence, scope
- **[docs/publishing.md](docs/publishing.md)** — PyPI Trusted Publishing and release procedure
- **[JSON_OUTPUT.md](JSON_OUTPUT.md)** — the stable, versioned JSON contract
- **[AUDIT_LOG.md](AUDIT_LOG.md)** — bugs found by repeated audits, round by round
- **[writing/](writing/)** — the four most transferable findings, written up as standalone posts
