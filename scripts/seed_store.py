#!/usr/bin/env python3
"""One-time seed: pricing.json → draft+publish StoreVersion (idempotent).

Usage:
  BOT_TOKEN=… DATABASE_URL=… python -m scripts.seed_store
  BOT_TOKEN=… DATABASE_URL=… python -m scripts.seed_store --path config/pricing.json
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bot.config import get_settings  # noqa: E402
from database.base import get_session_factory, init_db  # noqa: E402
from services.store_config import seed_store_from_pricing_json  # noqa: E402


async def _main(path: Path) -> int:
    settings = get_settings()
    init_db(settings)
    factory = get_session_factory()
    async with factory() as session:
        version = await seed_store_from_pricing_json(session, path)
        await session.commit()
    if version is None:
        print("seed no-op: published store already exists")
        return 0
    print(f"seeded+published store version={version.version} id={version.id}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed store catalog from pricing.json")
    parser.add_argument(
        "--path",
        type=Path,
        default=None,
        help="Path to pricing JSON (default: settings.pricing_config_path)",
    )
    args = parser.parse_args()
    settings = get_settings()
    path = args.path or Path(settings.pricing_config_path)
    raise SystemExit(asyncio.run(_main(path)))


if __name__ == "__main__":
    main()
