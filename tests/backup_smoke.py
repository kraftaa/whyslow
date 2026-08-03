import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time

from whyslow.storage import SCHEMA_VERSION, Store


with tempfile.TemporaryDirectory(prefix="whyslow-backup-") as temp:
    root = Path(temp)
    source_path = root / "store.sqlite3"
    backup_dir = root / "backups"

    seed = Store(source_path)
    seed.write_event("deploy", "v0.1.2", "sha=test", ts=time.time())
    seed.write_heartbeat("postgres", "writer", expected_interval=1.0)
    seed.close()

    stop = threading.Event()
    writer_errors = []

    def continuous_writer():
        store = Store(source_path)
        counter = 0
        while not stop.is_set():
            try:
                store.write_sessions(
                    [(
                        counter,
                        "client backend",
                        "role",
                        "web-1",
                        "active",
                        "cpu",
                        "SELECT 1",
                    )],
                    ts=time.time(),
                )
                counter += 1
            except Exception as exc:
                writer_errors.append((type(exc).__name__, str(exc)))
            time.sleep(0.005)
        store.close()

    writer = threading.Thread(target=continuous_writer)
    writer.start()
    time.sleep(0.2)

    backup_dir.mkdir(mode=0o700)
    online_path = backup_dir / "online.sqlite3"
    backup_store = Store(source_path)
    backup_store.backup(online_path)
    backup_store.close()

    stop.set()
    writer.join(timeout=5)
    assert not writer.is_alive(), "concurrent writer did not stop"
    assert not writer_errors, writer_errors[:3]
    assert online_path.is_file()
    assert stat.S_IMODE(online_path.stat().st_mode) == 0o600
    assert not list(backup_dir.glob("*.partial")), "partial backup leaked"

    with sqlite3.connect(online_path) as raw_backup:
        assert raw_backup.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        backed_up_rows = raw_backup.execute(
            "SELECT count(*) FROM session_changes"
        ).fetchone()[0]
        backed_up_version = int(raw_backup.execute(
            "SELECT value FROM schema_metadata WHERE key = 'schema_version'"
        ).fetchone()[0])
    assert backed_up_rows > 0
    assert backed_up_version == SCHEMA_VERSION

    original_bytes = online_path.read_bytes()
    overwrite_check = Store(source_path)
    try:
        overwrite_check.backup(online_path)
    except FileExistsError:
        pass
    else:
        raise AssertionError("backup overwrote an existing destination")
    finally:
        overwrite_check.close()
    assert online_path.read_bytes() == original_bytes

    source = Store(source_path)
    source_rows = len(source.sessions_in(0, time.time() + 1))
    source.close()
    assert source_rows >= backed_up_rows

    restored_path = root / "restored.sqlite3"
    restored_path.write_bytes(online_path.read_bytes())
    restored = Store(restored_path)
    assert restored.schema_version() == SCHEMA_VERSION
    assert len(restored.sessions_in(0, time.time() + 1)) == backed_up_rows
    assert restored.events_in(0, time.time() + 1)[0][1] == "deploy"
    restored.close()

    # The CLI creates uniquely named backups and retains only the requested
    # newest set without touching unrelated SQLite files.
    unrelated = backup_dir / "manual.sqlite3"
    unrelated.write_bytes(online_path.read_bytes())
    for _ in range(3):
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "whyslow.cli",
                "backup",
                "--db",
                os.fspath(source_path),
                "--output-dir",
                os.fspath(backup_dir),
                "--keep",
                "2",
            ],
            text=True,
            capture_output=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "backup ready" in result.stdout

    retained = sorted(backup_dir.glob("whyslow-*.sqlite3"))
    assert len(retained) == 2, retained
    assert unrelated.exists(), "retention removed a non-whyslow backup"
    assert stat.S_IMODE(backup_dir.stat().st_mode) == 0o700

print("PASS: online backups are atomic, private, restorable, and retention-safe")
