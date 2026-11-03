#!/usr/bin/env python3
"""Post-restart / deploy smoke checks (settings, DB path, optional panel probe).

Usage (from repo root, with .env loaded via Settings):
  python scripts/post_restart_check.py
  make post-restart

Does not start the bot or create a database. Reads an existing database and
requires its exact Alembic release head. No network by default. An explicitly
authorized --probe-panel checks GET /api/admin with DB-effective connection settings.
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot.config import Settings
    from services.panel_settings import EffectivePasarGuardConfig

# Repo root on sys.path when run as scripts/post_restart_check.py
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _expected_revision() -> str:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config(str(_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_ROOT / "alembic"))
    heads = ScriptDirectory.from_config(config).get_heads()
    if len(heads) != 1:
        raise RuntimeError("release_requires_one_head")
    return heads[0]


async def _read_database(
    settings: Settings, *, include_panel: bool = False
) -> tuple[list[str], EffectivePasarGuardConfig | None]:
    """Read an existing target; never create a DB, directory or app session global."""
    from sqlalchemy import text
    from sqlalchemy.engine import make_url
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from database.models.pasarguard_settings import PasarGuardSettings
    from services.panel_settings import get_effective_pasarguard_config, resolve_pasarguard_config

    url = make_url(settings.database_url)
    if url.get_backend_name() == "sqlite":
        if url.database in {None, "", ":memory:"}:
            raise ValueError("existing_file_required")
        path = Path(url.database).resolve()
        if not path.is_file():
            raise FileNotFoundError("database_missing")
        # Native read-only connection bypasses application's global SQLite
        # writer setup (which enables WAL and may change an existing file).
        with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as connection:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            revisions = [
                r[0] for r in connection.execute("SELECT version_num FROM alembic_version")
            ]
            cfg = None
            if include_panel:
                row = connection.execute(
                    "SELECT base_url,api_key,sub_path,timeout FROM pasarguard_settings WHERE id=1"
                ).fetchone()
                stored = (
                    PasarGuardSettings(
                        base_url=row[0], api_key=row[1], sub_path=row[2], timeout=row[3]
                    )
                    if row is not None
                    else None
                )
                cfg = resolve_pasarguard_config(settings, stored)
            return revisions, cfg
    if url.get_backend_name() != "postgresql":
        raise ValueError("unsupported_database")
    engine = create_async_engine(url, isolation_level="REPEATABLE READ")
    try:
        async with engine.connect() as connection, connection.begin():
            await connection.exec_driver_sql("SET TRANSACTION READ ONLY")
            revisions = list(
                (
                    await connection.execute(text("SELECT version_num FROM alembic_version"))
                ).scalars()
            )
            cfg = None
            if include_panel:
                async with AsyncSession(bind=connection, autoflush=False) as session:
                    cfg = await get_effective_pasarguard_config(session, settings)
            return revisions, cfg
    finally:
        await engine.dispose()


async def _check_db(settings: Settings) -> tuple[str, bool]:
    try:
        expected = _expected_revision()
        revisions, _ = await _read_database(settings)
        if revisions != [expected]:
            return f"FAIL: database schema differs from release head {expected}", False
    except Exception as exc:  # noqa: BLE001 — never expose DSNs or credentials
        return f"FAIL: database verification ({type(exc).__name__})", False
    return f"OK: existing database, read-only check, schema {expected}", True


async def _probe_panel(settings: Settings) -> tuple[str, bool]:
    from api_client.factory import create_pasarguard_client_from_config

    try:
        _, cfg = await _read_database(settings, include_panel=True)
        if cfg is None or not cfg.uses_api_key or not cfg.base_url or "example.com" in cfg.base_url:
            return "FAIL: effective panel configuration is incomplete", False
        # Snapshot/connection closed before the network await. The same DB-over-
        # environment policy is used by application handlers.
        client = create_pasarguard_client_from_config(cfg)
        try:
            await client.get_current_admin()
        finally:
            await client.aclose()
    except Exception as exc:  # noqa: BLE001 — no response body/key/URL in output
        return f"FAIL: effective panel probe ({type(exc).__name__})", False
    return "OK: effective panel GET /api/admin succeeded", True


def _print_checklist() -> None:
    print()
    print("=== Manual Telegram retest / چک‌لیست دستی تلگرام ===")
    print("EN:")
    print("  1. Open the bot → /start (menu loads)")
    print("  2. «سرویس‌های من» / My services — list + open one detail (QR / link)")
    print("  3. «راهنمای اتصال» / Connection howto — OS picker works")
    print("  4. Optional: wallet balance, buy flow, admin panel if you are admin")
    print("FA:")
    print("  ۱. ربات را باز کنید → /start (منو بیاید)")
    print("  ۲. «سرویس‌های من» — لیست و جزئیات یک سرویس (QR / لینک)")
    print("  ۳. «راهنمای اتصال» — انتخاب سیستم‌عامل درست کار کند")
    print("  ۴. اختیاری: کیف پول، خرید، پنل ادمین")
    print()


async def _async_main(*, probe_panel: bool = False) -> int:
    print("==> post_restart_check")
    try:
        from bot.config import Settings

        settings = Settings()  # type: ignore[call-arg]
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL: cannot load settings ({type(exc).__name__})")
        return 1

    print(f"OK: settings loaded (run_mode={settings.run_mode})")

    db_msg, db_ok = await _check_db(settings)
    print(db_msg)

    if not db_ok:
        return 1
    panel_ok = True
    if probe_panel:
        panel_msg, panel_ok = await _probe_panel(settings)
        print(panel_msg)
    else:
        print("NOT RUN: panel probe (requires --probe-panel and live authorization)")

    _print_checklist()

    if not panel_ok:
        return 1
    print(
        "post_restart_check: PASS (DB/schema"
        + (" + panel API" if probe_panel else "; panel NOT RUN")
        + "; Telegram UI/monitoring NOT VERIFIED)"
    )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--probe-panel",
        action="store_true",
        help="Explicit read-only live panel API probe using effective DB settings",
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_async_main(probe_panel=args.probe_panel)))


if __name__ == "__main__":
    main()
