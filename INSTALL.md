# Production installation, upgrades, and rollback

Production runs from a versioned wheel in a dedicated virtual environment.
Editable installs (`pip install -e`) are for development only.

## Install a release

Download the release assets from the private repository using an authenticated
GitHub CLI, then verify their checksums:

```bash
gh release download v0.1.1 --repo kraftaa/whyslow --dir /tmp/whyslow-v0.1.1
cd /tmp/whyslow-v0.1.1
sha256sum --check SHA256SUMS
tar -xzf whyslow-0.1.1.tar.gz
```

Create a versioned environment and install the wheel with CloudWatch support:

```bash
sudo useradd --system --home-dir /var/lib/whyslow \
  --shell /usr/sbin/nologin whyslow
sudo install -d -o whyslow -g whyslow -m 0700 /var/lib/whyslow
sudo mkdir -p /opt/whyslow/releases
sudo python3 -m venv /opt/whyslow/releases/0.1.1
sudo /opt/whyslow/releases/0.1.1/bin/python -m pip install --upgrade pip
sudo /opt/whyslow/releases/0.1.1/bin/python -m pip install \
  'whyslow[cloudwatch] @ file:///tmp/whyslow-v0.1.1/whyslow-0.1.1-py3-none-any.whl'
sudo ln -sfn /opt/whyslow/releases/0.1.1 /opt/whyslow/venv
/opt/whyslow/venv/bin/whyslow --version
```

Install the units from the extracted `whyslow-0.1.1/deploy/` directory into
`/etc/systemd/system/`. They deliberately
invoke `/opt/whyslow/venv/bin/whyslow`, so switching the `venv` symlink selects
one complete, immutable installation for every collector and the prune timer.
Keep environment files under `/etc/whyslow/` root-owned and mode `0600` as
described in [RUNBOOK.md](RUNBOOK.md).

Before enabling services, run:

```bash
sudo -u whyslow /opt/whyslow/venv/bin/whyslow doctor \
  --db /var/lib/whyslow/store.sqlite3
sudo systemctl daemon-reload
sudo systemctl enable --now whyslow-collect-pg
sudo systemctl enable --now whyslow-collect-cw
sudo systemctl enable --now whyslow-prune.timer
```

Enable one `whyslow-collect-puma@HOST` instance for each Puma target.

## Upgrade

Never replace packages inside the running environment. Build the new
versioned environment first, verify it, and keep the previous one intact.

```bash
sudo systemctl stop 'whyslow-collect-puma@*' whyslow-collect-cw whyslow-collect-pg
sudo sqlite3 /var/lib/whyslow/store.sqlite3 \
  ".backup '/var/lib/whyslow/store.before-upgrade.sqlite3'"

# Download, checksum, create /opt/whyslow/releases/NEW_VERSION, and install
# the new wheel exactly as in the installation section. Then:
sudo ln -sfn /opt/whyslow/releases/NEW_VERSION /opt/whyslow/venv
/opt/whyslow/venv/bin/whyslow --version
sudo -u whyslow /opt/whyslow/venv/bin/whyslow doctor \
  --db /var/lib/whyslow/store.sqlite3
sudo systemctl start whyslow-collect-pg whyslow-collect-cw
sudo systemctl start 'whyslow-collect-puma@*'
```

The first command that opens the evidence store applies any pending numbered
SQLite migrations transactionally. Do not remove the pre-upgrade backup until
the new version has collected successfully and `whyslow doctor` is ready.

## Roll back

Stop collectors before changing either binaries or SQLite state:

```bash
sudo systemctl stop 'whyslow-collect-puma@*' whyslow-collect-cw whyslow-collect-pg
sudo ln -sfn /opt/whyslow/releases/PREVIOUS_VERSION /opt/whyslow/venv
```

Run `whyslow doctor`. If it says the evidence schema is newer than the old
binary supports, restore the pre-upgrade database before restarting:

```bash
sudo cp /var/lib/whyslow/store.before-upgrade.sqlite3 \
  /var/lib/whyslow/store.sqlite3
sudo chown whyslow:whyslow /var/lib/whyslow/store.sqlite3
sudo chmod 0600 /var/lib/whyslow/store.sqlite3
sudo systemctl start whyslow-collect-pg whyslow-collect-cw
sudo systemctl start 'whyslow-collect-puma@*'
```

Restoring the backup discards evidence collected after that backup. This is
why old releases refuse a newer schema rather than attempting a destructive
downgrade.

## Creating a release

Update the single version in `whyslow/__init__.py`, merge with green CI, then
push a matching tag such as `v0.1.1`. The release workflow rejects a tag that
does not exactly match the package version. It builds and checks the wheel and
source distribution, runs the isolated wheel smoke test, audits the installed
runtime dependency tree for known vulnerabilities, and creates a versioned
CycloneDX JSON SBOM. `SHA256SUMS` covers the wheel, source archive, and SBOM;
all four files are attached to the GitHub release.
