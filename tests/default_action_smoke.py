"""The default action is `explain`, so the primary command is
`whyslow --last 15m`, not `whyslow explain --last 15m`. Verify that
without breaking the explicit subcommands."""
import subprocess
import sys
import shutil

DB = "/tmp/whyslow_default/store.sqlite3"
shutil.rmtree("/tmp/whyslow_default", ignore_errors=True)


def run(*args):
    return subprocess.run(
        [sys.executable, "-m", "whyslow.cli", *args],
        capture_output=True, text=True,
    )


# Default action: no subcommand given.
r = run("--last", "15m", "--db", DB)
assert r.returncode == 0, f"default action failed: {r.stderr}"
assert "Incident Summary" in r.stdout, "bare `whyslow --last` should run explain"

# Explicit `explain` must still work identically.
r2 = run("explain", "--last", "15m", "--db", DB)
assert r2.returncode == 0
assert "Incident Summary" in r2.stdout

# Other subcommands must NOT be swallowed by the default.
r3 = run("status", "--db", DB)
assert "Collectors" in r3.stdout, "`status` must not be rerouted to explain"

r4 = run("event", "--source", "deploy", "--kind", "v1", "--db", DB)
assert r4.returncode == 0 and "recorded event" in r4.stdout

# Bare --help must not be prefixed with `explain`.
r5 = run("--help")
assert r5.returncode == 0
assert "collect-pg" in r5.stdout and "usage: whyslow" in r5.stdout

print("PASS: `whyslow --last 15m` defaults to explain; subcommands and --help unaffected")
