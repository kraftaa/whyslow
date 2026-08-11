# LinkedIn launch kit — whyslow v0.5.0

## Recommended post

Would you let an AI agent terminate a PostgreSQL backend in production?

“The model is smart” is not enough evidence for me.

I built **Whyslow**, a reproducible benchmark for database troubleshooting
agents. It creates real incidents in disposable PostgreSQL environments, gives
the same task to Claude Code, Codex, or another responder, and independently
checks two things:

1. Did the database actually recover without damaged data or collateral impact?
2. How did the agent behave—failed commands, repetition, time, approvals, and
   unsafe operations?

In one real run, Claude Code diagnosed a row-lock chain, terminated only the
root blocker, preserved the unrelated healthcheck session, and restored the
writable path:

- Final-state score: **100/100**
- Trajectory score: **94/100**
- Recovery time: **177.8 seconds**
- Failed commands: **0**
- Unsafe operations detected: **0**

Whyslow v0.5.0 contains nine scenarios:

- lock contention
- missing composite index
- connection exhaustion
- sequence exhaustion
- trigger-induced write latency
- invalid index recovery
- prompt injection in operational evidence
- synthetic-secret exposure
- cross-tenant privilege escalation with attack-trigger proof

This is not a claim that an agent is “production ready.” Nine scenarios are a
focused regression suite, not an industry-wide benchmark. The point is to
replace a convincing transcript with reproducible evidence—and to rerun the
same exam when the model, prompt, tools, or permissions change.

Install:

`pipx install whyslow-db`

Run:

`whyslow benchmark run pg_lock_contention_v1 --timeout 600 --reset-after -- claude`

Before trusting an AI agent on a real database, make it pass the incident tests.

#PostgreSQL #AIEngineering #AIAgents #DatabaseReliability #DevTools #SRE

## Short version

Claude can often fix a PostgreSQL incident. But can it do so repeatedly,
safely, without damaging data—and can you prove it?

I built Whyslow: nine reproducible PostgreSQL incidents with independent
final-state and behavior scoring. A real Claude Code lock-contention run scored
100/100 on recovery and 94/100 on trajectory quality, with zero failed or unsafe
commands detected.

Whyslow is the driving test, not the driving school.

`pipx install whyslow-db`

#PostgreSQL #AIAgents #SRE #AIEngineering

## 60-second demo script

```bash
pipx upgrade whyslow-db
whyslow --version
whyslow benchmark list
whyslow benchmark run pg_lock_contention_v1 \
  --timeout 600 --reset-after -- claude
```

Narration:

1. “Whyslow creates a live but disposable PostgreSQL incident.”
2. “The task is delivered automatically; I do not tell Claude the answer.”
3. “Claude can inspect PostgreSQL and request approval for remediation.”
4. “The first score verifies recovery, health, integrity, collateral damage,
   and the incident report.”
5. “The second score evaluates the observable trajectory.”
6. “Every command remains reviewable in `timeline.md`.”

## Suggested five-slide carousel

1. **Would you trust an AI agent with your database?**
2. **A confident answer is not evidence.** Show task → agent → hidden evaluator.
3. **Two independent scores.** Final database state and observable behavior.
4. **Real result.** 100/100 recovery, 94/100 trajectory, zero unsafe commands.
5. **Nine PostgreSQL incident tests.** End with the install and run commands.

## Claims to avoid

- Do not say Whyslow proves production readiness.
- Do not say Claude passed all nine scenarios until those runs exist.
- Do not call the suite an industry standard or comprehensive benchmark.
- Do not claim that trajectory safety rules detect every unsafe operation.
- Do not say approval behavior is scored for Claude; its local session format
  does not expose that telemetry.
