import subprocess
import sys
import time
from datetime import datetime, timezone

# Run the actual parse_time() function inside a subprocess with a
# different local timezone (America/Los_Angeles), to prove it no longer
# matters what timezone the machine running `whyslow` is in.

recorded_epoch = time.time()
utc_hhmm = datetime.fromtimestamp(recorded_epoch, tz=timezone.utc).strftime("%H:%M")

code = f"""
from whyslow.cli import parse_time
print(parse_time({utc_hhmm!r}))
"""

result = subprocess.run(
    [sys.executable, "-c", code],
    env={"TZ": "America/Los_Angeles", "PATH": "/usr/bin:/bin"},
    capture_output=True, text=True,
)
assert result.returncode == 0, f"subprocess failed: {result.stderr}"
parsed_epoch = float(result.stdout.strip())

diff_hours = abs(parsed_epoch - recorded_epoch) / 3600
print(f"recorded epoch:        {recorded_epoch}")
print(f"parsed epoch (LA tz):  {parsed_epoch}")
print(f"difference:            {diff_hours:.4f} hours")

assert diff_hours < 0.02, (
    f"parse_time() is still timezone-sensitive: {diff_hours:.2f}h off "
    f"when run under TZ=America/Los_Angeles"
)
print("\nPASS: parse_time() interprets HH:MM as UTC regardless of local system timezone")
