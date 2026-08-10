"""Build-artifact test: install the wheel without importing the source tree."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import venv

from whyslow import __version__ as expected_version


if len(sys.argv) != 2:
    raise SystemExit("usage: wheel_install_smoke.py DIST.whl")

wheel = Path(sys.argv[1]).resolve()
if not wheel.is_file() or wheel.suffix != ".whl":
    raise SystemExit(f"wheel not found: {wheel}")

sdist = wheel.with_name(
    wheel.name.removesuffix("-py3-none-any.whl") + ".tar.gz"
)
if not sdist.is_file():
    raise SystemExit(f"source distribution not found: {sdist}")

with tarfile.open(sdist, "r:gz") as archive:
    names = archive.getnames()
    roots = {name.split("/", 1)[0] for name in names}
    assert len(roots) == 1, roots
    root = next(iter(roots))
    required_source_files = {
        f"{root}/INSTALL.md",
        f"{root}/JSON_OUTPUT.md",
        f"{root}/RUNBOOK.md",
        f"{root}/deploy/whyslow-collect-pg.service",
        f"{root}/deploy/whyslow-collect-puma@.service",
        f"{root}/deploy/whyslow-collect-cw.service",
        f"{root}/deploy/whyslow-prune.service",
        f"{root}/deploy/whyslow-prune.timer",
        f"{root}/deploy/whyslow-backup.service",
        f"{root}/deploy/whyslow-backup.timer",
    }
    missing = required_source_files - set(names)
    assert not missing, f"source distribution omitted: {sorted(missing)}"

with tempfile.TemporaryDirectory(prefix="whyslow-wheel-") as temp:
    root = Path(temp)
    environment = root / "venv"
    venv.EnvBuilder(with_pip=True).create(environment)
    executable_dir = environment / ("Scripts" if os.name == "nt" else "bin")
    python = executable_dir / ("python.exe" if os.name == "nt" else "python")
    whyslow = executable_dir / ("whyslow.exe" if os.name == "nt" else "whyslow")
    child_env = os.environ.copy()
    child_env.pop("PYTHONPATH", None)

    install = subprocess.run(
        [
            str(python), "-m", "pip", "install",
            f"whyslow-db[cloudwatch] @ {wheel.as_uri()}",
        ],
        cwd=root,
        env=child_env,
        text=True,
        capture_output=True,
    )
    assert install.returncode == 0, install.stdout + install.stderr

    version = subprocess.run(
        [str(whyslow), "--version"], cwd=root, env=child_env,
        text=True, capture_output=True,
    )
    assert version.returncode == 0, version.stderr
    assert version.stdout.strip() == f"whyslow {expected_version}", version.stdout

    imports = subprocess.run(
        [
            str(python), "-c",
            "import boto3, psycopg2, whyslow; "
            "from whyslow.benchmark.common import COMPOSE_FILE; "
            "from importlib.metadata import version; "
            "assert COMPOSE_FILE.is_file(); "
            "assert version('whyslow-db') == whyslow.__version__ == "
            f"{expected_version!r}",
        ],
        cwd=root,
        env=child_env,
        text=True,
        capture_output=True,
    )
    assert imports.returncode == 0, imports.stdout + imports.stderr

    scenarios = subprocess.run(
        [str(whyslow), "benchmark", "list"], cwd=root, env=child_env,
        text=True, capture_output=True,
    )
    assert scenarios.returncode == 0, scenarios.stderr
    assert "pg_lock_contention_v1" in scenarios.stdout, scenarios.stdout
    assert "pg_prompt_injection_v1" in scenarios.stdout, scenarios.stdout

    status = subprocess.run(
        [str(whyslow), "status", "--json", "--db", str(root / "evidence.sqlite3")],
        cwd=root,
        env=child_env,
        text=True,
        capture_output=True,
    )
    assert status.returncode == 1, status.stderr
    payload = json.loads(status.stdout)
    assert payload["type"] == "status"
    assert payload["schema_version"] == 1
    assert payload["ok"] is False

print(f"PASS: validated source archive and exercised isolated wheel {wheel.name}")
