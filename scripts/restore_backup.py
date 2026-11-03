#!/usr/bin/env python3
"""Restore a gzipped Neo Bot dump into a target database and verify it.

Refuses a non-empty target unless ``--force`` is passed — restoring over a live
database is the one way this script can cause the disaster it exists to prevent.

Usage:
  source .venv/bin/activate
  python scripts/restore_backup.py path/to/neo_bot_YYYYMMDD.gz \\
      --database-url sqlite+aiosqlite:////tmp/restored.db

  python scripts/restore_backup.py dump.gz \\
      --database-url postgresql+asyncpg://... --force
"""

from __future__ import annotations

import argparse
import gzip
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

# Repo root on sys.path so ``services`` / ``bot`` import cleanly when run as a script.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from services.db_backup import (  # noqa: E402
    BackupError,
    count_core_rows,
    is_postgres_url,
    is_sqlite_url,
    sqlite_path_from_url,
    target_has_tables,
)


def _restore_sqlite(dump_gz: Path, database_url: str) -> None:
    target = sqlite_path_from_url(database_url)
    target.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(dump_gz, "rb") as gz, target.open("wb") as out:
        shutil.copyfileobj(gz, out)
    # Sanity: must open as sqlite
    conn = sqlite3.connect(str(target))
    try:
        conn.execute("SELECT COUNT(*) FROM users").fetchone()
    finally:
        conn.close()


def _restore_postgres(dump_gz: Path, database_url: str) -> None:
    if shutil.which("psql") is None:
        raise BackupError("باینری psql روی سرور نیست؛ ری‌استور پستگرس بدون آن ممکن نیست.")
    cleaned = database_url
    for prefix in (
        "postgresql+asyncpg://",
        "postgresql+psycopg://",
        "postgresql+psycopg2://",
        "postgres+asyncpg://",
    ):
        if cleaned.startswith(prefix):
            cleaned = "postgresql://" + cleaned[len(prefix) :]
            break
    from urllib.parse import unquote, urlparse

    parsed = urlparse(cleaned)
    env = {**os.environ}
    if parsed.password:
        env["PGPASSWORD"] = unquote(parsed.password)
    cmd = [
        "psql",
        "-h",
        parsed.hostname or "",
        "-p",
        str(parsed.port or 5432),
        "-U",
        unquote(parsed.username or ""),
        "-d",
        (parsed.path or "").lstrip("/"),
        "-v",
        "ON_ERROR_STOP=1",
    ]
    with gzip.open(dump_gz, "rb") as gz:
        proc = subprocess.run(cmd, input=gz.read(), env=env, capture_output=True, check=False)
    if proc.returncode != 0:
        err = (proc.stderr or b"").decode("utf-8", errors="replace")[:300]
        raise BackupError(f"psql شکست خورد: {err or proc.returncode}")


def _alembic_current(database_url: str) -> str:
    """Run ``alembic current`` against ``database_url``; return stdout strip."""
    env = {**os.environ, "DATABASE_URL": database_url}
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", "current"],
        cwd=str(_ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        raise BackupError(f"alembic current شکست خورد:\n{out[-500:]}")
    return out.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Restore a Neo Bot gzipped DB dump.")
    parser.add_argument("dump", type=Path, help="Path to *.gz dump from the backup job")
    parser.add_argument(
        "--database-url",
        required=True,
        help="Target DATABASE_URL (sqlite+aiosqlite or postgresql+asyncpg)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Allow restore into a database that already has tables",
    )
    args = parser.parse_args(argv)

    dump = Path(args.dump)
    if not dump.is_file():
        print(f"dump not found: {dump}", file=sys.stderr)
        return 2
    url = args.database_url

    try:
        if target_has_tables(url) and not args.force:
            print(
                "target database already has tables; pass --force to overwrite",
                file=sys.stderr,
            )
            return 3
        if is_sqlite_url(url):
            _restore_sqlite(dump, url)
        elif is_postgres_url(url):
            _restore_postgres(dump, url)
        else:
            print("unsupported database URL", file=sys.stderr)
            return 2
        users, orders, ledger = count_core_rows(url)
        print(f"users={users}")
        print(f"orders={orders}")
        print(f"wallet_ledger_entries={ledger}")
        current = _alembic_current(url)
        print("alembic current:")
        print(current)
    except BackupError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
