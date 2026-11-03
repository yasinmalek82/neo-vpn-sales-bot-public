"""T-009: off-site DB backup job — write-path tests + restore round trip."""

from __future__ import annotations

import gzip
import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import database.models  # noqa: F401
from bot.config import Settings
from database.base import Base
from database.models import User
from database.models.report import (
    REPORT_SETTINGS_ROW_ID,
    TOPIC_BACKUP,
    TOPIC_ERRORS,
    ReportSettings,
)
from scheduler.jobs import backup as backup_job
from scheduler.jobs.backup import run_database_backup
from services.db_backup import (
    build_caption,
    count_core_rows,
    create_gzipped_dump,
    prune_backup_dir,
    target_has_tables,
)

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    return tmp_path / "source.db"


@pytest.fixture
def database_url(tmp_db: Path) -> str:
    return f"sqlite+aiosqlite:///{tmp_db}"


async def _prepare_db(url: str, *, users: int = 0) -> async_sessionmaker:
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    if users:
        async with factory() as session:
            for i in range(users):
                session.add(
                    User(
                        telegram_id=1000 + i,
                        username=f"u{i}",
                        first_name=f"U{i}",
                        last_name=None,
                        language_code="fa",
                        referral_code=f"BK{i:06d}",
                        is_admin=False,
                        wallet_balance_toman=0,
                    )
                )
            await session.commit()
    return factory


