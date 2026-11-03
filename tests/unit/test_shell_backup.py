"""Execute the production shell backup with a controlled external dump process."""

from __future__ import annotations

import gzip
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "docker" / "backup.sh"


@pytest.mark.parametrize("failure", ["dump", "empty", "compress"])
def test_failed_backup_preserves_previous_generation(tmp_path: Path, failure: str) -> None:
    binaries = tmp_path / "bin"
    binaries.mkdir()
    dump = binaries / "pg_dump"
    dump.write_text(
        "#!/bin/sh\nprintf 'partial dump'\nexit 1\n"
        if failure == "dump"
        else "#!/bin/sh\nexit 0\n" if failure == "empty" else "#!/bin/sh\nprintf 'complete dump'\n"
    )
    dump.chmod(0o700)
    if failure == "compress":
        compressor = binaries / "gzip"
        compressor.write_text("#!/bin/sh\nprintf 'partial gzip'\nexit 1\n")
        compressor.chmod(0o700)
    backups = tmp_path / "backups"
    daily = backups / "daily"
    daily.mkdir(parents=True)
    previous = daily / "previous.sql.gz"
    previous_bytes = gzip.compress(b"recoverable prior backup")
    previous.write_bytes(previous_bytes)
    os.utime(previous, (1, 1))

    result = subprocess.run(
        ["/bin/sh", str(SCRIPT)],
        env={**os.environ, "PATH": f"{binaries}:{os.environ['PATH']}", "BACKUP_DIR": str(backups)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0, result.stdout
    assert "Backup completed" not in result.stdout
    assert previous.read_bytes() == previous_bytes
    assert list(daily.iterdir()) == [previous]


def test_successful_backup_publishes_complete_dump_then_prunes(tmp_path: Path) -> None:
    binaries = tmp_path / "bin"
    binaries.mkdir()
    dump = binaries / "pg_dump"
    dump.write_text("#!/bin/sh\nprintf 'CREATE TABLE restored (id int);\\n'\n")
    dump.chmod(0o700)
    backups = tmp_path / "backups"
    daily = backups / "daily"
    daily.mkdir(parents=True)
    previous = daily / "previous.sql.gz"
    previous.write_bytes(gzip.compress(b"old"))
    os.utime(previous, (1, 1))

    result = subprocess.run(
        ["/bin/sh", str(SCRIPT)],
        env={**os.environ, "PATH": f"{binaries}:{os.environ['PATH']}", "BACKUP_DIR": str(backups)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Backup completed" in result.stdout
    assert not previous.exists()
    archives = list(daily.iterdir())
    assert len(archives) == 1
    assert gzip.decompress(archives[0].read_bytes()) == b"CREATE TABLE restored (id int);\n"
