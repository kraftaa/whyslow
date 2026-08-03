# JSON output contract

`explain`, `diff`, and `status` accept `--json`. JSON is written to stdout;
usage and runtime errors remain on stderr, and command exit codes keep their
normal meaning.

```bash
whyslow --last 15m --json
whyslow diff --last 15m --baseline-last 15m --json
whyslow status --json
```

Every document contains:

```json
{
  "schema_version": 1,
  "type": "explain"
}
```

Consumers should select a parser using both fields and ignore unknown fields.
Adding a field is backward-compatible. Removing a field, renaming one, or
changing its type requires a new `schema_version`.

## `explain`

- `window`: exact `start_ts` and `end_ts` epoch seconds.
- `coverage`: per-collector and per-role evidence coverage.
- `contributors`: confidence, named signals, blocking PIDs, durations, and
  resolution state.
- `timeline`: ordered objects containing `ts`, human-readable `message`, and
  structured `evidence`. CloudWatch evidence includes `source_instance`.

## `diff`

- `windows`: exact baseline and incident windows.
- `baseline` and `incident`: session, blocking, Puma, and CloudWatch summaries.
- Set-like fields such as roles, applications, and maintenance labels are
  emitted as sorted arrays for deterministic output.

## `status`

- `ok`: the same health verdict represented by the process exit code.
- `observed_at`: epoch timestamp used for heartbeat ages.
- `collectors`: active collector health and database role.
- `retired_collectors`: preserved historical membership.
- `coverage`: row counts and stored time ranges by evidence table.

An unhealthy status still emits a complete JSON document and exits non-zero,
so shell monitoring can use the exit code while another process consumes the
details.