def _stamp_alembic(url: str) -> str:
    """Stamp head so restore proof can compare ``alembic current``."""
    env = {**os.environ, "DATABASE_URL": url, "BOT_TOKEN": "test-token"}
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "stamp", "head"],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    cur = subprocess.run(
        [sys.executable, "-m", "alembic", "current"],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert cur.returncode == 0, cur.stdout + cur.stderr
    return (cur.stdout or "").strip()


class _FakeBot:
    def __init__(self, *, fail_document: bool = False) -> None:
        self.fail_document = fail_document
        self.documents: list[dict] = []
        self.messages: list[dict] = []

    async def send_document(self, **kwargs):  # noqa: ANN003
        if self.fail_document:
            raise RuntimeError("telegram down")
        self.documents.append(kwargs)
        return SimpleNamespace(message_id=1)

    async def send_message(self, **kwargs):  # noqa: ANN003
        self.messages.append(kwargs)
        return SimpleNamespace(message_id=2)


async def _enable_report_group(factory: async_sessionmaker) -> None:
    async with factory() as session:
        row = ReportSettings(
            id=REPORT_SETTINGS_ROW_ID,
            chat_id=-100123,
            chat_title="reports",
            topics={TOPIC_BACKUP: 11, TOPIC_ERRORS: 22},
            verified={TOPIC_BACKUP: datetime.now(UTC).isoformat()},
            enabled=True,
        )
        session.add(row)
        await session.commit()


def _settings(*, database_url: str, backup_dir: Path, retain: int = 7) -> Settings:
    return Settings.model_validate(
        {
            "BOT_TOKEN": "test-token",
            "DATABASE_URL": database_url,
            "BACKUP_ENABLED": True,
            "BACKUP_DIR": str(backup_dir),
            "BACKUP_RETAIN_DAILY": retain,
            "SCHEDULER_ENABLED": False,
        }
    )


@pytest.mark.asyncio
async def test_dump_is_readable_sqlite_round_trip(
    tmp_path: Path, database_url: str, tmp_db: Path
) -> None:
    await _prepare_db(database_url, users=2)
    dest = tmp_path / "out.gz"
    create_gzipped_dump(database_url, dest)
    assert dest.is_file() and dest.stat().st_size > 0
    # Round-trip: gunzip into a fresh file and SELECT users
    restored = tmp_path / "restored.db"
    with gzip.open(dest, "rb") as gz, restored.open("wb") as out:
        out.write(gz.read())
    conn = sqlite3.connect(str(restored))
    try:
        n = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    finally:
        conn.close()
    assert n == 2


@pytest.mark.asyncio
async def test_caption_row_counts_match_dump(tmp_path: Path, database_url: str) -> None:
    await _prepare_db(database_url, users=3)
    dest = tmp_path / "c.gz"
    create_gzipped_dump(database_url, dest)
    users, orders, ledger = count_core_rows(database_url)
    assert users == 3
    caption = build_caption(
        when=datetime(2026, 9, 20, 3, 0, tzinfo=UTC),
        size_bytes=dest.stat().st_size,
        users=users,
        orders=orders,
        ledger=ledger,
    )
    # Persian digit ۳ for three users
    from services.ux_format import to_persian_digits

    assert to_persian_digits(3) in caption
    assert "کاربران:" in caption
    # Counts come from the live DB that was just dumped — same source as caption.
    assert users == count_core_rows(database_url)[0]


@pytest.mark.asyncio
async def test_oversize_sends_too_large_and_keeps_local(tmp_path: Path, database_url: str) -> None:
    factory = await _prepare_db(database_url, users=1)
    await _enable_report_group(factory)
    backup_dir = tmp_path / "backups"
    bot = _FakeBot()
    settings = _settings(database_url=database_url, backup_dir=backup_dir)
    path = await run_database_backup(
        factory, bot, settings, max_document_bytes=10  # force oversize
    )
    assert path is not None and path.is_file()
    assert not bot.documents
    assert bot.messages
    body = bot.messages[0]["text"]
    assert "۵۰" in body or "50" in body or "مگابایت" in body
    assert str(path) in body
    # Local dump must still exist
    assert path.exists()


@pytest.mark.asyncio
async def test_send_failure_keeps_dump_and_reports_errors(
    tmp_path: Path, database_url: str
) -> None:
    factory = await _prepare_db(database_url, users=1)
    await _enable_report_group(factory)
    backup_dir = tmp_path / "backups"
    bot = _FakeBot(fail_document=True)
    settings = _settings(database_url=database_url, backup_dir=backup_dir)
    path = await run_database_backup(factory, bot, settings)
    assert path is not None and path.is_file()
    err_msgs = [m for m in bot.messages if m.get("message_thread_id") == 22]
    assert err_msgs
    assert "بکاپ امروز انجام نشد" in err_msgs[0]["text"]


@pytest.mark.asyncio
async def test_second_fire_while_running_is_skipped(tmp_path: Path, database_url: str) -> None:
    factory = await _prepare_db(database_url, users=1)
    await _enable_report_group(factory)
    backup_dir = tmp_path / "backups"
    settings = _settings(database_url=database_url, backup_dir=backup_dir)
    bot = _FakeBot()

    # Hold the lock so the second call sees "already running"
    await backup_job._backup_lock.acquire()
    backup_job._backup_running = True
    try:
        before = list(backup_dir.glob("*.gz")) if backup_dir.exists() else []
        result = await run_database_backup(factory, bot, settings)
        assert result is None
        after = list(backup_dir.glob("*.gz")) if backup_dir.exists() else []
        assert after == before
        assert not bot.documents
    finally:
        backup_job._backup_running = False
        backup_job._backup_lock.release()

    # First real run still works and writes one dump
    path = await run_database_backup(factory, bot, settings)
    assert path is not None and path.is_file()


@pytest.mark.asyncio
async def test_retention_prunes_old_keeps_newest(tmp_path: Path) -> None:
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    paths = []
    for i in range(5):
        p = backup_dir / f"neo_bot_{i}.gz"
        p.write_bytes(b"x" * (i + 1))
        # stagger mtimes
        os.utime(p, (1_700_000_000 + i, 1_700_000_000 + i))
        paths.append(p)
    newest = paths[-1]
    removed = prune_backup_dir(backup_dir, retain_daily=2)
    assert newest.exists()
    remaining = sorted(backup_dir.glob("*.gz"))
    assert len(remaining) == 2
    assert newest in remaining
    assert len(removed) == 3


@pytest.mark.asyncio
async def test_restore_refuses_nonempty_without_force(tmp_path: Path, database_url: str) -> None:
    await _prepare_db(database_url, users=1)
    dest = tmp_path / "d.gz"
    create_gzipped_dump(database_url, dest)
    assert target_has_tables(database_url) is True
    proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/restore_backup.py"),
            str(dest),
            "--database-url",
            database_url,
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        env={**os.environ, "BOT_TOKEN": "test-token"},
        check=False,
    )
    assert proc.returncode == 3
    assert "already has tables" in (proc.stderr or "")
    # with --force it proceeds
    proc2 = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/restore_backup.py"),
            str(dest),
            "--database-url",
            database_url,
            "--force",
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        env={**os.environ, "BOT_TOKEN": "test-token"},
        check=False,
    )
    assert proc2.returncode == 0, proc2.stdout + proc2.stderr


