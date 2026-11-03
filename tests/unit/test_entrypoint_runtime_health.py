"""Execute the real entrypoint while replacing every external operation locally."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from bot.runtime_state import RUNTIME_HEALTH_PATH

SCRIPT = Path(__file__).resolve().parents[2] / "docker" / "entrypoint.sh"


def _command(path: Path, body: str) -> None:
    path.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "from pathlib import Path\n"
        "fixture = Path(os.environ['TEST_HEALTH_FIXTURE'])\n"
        "trace = Path(os.environ['TEST_TRACE'])\n" + body
    )
    path.chmod(0o700)


@pytest.mark.parametrize(
    ("stale_present", "invalidation_fails"), [(False, False), (True, False), (True, True)]
)
def test_entrypoint_invalidates_before_migrations_and_fails_closed(
    tmp_path: Path, stale_present: bool, invalidation_fails: bool
) -> None:
    binaries = tmp_path / "bin"
    binaries.mkdir()
    fixture = tmp_path / "synthetic-runtime-health.json"
    trace = tmp_path / "trace.txt"
    if stale_present:
        fixture.write_text('{"pid":1,"receiver_ok":true,"stopped":false}')
    _command(
        binaries / "rm",
        f"assert sys.argv[1:] == {['-f', str(RUNTIME_HEALTH_PATH)]!r}\n"
        "if os.environ['TEST_INVALIDATION_FAILS'] == '1':\n"
        "    trace.write_text('invalidation_failed\\n')\n"
        "    raise SystemExit(23)\n"
        "fixture.unlink(missing_ok=True)\n"
        "trace.write_text('invalidated\\n')\n",
    )
    _command(
        binaries / "alembic",
        "assert sys.argv[1:] == ['upgrade', 'head']\n"
        "assert not fixture.exists(), 'stale health reached migrations'\n"
        "assert trace.read_text() == 'invalidated\\n'\n"
        "trace.write_text(trace.read_text() + 'migrated\\n')\n",
    )
    final = binaries / "synthetic-final"
    _command(
        final,
        "assert sys.argv[1:] == ['synthetic-argument']\n"
        "assert trace.read_text() == 'invalidated\\nmigrated\\n'\n"
        "trace.write_text(trace.read_text() + 'final\\n')\n",
    )
    result = subprocess.run(
        ["/bin/sh", str(SCRIPT), str(final), "synthetic-argument"],
        env={
            # PATH contains only fakes: canonical rm is never executed on the host.
            "PATH": str(binaries),
            "TEST_HEALTH_FIXTURE": str(fixture),
            "TEST_TRACE": str(trace),
            "TEST_INVALIDATION_FAILS": "1" if invalidation_fails else "0",
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if invalidation_fails:
        assert result.returncode == 23
        assert trace.read_text().splitlines() == ["invalidation_failed"]
        assert fixture.exists()
    else:
        assert result.returncode == 0, result.stderr
        assert trace.read_text().splitlines() == ["invalidated", "migrated", "final"]
        assert not fixture.exists()
    assert not result.stderr
