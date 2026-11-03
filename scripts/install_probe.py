"""Read-only credential probes invoked from the pinned bot image."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Any


async def _telegram(token: str) -> dict[str, Any]:
    from aiogram import Bot

    bot = Bot(token=token)
    try:
        me, webhook = await asyncio.gather(bot.get_me(), bot.get_webhook_info())
        return {"ok": True, "username": me.username, "has_webhook": bool(webhook.url)}
    finally:
        await bot.session.close()


async def _panel(base_url: str, api_key: str) -> dict[str, Any]:
    from api_client.client import PasarGuardClient

    client = PasarGuardClient(base_url=base_url, api_key=api_key)
    try:
        await client.get_current_admin()
        return {"ok": True}
    finally:
        await client.aclose()


async def _dispatch(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    if kind == "telegram":
        return await _telegram(str(payload.get("token", "")))
    if kind == "panel":
        return await _panel(str(payload.get("base_url", "")), str(payload.get("api_key", "")))
    return {"ok": False, "error": "unsupported_probe"}


def main(argv: list[str] | None = None) -> int:
    logging.disable(logging.CRITICAL)
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        payload = json.loads(sys.stdin.read())
        if isinstance(payload, dict):
            result = asyncio.run(_dispatch(args[0] if args else "", payload))
        else:
            result = {"ok": False, "error": "invalid_probe_input"}
    except Exception:
        result = {"ok": False, "error": "credential_check_failed"}
    print(json.dumps(result, separators=(",", ":")))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
