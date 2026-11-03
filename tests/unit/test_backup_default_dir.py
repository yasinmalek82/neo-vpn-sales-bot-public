"""The nightly backup must land in a directory that exists when BACKUP_DIR is unset.

The default used to be ``/backups``: the root volume is read-only on the Mac, and in
Docker the process runs as user ``app`` in ``/app`` and the volume is mounted at
``/app/backups``, so ``mkdir`` raised every night and the job reported an internal error.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

import pytest

from bot.config import Settings
from scheduler.jobs.backup import run_database_backup
from tests.unit.test_backup_job import _FakeBot, _prepare_db

ROOT = Path(__file__).resolve().parents[2]


def _default_settings(database_url: str) -> Settings:
    return Settings.model_validate(
        {
            "BOT_TOKEN": "test-token",
            "DATABASE_URL": database_url,
            "BACKUP_ENABLED": True,
            "SCHEDULER_ENABLED": False,
        }
    )


@pytest.mark.asyncio
async def test_default_backup_dir_is_created_under_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)  # also hides the real .env from Settings
    monkeypatch.delenv("BACKUP_DIR", raising=False)
    factory = await _prepare_db(f"sqlite+aiosqlite:///{tmp_path / 'source.db'}", users=1)

    settings = _default_settings(f"sqlite+aiosqlite:///{tmp_path / 'source.db'}")
    path = await run_database_backup(factory, _FakeBot(), settings)

    assert path is not None
    dumps = list((tmp_path / "backups").glob("neo_bot_*.gz"))
    assert [d.resolve() for d in dumps] == [path.resolve()]


def test_default_backup_dir_is_a_volume_the_bot_container_mounts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BACKUP_DIR", raising=False)
    default = _default_settings("sqlite+aiosqlite:///x.db").backup_dir

    workdir = re.search(r"^WORKDIR\s+(\S+)", (ROOT / "docker/Dockerfile").read_text(), re.M)
    assert workdir is not None
    compose = (ROOT / "docker/docker-compose.yml").read_text()
    bot_service = compose.split("\n  db:")[0]  # the `bot` service block, before `db`
    mounts = set(re.findall(r"-\s*\.\./\w+:(/\S+?)(?::ro)?\s*$", bot_service, re.M))

    assert str(PurePosixPath(workdir.group(1)) / default) in mounts
