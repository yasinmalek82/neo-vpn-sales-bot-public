"""Credential probes and explicitly requested Telegram handover."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from typing import Any


async def _telegram(
    token: str, *, remove_webhook: bool = False, expected_username: str = ""
) -> dict[str, Any]:
    from aiogram import Bot

    bot = Bot(token=token)
    try:
        me, webhook = await asyncio.gather(bot.get_me(), bot.get_webhook_info())
        if expected_username and me.username != expected_username:
            return {"ok": False, "error": "bot_identity_changed"}
        if remove_webhook:
            if not expected_username:
                return {"ok": False, "error": "bot_identity_changed"}
            if webhook.url:
                await bot.delete_webhook(drop_pending_updates=False)
                webhook = await bot.get_webhook_info()
            if webhook.url:
                return {"ok": False, "error": "webhook_still_active"}
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
    if kind == "telegram-remove-webhook":
        return await _telegram(
            str(payload.get("token", "")),
            remove_webhook=True,
            expected_username=str(payload.get("expected_username", "")),
        )
    if kind in {"telegram-config", "telegram-config-remove-webhook"}:
        expected = os.environ.get("BOT_USERNAME", "").lstrip("@")
        remove = kind == "telegram-config-remove-webhook"
        if not expected or (remove and payload.get("expected_username") != expected):
            return {"ok": False, "error": "bot_identity_changed"}
        return await _telegram(
            os.environ.get("BOT_TOKEN", ""), remove_webhook=remove, expected_username=expected
        )
    if kind == "panel":
        return await _panel(str(payload.get("base_url", "")), str(payload.get("api_key", "")))
    if kind == "configuration":
        from bot.config import Settings

        Settings(_env_file=None)
        result = {"ok": True}
    else:
        result = {"ok": False, "error": "unsupported_probe"}
    return result


def main(argv: list[str] | None = None) -> int:
    logging.disable(logging.CRITICAL)
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        payload = json.loads(sys.stdin.read())
        if isinstance(payload, dict):
            result = asyncio.run(
                asyncio.wait_for(_dispatch(args[0] if args else "", payload), timeout=45)
            )
        else:
            result = {"ok": False, "error": "invalid_probe_input"}
    except Exception:
        result = {"ok": False, "error": "credential_check_failed"}
    print(json.dumps(result, separators=(",", ":")))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
