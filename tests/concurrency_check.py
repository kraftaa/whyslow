import subprocess
import sys
import shutil

shutil.rmtree("/tmp/concurrent_smoke", ignore_errors=True)

WRITER_CODE = """
import sys
from whyslow.storage import Store

store = Store("/tmp/concurrent_smoke/store.sqlite3")
errors = 0
for i in range(200):
    try:
        store.write_sessions(
            [(i, "client backend", "role", "app", "active", "cpu", "SELECT 1")],
            ts=float(i),
        )
    except Exception as e:
        errors += 1
        print(f"WRITER {sys.argv[1]} error on iteration {i}: {type(e).__name__}: {e}", file=sys.stderr)
print(f"WRITER {sys.argv[1]} done, {errors} errors")
"""

with open("/tmp/writer.py", "w") as f:
    f.write(WRITER_CODE)

# Simulate what actually happens in production: multiple collector
# processes (postgres + puma + cloudwatch) writing concurrently to the
# same SQLite file.
procs = [
    subprocess.Popen([sys.executable, "/tmp/writer.py", str(i)],
                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    for i in range(3)
]

total_errors = 0
for i, p in enumerate(procs):
    out, err = p.communicate(timeout=60)
    print(out.strip())
    if err.strip():
        print(err.strip())
    if "error" in out.lower() and "0 errors" not in out:
        total_errors += 1

print(f"\n{'FAIL' if total_errors else 'no errors observed'}: "
      f"{total_errors} of 3 concurrent writer processes hit at least one error")
