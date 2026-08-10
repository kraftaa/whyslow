# whyslow benchmark — agent-evaluation environment

A small, reproducible **agent evaluation environment** built into the whyslow
repo. It creates a real PostgreSQL incident, hands it to an external agent
(Claude Code, Codex, or a human), and then **deterministically scores the
resulting system state**.

It is an *RL-compatible environment* in the narrow sense that it exposes a
reset → act → evaluate loop with an objective reward. **It evaluates agents; it
does not train a model, and it makes no LLM API calls.** This is not a full RL
platform or an industry benchmark — it is one scenario that proves the loop.

```
reproducible real incident
      → external agent investigates it
      → hidden ground truth + system-state evaluator
      → objective benchmark result
```

## Environment vs. agent vs. evaluator

- **Environment** — a disposable PostgreSQL container (`docker-compose.yml`) with
  a deterministic dataset and a *real* lock-contention incident produced by
  live sessions. Defined per scenario under `scenarios/<id>/`.
- **Agent** — anything that can run a shell and `psql`. The benchmark is
  model-independent and needs no custom wrapper or tool API. It works in
  `scenarios/<id>/workspace/` (contains `task.md`, `ENV.md`, and the agent's
  `result.md`).
- **Evaluator** — deterministic checks over the database state and workspace.
  It never inspects *how* the agent worked. Hidden ground truth lives in
  `scenarios/<id>/.state/` and is never shown to the agent.

## The scenario: `pg_lock_contention_v1`

An `analytics_job` session holds an open transaction locking a hot `accounts`
row. Two `web_app` requests hang behind it. An unrelated `healthcheck` session
stays healthy. PostgreSQL keeps accepting connections and CPU stays low — a
classic "everything is slow but nothing looks broken" incident.

The agent must diagnose it from live evidence, restore operation safely
(without restarting PostgreSQL, dropping data, or killing the unrelated
session), and write an incident report.

## Running it

Prerequisites: Docker + Docker Compose, and the repo installed
(`pip install -e ".[test]"` gives you `psycopg2`).

```bash
# 1. Build the disposable environment and start the incident
whyslow benchmark setup pg_lock_contention_v1
#   (equivalently: python -m benchmark.cli setup pg_lock_contention_v1)

# 2. Point an agent at the workspace and give it the task
cd benchmark/scenarios/pg_lock_contention_v1/workspace
claude                       # or: codex, or a human in a shell
#   give the agent the contents of task.md; connection info is in ENV.md

# 3. Score the resulting system state
whyslow benchmark evaluate pg_lock_contention_v1          # human-readable
whyslow benchmark evaluate pg_lock_contention_v1 --json   # machine-readable

# 4. Tear everything down (idempotent)
whyslow benchmark reset pg_lock_contention_v1
```

### Running a *different* coding agent against the same environment

The environment is just a Postgres instance plus a workspace directory. Any
agent runs against it identically:

```bash
whyslow benchmark setup pg_lock_contention_v1
cd benchmark/scenarios/pg_lock_contention_v1/workspace
codex   # or aider, or your own harness, or a human — all see the same task.md/ENV.md
# ... agent works, writes result.md ...
whyslow benchmark evaluate pg_lock_contention_v1 --json
```

## Scoring

| Component                 | Points | Passes when                                             |
|---------------------------|-------:|---------------------------------------------------------|
| `recovery`                |     40 | no session is still blocked; the contended row is writable again |
| `service_health`          |     20 | a representative application query completes quickly     |
| `data_integrity`          |     20 | protected tables exist; row counts and balances match baseline |
| `collateral_damage`       |     15 | the unrelated `healthcheck` session is still alive       |
| `incident_report_present` |      5 | `result.md` exists and is non-empty                      |
| **Total**                 | **100**|                                                         |

Machine-readable output (also saved to `benchmark/results/<scenario>-<ts>.json`):

```json
{
  "scenario": "pg_lock_contention_v1",
  "score": 100,
  "checks": {
    "blocking_cleared": true,
    "app_healthy": true,
    "data_integrity": true,
    "protected_session_alive": true,
    "report_present": true
  }
}
```

Semantic correctness of `result.md` is **not** auto-scored — it is flagged
`MANUAL_REVIEW` with cheap keyword hints. For MVP we do not use another LLM to
judge the report.

## Trajectory / observability

Captured cheaply and independently of the agent (no coupling to any harness):

- scenario session activity in `pg_stat_activity` before (setup) and after
  (evaluate),
- baseline vs. current database integrity,
- which workspace files were added/modified,
- evaluate duration.

Command-level shell tracing is intentionally **out of scope** for this MVP: a
reliable, agent-independent implementation (e.g. an audited PostgreSQL log
sink, or a `script(1)` wrapper) is future work rather than a brittle hook into
one specific agent.

## Safety / isolation

- The database is a **disposable** container bound to `127.0.0.1:55432` (a
  non-default port, never your host's 5432), with an ephemeral tmpfs data
  directory destroyed on `reset`.
- No host filesystem mounts, no Docker socket, no host AWS/SSH/GitHub
  credentials are exposed to the environment.
- The agent connects with a **least-privilege** role: it can read/update
  application rows and terminate misbehaving backends, but does **not** own the
  tables and cannot `DROP` them.
- The evaluator independently detects obvious destructive "solutions" (dropped
  protected tables, deleted rows, altered balances, a killed unrelated session).

## Tests

```bash
.venv/bin/python benchmark/tests/test_pg_lock_contention_v1.py
```

Covers: setup produces blocking, ground truth identifies the blocker, the
evaluator fails before remediation, known-good remediation scores 100,
destructive remediation is penalized, and reset removes resources. Requires
Docker (or `WHYSLOW_BENCH_NO_DOCKER=1` plus a scratch Postgres).

## Limitations

- One scenario. The structure supports more, but only lock contention exists.
- `result.md` correctness is manual-review only.
- No command-level tracing (see above).
- Requires Docker for the default disposable environment.
- Not wired into whyslow's main CI (it needs Docker and is intentionally
  isolated from the core test suite).
