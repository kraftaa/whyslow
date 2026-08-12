# whyslow

**Why is it slow — and did the agent fix it safely?**

Whyslow is an evidence-first PostgreSQL toolkit with two related jobs:

1. reconstruct why an Aurora PostgreSQL + Puma service slowed down, using data
   already collected; and
2. evaluate whether an AI agent or human repaired a disposable PostgreSQL
   incident correctly, efficiently, and within defined safety boundaries.

```
whyslow --from 11:42 --to 11:47      # or: whyslow --last 15m
```

Install the `whyslow-db` distribution; the command remains `whyslow`:

```bash
pipx install whyslow-db
# or: python -m pip install whyslow-db
```

## Test database agents before trusting them

With Docker and Docker Compose installed, the package also includes nine
reproducible PostgreSQL incidents for evaluating AI agents and humans. Each run
creates a live but disposable database, lets a responder investigate and act,
then independently checks what actually changed.

The benchmark is **agent-agnostic**. Codex, Claude Code, another CLI agent, a
custom harness, or a human can work on the same task and be evaluated against
the same hidden ground truth. Codex and Claude Code have built-in command
timeline adapters; other harnesses can emit the provider-neutral JSONL format.

The nine scenarios cover operational failures and adversarial safety cases:

- lock contention, missing indexes, connection exhaustion, sequence
  exhaustion, trigger latency, and invalid-index recovery;
- prompt injection hidden in database evidence, synthetic-secret handling,
  and cross-tenant least-privilege repair.

Evaluators check incident recovery, data integrity, collateral damage, tenant
isolation, least privilege, unsafe operations, and the observable commands the
responder executed.

```bash
whyslow benchmark list
whyslow benchmark run pg_cross_tenant_access_v1 \
  --timeout 600 --reset-after -- claude
```

For a non-interactive Codex run, use `codex exec`. The generated benchmark
workspace is intentionally not a Git repository, so Codex also needs
`--skip-git-repo-check`:

```bash
whyslow benchmark run pg_cross_tenant_access_v1 \
  --timeout 600 --reset-after -- \
  codex exec --skip-git-repo-check --approve-for-me
```

The runner starts recognized Codex and Claude Code CLIs with a standard prompt
to read `task.md` and `ENV.md`, complete the incident, write `result.md`, and
exit. The delivered prompt is preserved in the trajectory bundle.

The runner records terminal events, PostgreSQL statements, workspace changes,
timing, the incident report, and the deterministic final-state evaluation in a
single timestamped bundle. Codex and Claude Code runs also include readable and
machine-readable tool timelines showing commands, outcomes, and file edits
without copying private reasoning. Other agent harnesses can emit the same
provider-neutral JSONL protocol.

Every structured run produces two independent scores:

- **Final state:** was the system repaired without breaking protected state or
  crossing the scenario's security boundary?
- **Trajectory:** how reliably, efficiently, and safely did the responder get
  there?

This is a focused PostgreSQL regression pack, not a guarantee of production
safety or a claim that an agent will behave safely outside the tested
boundaries. It is RL-compatible in the narrow reset → act → evaluate sense;
it evaluates responders but does not train models or make LLM API calls.

The database is disposable, but the agent process still runs on your host under
its own sandbox and approval policy. Do not disable those protections merely
because the benchmark database is isolated.

See **[the benchmark guide](https://github.com/kraftaa/whyslow/blob/main/whyslow/benchmark/README.md)** for the
setup → act → evaluate → reset workflow and security scenarios.

**During an incident, go straight to [RUNBOOK.md](https://github.com/kraftaa/whyslow/blob/main/RUNBOOK.md)** — what to type,
and what each answer means.

## Why this exists

"Production is slow" usually collapses into one of a few root causes — a
Postgres lock chain, an app-server thread pool pinned on slow queries, or a
resource-contention event (a reindex, a bulk load, autovacuum, a cronjob —
anything sharing the DB instance's CPU/IO). This correlates the three places
you'd otherwise check by hand and reconstructs the incident window into one
plain-English timeline.

## How incident reconstruction works

The incident-reconstruction mode uses no model, statistics, or scoring formula:
every conclusion is a lookup
against rows the collectors already wrote. Three collectors (Postgres, Puma,
CloudWatch) write to one SQLite file on a timer; `explain` reconstructs any
past window from that stored data, names the contributors, and shows the exact
evidence lines behind each one. Every tunable lives in plain sight at the top
of `explain.py`.

See **[docs/design.md](https://github.com/kraftaa/whyslow/blob/main/docs/design.md)** for the six-step mechanism, why it
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
is in **[INSTALL.md](https://github.com/kraftaa/whyslow/blob/main/INSTALL.md)**. Full setup, querying, events, and `diff`
are in **[docs/usage.md](https://github.com/kraftaa/whyslow/blob/main/docs/usage.md)**.

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

- **[RUNBOOK.md](https://github.com/kraftaa/whyslow/blob/main/RUNBOOK.md)** — what to type during an incident, and what each answer means
- **[INSTALL.md](https://github.com/kraftaa/whyslow/blob/main/INSTALL.md)** — versioned production install, checksums, systemd, rollback
- **[docs/usage.md](https://github.com/kraftaa/whyslow/blob/main/docs/usage.md)** — running collectors, querying, events, `diff`
- **[docs/operations.md](https://github.com/kraftaa/whyslow/blob/main/docs/operations.md)** — `status`/`doctor`, deployment, reliability & retention
- **[docs/design.md](https://github.com/kraftaa/whyslow/blob/main/docs/design.md)** — how it works, why no integrations, evidence, scope
- **[docs/publishing.md](https://github.com/kraftaa/whyslow/blob/main/docs/publishing.md)** — PyPI Trusted Publishing and release procedure
- **[JSON_OUTPUT.md](https://github.com/kraftaa/whyslow/blob/main/JSON_OUTPUT.md)** — the stable, versioned JSON contract
- **[AUDIT_LOG.md](https://github.com/kraftaa/whyslow/blob/main/AUDIT_LOG.md)** — bugs found by repeated audits, round by round
- **[writing/](https://github.com/kraftaa/whyslow/tree/main/writing)** — the four most transferable findings, written up as standalone posts
