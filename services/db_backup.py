"""Database dump + gzip for the off-site Telegram backup job.

Reads only. Never touches wallet, orders, or pricing writes — row counts are
SELECT COUNT(*) so a caption can prove the dump is not empty.
"""

from __future__ import annotations

import gzip
import logging
import os
import shutil
import sqlite3
import subprocess
import tempfile
import zlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

from services.ux_format import to_persian_digits

logger = logging.getLogger(__name__)

__all__ = [
    "TELEGRAM_MAX_DOCUMENT_BYTES",
    "BackupError",
    "DumpResult",
    "build_caption",
    "count_core_rows",
    "create_gzipped_dump",
    "format_size_fa",
    "is_postgres_url",
    "is_sqlite_url",
    "prune_backup_dir",
    "sqlite_path_from_url",
    "target_has_tables",
]

# Telegram Bot API document limit. Over this we keep the local file and tell
# the owner — silent failure would look like "backup ran" with nothing off-site.
TELEGRAM_MAX_DOCUMENT_BYTES = 50 * 1024 * 1024


class BackupError(Exception):
    """Dump or restore could not proceed; message is safe for admin copy."""


@dataclass(frozen=True)
class DumpResult:
    path: Path
    size_bytes: int
    users: int
    orders: int
    ledger: int


def is_sqlite_url(database_url: str) -> bool:
    return database_url.startswith("sqlite")


def is_postgres_url(database_url: str) -> bool:
    return database_url.startswith("postgresql") or database_url.startswith("postgres")


def sqlite_path_from_url(database_url: str) -> Path:
    """``sqlite+aiosqlite:///./data/bot.db`` → ``./data/bot.db``."""
    if not is_sqlite_url(database_url):
        raise BackupError("این آدرس دیتابیس اسکیولایت نیست.")
    # Strip driver: sqlite+aiosqlite:///path or sqlite:///path
    raw = database_url.split("://", 1)[-1]
    if raw.startswith("//"):
        # sqlite:////abs or sqlite:///relative — urlparse handles poorly
        raw = raw[1:] if raw.startswith("///") else raw
        if raw.startswith("//"):
            raw = raw[1:]
    # Common forms after split:
    #   /./data/bot.db  or  //./data/bot.db  or  /tmp/x.db
    if database_url.startswith("sqlite+aiosqlite:////") or database_url.startswith("sqlite:////"):
        # absolute: sqlite:////tmp/x.db → /tmp/x.db
        path = database_url.split("://", 1)[1]  # ///tmp/x.db or ////tmp?
        # aiosqlite absolute: sqlite+aiosqlite:////tmp/x → four slashes after scheme
        rest = database_url.split("://", 1)[1]
        path = "/" + rest.lstrip("/")
        return Path(path)
    if database_url.startswith("sqlite+aiosqlite:///") or database_url.startswith("sqlite:///"):
        rest = database_url.split(":///", 1)[1]
        return Path(rest)
    raise BackupError(f"آدرس اسکیولایت خوانده نشد: {database_url}")


def _postgres_connection_env(database_url: str) -> tuple[list[str], dict[str, str]]:
    """Build libpq connection arguments without putting the password in argv."""
    # postgresql+asyncpg://user:pass@host:5432/db → libpq URL without +driver
    cleaned = database_url
    for prefix in (
        "postgresql+asyncpg://",
        "postgresql+psycopg://",
        "postgresql+psycopg2://",
        "postgres+asyncpg://",
        "postgresql://",
        "postgres://",
    ):
        if cleaned.startswith(prefix):
            cleaned = "postgresql://" + cleaned[len(prefix) :]
            break
    parsed = urlparse(cleaned)
    if not parsed.hostname or not parsed.path:
        raise BackupError("آدرس پستگرس ناقص است.")
    env_pass = unquote(parsed.password) if parsed.password else ""
    cmd = [
        "pg_dump",
        "-h",
        parsed.hostname,
        "-p",
        str(parsed.port or 5432),
        "-U",
        unquote(parsed.username or ""),
        "-d",
        parsed.path.lstrip("/"),
    ]
    return cmd, {"PGPASSWORD": env_pass}


