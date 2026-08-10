"""Audit the installed release and generate its CycloneDX SBOM.

The wheel is installed with every runtime extra in a fresh virtual environment.
Both the vulnerability audit and SBOM therefore describe what operators install,
not the repository's build and test toolchain.
"""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import venv


def run(command, **kwargs):
    print("+", " ".join(os.fspath(part) for part in command), flush=True)
    return subprocess.run(command, check=True, **kwargs)


def main():
    if len(sys.argv) != 3:
        raise SystemExit(
            "usage: release_security_smoke.py WHEEL OUTPUT_DIRECTORY"
        )

    wheel = Path(sys.argv[1]).resolve()
    output_dir = Path(sys.argv[2]).resolve()
    if not wheel.is_file() or wheel.suffix != ".whl":
        raise SystemExit(f"wheel not found: {wheel}")
    output_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="whyslow-security-") as temp:
        environment = Path(temp) / "venv"
        venv.EnvBuilder(with_pip=True).create(environment)
        scripts = environment / ("Scripts" if os.name == "nt" else "bin")
        python = scripts / ("python.exe" if os.name == "nt" else "python")

        run([python, "-m", "pip", "install", f"{wheel}[cloudwatch]"])
        installed_version = subprocess.check_output(
            [python, "-c", "from importlib.metadata import version; print(version('whyslow-db'))"],
            text=True,
        ).strip()
        site_packages = subprocess.check_output(
            [python, "-c", "import site; print(site.getsitepackages()[0])"],
            text=True,
        ).strip()

        # pip bootstraps the environment but is not a whyslow runtime
        # dependency. Remove it before auditing and inventorying the deployed
        # application so vulnerabilities in an unused installer do not become
        # false release blockers or SBOM components.
        run([python, "-m", "pip", "uninstall", "--yes", "pip"])

        run(
            [
                sys.executable,
                "-m",
                "pip_audit",
                "--path",
                site_packages,
                "--progress-spinner",
                "off",
            ]
        )

        sbom = output_dir / f"whyslow-{installed_version}.cdx.json"
        run(
            [
                sys.executable,
                "-m",
                "cyclonedx_py",
                "environment",
                os.fspath(python),
                "--output-format",
                "JSON",
                "--output-file",
                os.fspath(sbom),
            ]
        )

    document = json.loads(sbom.read_text())
    if document.get("bomFormat") != "CycloneDX":
        raise AssertionError("SBOM is not a CycloneDX document")
    components = document.get("components", [])
    whyslow = [component for component in components if component.get("name") == "whyslow-db"]
    if not whyslow or whyslow[0].get("version") != installed_version:
        raise AssertionError("SBOM does not identify the installed whyslow release")
    if not any(component.get("name") == "boto3" for component in components):
        raise AssertionError("SBOM is missing the CloudWatch runtime dependency")

    print(
        f"PASS: audited installed whyslow {installed_version} and wrote "
        f"{sbom.name} with {len(components)} components"
    )


if __name__ == "__main__":
    main()