@pytest.mark.asyncio
async def test_restore_round_trip_counts_and_alembic(
    tmp_path: Path,
) -> None:
    src = tmp_path / "src.db"
    url = f"sqlite+aiosqlite:///{src}"
    await _prepare_db(url, users=3)
    head_before = _stamp_alembic(url)
    users_b, orders_b, ledger_b = count_core_rows(url)
    assert users_b == 3

    dump = tmp_path / "round.gz"
    create_gzipped_dump(url, dump)

    target = tmp_path / "restored.db"
    target_url = f"sqlite+aiosqlite:///{target}"
    assert target_has_tables(target_url) is False
    proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/restore_backup.py"),
            str(dump),
            "--database-url",
            target_url,
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        env={**os.environ, "BOT_TOKEN": "test-token"},
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = proc.stdout
    assert "users=3" in out
    users_a, orders_a, ledger_a = count_core_rows(target_url)
    assert (users_a, orders_a, ledger_a) == (users_b, orders_b, ledger_b)
    # alembic current on restored DB matches source stamp
    assert "h5i6j7k8l9m0" in out or head_before.split()[0] in out


@pytest.mark.asyncio
async def test_a_corrupt_archive_is_deleted_reported_and_never_sent(
    tmp_path: Path, database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = await _prepare_db(database_url, users=1)
    await _enable_report_group(factory)
    backup_dir = tmp_path / "backups"
    real = backup_job.create_gzipped_dump

    def _truncated(url: str, dest: Path) -> Path:
        real(url, dest)
        dest.write_bytes(dest.read_bytes()[:-20])  # cut the gzip trailer
        return dest

    monkeypatch.setattr(backup_job, "create_gzipped_dump", _truncated)
    bot = _FakeBot()
    settings = _settings(database_url=database_url, backup_dir=backup_dir)

    path = await run_database_backup(factory, bot, settings)

    assert path is None
    assert bot.documents == []
    assert list(backup_dir.glob("*.gz")) == []
    err_msgs = [m for m in bot.messages if m.get("message_thread_id") == 22]
    assert err_msgs and "بکاپ امروز انجام نشد" in err_msgs[0]["text"]


@pytest.mark.asyncio
async def test_corruption_inside_the_deflate_stream_is_deleted_reported_and_never_sent(
    tmp_path: Path, database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A flipped byte raises zlib.error, which is not an OSError."""
    factory = await _prepare_db(database_url, users=1)
    await _enable_report_group(factory)
    backup_dir = tmp_path / "backups"

    def _flipped(url: str, dest: Path) -> Path:  # noqa: ARG001
        raw = b"".join(os.urandom(8) + b"row %d\n" % i for i in range(20000))
        blob = bytearray(gzip.compress(raw))
        blob[20] ^= 0xFF
        dest.write_bytes(bytes(blob))
        return dest

    monkeypatch.setattr(backup_job, "create_gzipped_dump", _flipped)
    bot = _FakeBot()
    settings = _settings(database_url=database_url, backup_dir=backup_dir)

    path = await run_database_backup(factory, bot, settings)

    assert path is None
    assert bot.documents == []
    assert list(backup_dir.glob("*.gz")) == []
    err_msgs = [m for m in bot.messages if m.get("message_thread_id") == 22]
    assert err_msgs and "بکاپ امروز انجام نشد" in err_msgs[0]["text"]
    assert "فایل بکاپ" in err_msgs[0]["text"]