def _postgres_pg_dump_env(database_url: str) -> tuple[list[str], dict[str, str]]:
    cmd, env = _postgres_connection_env(database_url)
    return [*cmd, "--no-owner", "--no-acl", "-F", "p"], env


def _postgres_counts(database_url: str, query: str, *, columns: int) -> tuple[int, ...]:
    if shutil.which("psql") is None:
        raise BackupError("باینری psql روی سرور نیست؛ بررسی پستگرس ممکن نیست.")
    cmd, env = _postgres_connection_env(database_url)
    cmd[0] = "psql"
    try:
        proc = subprocess.run(
            [*cmd, "-X", "-w", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-c", query],
            env={**os.environ, **env, "PGCONNECT_TIMEOUT": "10"},
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        if proc.returncode != 0:
            raise BackupError("بررسی پستگرس ناموفق بود.")
        values = proc.stdout.strip().split("|")
        if len(values) != columns or any(not value.isdecimal() for value in values):
            raise BackupError("پاسخ شمارش پستگرس معتبر نیست.")
        return tuple(int(value) for value in values)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BackupError("بررسی پستگرس ناموفق بود.") from exc


def create_gzipped_dump(database_url: str, dest_gz: Path) -> Path:
    """Write a gzipped dump to ``dest_gz``; a failed dump leaves no partial file.

    See :func:`_write_gzipped_dump` for the per-engine details.
    """
    dest_gz = Path(dest_gz)
    try:
        return _write_gzipped_dump(database_url, dest_gz)
    except BaseException:
        # A half-written .gz is worse than none: prune keeps it as a «backup».
        dest_gz.unlink(missing_ok=True)
        raise


def _write_gzipped_dump(database_url: str, dest_gz: Path) -> Path:
    """Write a gzipped dump to ``dest_gz``. Parent dirs are created.

    SQLite uses ``Connection.backup()`` (not a file copy) so a concurrent writer
    cannot leave a torn page in the dump. Postgres shells out to ``pg_dump``;
    a missing binary is a hard error, not a silent skip.
    """
    dest_gz = Path(dest_gz)
    dest_gz.parent.mkdir(parents=True, exist_ok=True)
    if is_sqlite_url(database_url):
        src = sqlite_path_from_url(database_url)
        if not src.is_file():
            raise BackupError(f"فایل دیتابیس پیدا نشد: {src}")
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            src_conn = sqlite3.connect(f"file:{src.resolve()}?mode=ro", uri=True)
            try:
                dst_conn = sqlite3.connect(str(tmp_path))
                try:
                    src_conn.backup(dst_conn)
                finally:
                    dst_conn.close()
            finally:
                src_conn.close()
            with tmp_path.open("rb") as raw, gzip.open(dest_gz, "wb") as gz:
                shutil.copyfileobj(raw, gz)
        finally:
            tmp_path.unlink(missing_ok=True)
        return dest_gz

    if is_postgres_url(database_url):
        import os

        if shutil.which("pg_dump") is None:
            raise BackupError("باینری pg_dump روی سرور نیست؛ بکاپ پستگرس بدون آن ممکن نیست.")
        cmd, env = _postgres_pg_dump_env(database_url)
        try:
            proc = subprocess.run(
                cmd,
                check=False,
                capture_output=True,
                env={**os.environ, **env},
            )
        except FileNotFoundError as exc:
            raise BackupError(
                "باینری pg_dump روی سرور نیست؛ بکاپ پستگرس بدون آن ممکن نیست."
            ) from exc
        if proc.returncode != 0:
            err = (proc.stderr or b"").decode("utf-8", errors="replace")[:200]
            raise BackupError(f"pg_dump شکست خورد: {err or proc.returncode}")
        with gzip.open(dest_gz, "wb") as gz:
            gz.write(proc.stdout or b"")
        return dest_gz

    raise BackupError("نوع دیتابیس برای بکاپ پشتیبانی نمی‌شود.")


def verify_gzip_readable(path: Path, *, chunk_bytes: int = 1 << 20) -> None:
    """Read the whole ``.gz`` end to end; ``BackupError`` if it does not decompress.

    A truncated or corrupt archive looks fine on disk and in Telegram, and is
    discovered only on the day a restore is needed. Blocking: call in a thread.
    """
    try:
        with gzip.open(path, "rb") as fh:
            while fh.read(chunk_bytes):
                pass
    except (OSError, EOFError, zlib.error) as exc:
        raise BackupError("فایل بکاپ کامل یا سالم نیست") from exc


def count_core_rows(database_url: str) -> tuple[int, int, int]:
    """``(users, orders, wallet_ledger_entries)`` without an optional Python driver."""
    if is_sqlite_url(database_url):
        path = sqlite_path_from_url(database_url)
        conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
        try:
            cur = conn.cursor()

            def _count(table: str) -> int:
                cur.execute(f"SELECT COUNT(*) FROM {table}")  # noqa: S608 — fixed names
                return int(cur.fetchone()[0])

            return _count("users"), _count("orders"), _count("wallet_ledger_entries")
        finally:
            conn.close()

    if is_postgres_url(database_url):
        users, orders, ledger = _postgres_counts(
            database_url,
            "SELECT (SELECT COUNT(*) FROM users), (SELECT COUNT(*) FROM orders), "
            "(SELECT COUNT(*) FROM wallet_ledger_entries)",
            columns=3,
        )
        return users, orders, ledger

    raise BackupError("نوع دیتابیس برای شمارش پشتیبانی نمی‌شود.")


def format_size_fa(size_bytes: int) -> str:
    """Human size with Persian digits (KB / MB)."""
    n = max(0, int(size_bytes))
    if n < 1024:
        return f"{to_persian_digits(n)} بایت"
    if n < 1024 * 1024:
        kb = round(n / 1024, 1)
        text = f"{kb:.1f}".rstrip("0").rstrip(".")
        return f"{to_persian_digits(text)} کیلوبایت"
    mb = round(n / (1024 * 1024), 1)
    text = f"{mb:.1f}".rstrip("0").rstrip(".")
    return f"{to_persian_digits(text)} مگابایت"


def build_caption(
    *,
    when: datetime,
    size_bytes: int,
    users: int,
    orders: int,
    ledger: int,
) -> str:
    from bot.texts import texts

    date = to_persian_digits(when.strftime("%Y-%m-%d %H:%M"))
    return texts.admin_backup_caption.format(
        date=date,
        size=format_size_fa(size_bytes),
        users=to_persian_digits(users),
        orders=to_persian_digits(orders),
        ledger=to_persian_digits(ledger),
    )


def prune_backup_dir(backup_dir: Path, retain_daily: int) -> list[Path]:
    """Keep the newest ``retain_daily`` ``*.gz`` dumps; never delete the newest.

    Returns paths that were removed.
    """
    backup_dir = Path(backup_dir)
    if retain_daily <= 0 or not backup_dir.is_dir():
        return []
    files = sorted(
        [p for p in backup_dir.glob("*.gz") if p.is_file()],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not files:
        return []
    # Always keep index 0 (newest)
    keep = max(1, int(retain_daily))
    removed: list[Path] = []
    for path in files[keep:]:
        path.unlink(missing_ok=True)
        removed.append(path)
    return removed


def target_has_tables(database_url: str) -> bool:
    """True when the target already has user-facing tables (refuse restore)."""
    if is_sqlite_url(database_url):
        path = sqlite_path_from_url(database_url)
        if not path.is_file() or path.stat().st_size == 0:
            return False
        conn = sqlite3.connect(str(path))
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            return bool(rows)
        finally:
            conn.close()
    if is_postgres_url(database_url):
        (count,) = _postgres_counts(
            database_url,
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'",
            columns=1,
        )
        return count > 0
    raise BackupError("نوع دیتابیس برای بررسی هدف پشتیبانی نمی‌شود.")
