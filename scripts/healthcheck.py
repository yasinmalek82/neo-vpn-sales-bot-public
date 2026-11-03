#!/usr/bin/env python3
"""Container healthcheck: receiver/event-loop evidence plus DB SELECT 1.

Exit 0 on success, 1 on failure. Safe for docker HEALTHCHECK (no secrets printed).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


async def _check(*, runtime_path: Path | None = None) -> None:
    from sqlalchemy import text

    from bot.config import get_settings
    from bot.runtime_state import RUNTIME_HEALTH_PATH, check_runtime_health
    from database.base import close_db, get_engine, init_db

    check_runtime_health(runtime_path if runtime_path is not None else RUNTIME_HEALTH_PATH)
    settings = get_settings()
    # Touch a few critical fields so misconfig fails closed.
    if not (settings.bot_token or "").strip():
        raise RuntimeError("BOT_TOKEN missing")
    init_db(settings)
    engine = get_engine()
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    finally:
        await close_db()


def main() -> int:
    try:
        asyncio.run(_check())
    except Exception as exc:  # noqa: BLE001 — healthcheck must not leak secrets
        print(f"healthcheck_fail:{type(exc).__name__}", file=sys.stderr)
        return 1
    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
